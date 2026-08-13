#!/usr/bin/env python3
"""FLOW cloud data engine v2.

Publishes market.json (quotes), sp500.json (S&P 500 constituents) and ai.json
(AI briefs) via GitHub Pages for the FLOW terminal.

Quote sources, in order of preference:
  FINNHUB_KEY   - 60 req/min free: sweeps the FULL universe (core + S&P 500,
                  ~560 symbols) every run. Designed for a 30-min cron.
  TWELVEDATA_KEY- 8 credits/min, 800/day free: strictly budget-gated. Runs at
                  most every 2h, 64 credits per run: priority symbols plus a
                  rotating window over the rest of the universe.
"""
import csv, io, json, os, time, urllib.request, urllib.parse
from datetime import datetime, timezone

FH_KEY = os.environ.get("FINNHUB_KEY", "")
TD_KEY = os.environ.get("TWELVEDATA_KEY", "")
AI_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RUN_AI = os.environ.get("RUN_AI", "") == "1"

CORE = [
    "AAPL", "NVDA", "MSFT", "TSLA", "META", "AMZN", "GOOGL", "SPY",
    "AVGO", "TSM", "LLY", "JPM", "V", "XOM", "AMD", "PLTR", "ORCL", "CRWD",
    "NFLX", "UNH", "CAT", "GE", "VRT", "SMCI", "MU", "ASML", "COIN", "NEE",
    "CEG", "ARM",
    "BABA", "PDD", "TCEHY", "JD", "BIDU", "NTES", "NIO", "LI", "XPEV",
    "QQQ", "VOO", "IWM", "SMH", "IBIT", "ETHA", "GLD", "TLT", "EEM", "XLE",
    "XLF", "ARKK", "URA", "BOTZ", "JEPI", "KWEB", "FXI", "MCHI", "ASHR",
]
CONSTITUENTS_URL = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"


def jget(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "flow-data/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def load_prev(path):
    if os.path.exists(path):
        try:
            return json.load(open(path))
        except Exception:
            pass
    return {}


def et_market_window():
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return False
    h = now.hour
    return 12 <= h or h < 2


def fetch_sp500():
    """Refresh constituent list (daily is plenty)."""
    prev = load_prev("sp500.json")
    if prev.get("updated") and time.time() * 1000 - prev["updated"] < 20 * 3600e3:
        return [x["s"] for x in prev.get("list", [])]
    try:
        req = urllib.request.Request(CONSTITUENTS_URL, headers={"User-Agent": "flow-data/2.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            rows = list(csv.DictReader(io.StringIO(r.read().decode())))
        lst = [{"s": x["Symbol"].strip(), "n": x["Security"].strip(), "sec": x["GICS Sector"].strip()}
               for x in rows if x.get("Symbol")]
        if len(lst) > 400:
            json.dump({"updated": int(time.time() * 1000), "list": lst}, open("sp500.json", "w"), separators=(",", ":"))
            print(f"sp500.json refreshed: {len(lst)} constituents")
        return [x["s"] for x in lst]
    except Exception as e:
        print("constituents fetch failed:", e)
        return [x["s"] for x in prev.get("list", [])]



def load_custom_symbols():
    """User-editable ticker list + name resolution → custom.json."""
    syms = []
    if os.path.exists("custom_symbols.txt"):
        for line in open("custom_symbols.txt"):
            t = line.strip().upper()
            if t and not t.startswith("#") and len(t) <= 8:
                syms.append(t)
    prev = load_prev("custom.json")
    known = {x["s"]: x for x in prev.get("list", [])}
    out = []
    changed = False
    for sym in syms:
        if sym in known:
            out.append(known[sym])
            continue
        name = sym
        try:
            req = urllib.request.Request(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym.replace('.', '-'))}?interval=1d&range=1d",
                headers={"User-Agent": "Mozilla/5.0 (Macintosh) flow-data/2.1"})
            with urllib.request.urlopen(req, timeout=15) as r:
                m = json.loads(r.read().decode())["chart"]["result"][0]["meta"]
            name = m.get("longName") or m.get("shortName") or sym
            time.sleep(1)
        except Exception as e:
            print(f"custom {sym}: name lookup failed ({e}) — using ticker as name")
        out.append({"s": sym, "n": name, "sec": "Custom"})
        changed = True
    if changed or len(out) != len(known):
        json.dump({"updated": int(time.time() * 1000), "list": out}, open("custom.json", "w"), separators=(",", ":"))
        print(f"custom.json: {len(out)} custom symbols")
    return [x["s"] for x in out]


def fetch_finnhub(universe, quotes):
    ok = fail = 0
    for i, sym in enumerate(universe):
        try:
            q = jget(f"https://finnhub.io/api/v1/quote?symbol={urllib.parse.quote(sym)}&token={FH_KEY}", timeout=15)
            if q.get("c"):
                dp = q.get("dp")
                quotes[sym] = {"c": q["c"], "pc": q.get("pc", 0), "dp": dp if dp is not None else 0,
                               "h": q.get("h", 0), "l": q.get("l", 0), "t": q.get("t", 0)}
                ok += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            if fail <= 3:
                print(f"  {sym}: {e}")
            if "429" in str(e):
                time.sleep(30)
        time.sleep(1.05)  # 60/min ceiling
        if i and i % 100 == 0:
            print(f"  ...{i}/{len(universe)} ({ok} ok)")
    print(f"finnhub sweep: {ok} ok, {fail} failed")
    return ok



def fetch_yahoo(universe, quotes):
    """Keyless fallback: Yahoo spark endpoint, 20 symbols per call."""
    ok = 0
    chunks = [universe[i:i + 20] for i in range(0, len(universe), 20)]
    for ci, chunk in enumerate(chunks):
        ysyms = ",".join(sym.replace(".", "-") for sym in chunk)
        url = f"https://query1.finance.yahoo.com/v8/finance/spark?symbols={urllib.parse.quote(ysyms)}&range=1d&interval=1d"
        j = None
        for attempt in range(2):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) flow-data/2.1"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    j = json.loads(r.read().decode())
                break
            except Exception as e:
                print(f"yahoo chunk {ci} attempt {attempt}: {e}")
                time.sleep(20)
        if not j:
            continue
        for sym in chunk:
            d = j.get(sym.replace(".", "-"))
            if not d:
                continue
            closes = [c for c in (d.get("close") or []) if c is not None]
            pc = d.get("chartPreviousClose")
            ts = (d.get("timestamp") or [0])[-1]
            if closes and pc:
                c = closes[-1]
                quotes[sym] = {"c": round(c, 4), "pc": pc, "dp": round((c / pc - 1) * 100, 3),
                               "h": 0, "l": 0, "t": ts}
                ok += 1
        time.sleep(1.5)
        if ci and ci % 10 == 0:
            print(f"  yahoo ...{ci}/{len(chunks)} chunks ({ok} ok)")
    print(f"yahoo sweep: {ok} quotes from {len(chunks)} calls")
    return ok


def fetch_twelvedata(universe, quotes, prev):
    # hard budget gate: at most one fetch per ~2h -> <= 768 credits/day
    if prev.get("src") == "twelvedata" and time.time() * 1000 - prev.get("updated", 0) < 110 * 60e3:
        print("TD budget gate: last fetch <110 min ago — skipping")
        return -1
    rot = prev.get("rot", 0)
    rest = [s for s in universe if s not in CORE[:8] and "." not in s]
    window = [rest[(rot + i) % len(rest)] for i in range(56)] if rest else []
    todo = CORE[:8] + window
    batches = [todo[i:i + 8] for i in range(0, len(todo), 8)]
    ok = 0
    for bi, batch in enumerate(batches):
        url = f"https://api.twelvedata.com/quote?symbol={','.join(batch)}&apikey={TD_KEY}"
        j = None
        for attempt in range(3):
            try:
                j = jget(url)
            except Exception as e:
                print(f"batch {bi} attempt {attempt}: {e}")
                time.sleep(61)
                continue
            if j.get("code") == 429:
                j = None
                time.sleep(65)
                continue
            if j.get("code") == 401:
                print("TD key invalid (401) — aborting TD fetch")
                return 0
            break
        if j is None:
            continue
        m = {j["symbol"]: j} if j.get("symbol") else j
        for s in batch:
            o = m.get(s, {})
            if o.get("close"):
                quotes[s] = {"c": float(o["close"]), "pc": float(o["previous_close"]),
                             "dp": float(o.get("percent_change") or 0),
                             "h": float(o.get("high") or 0), "l": float(o.get("low") or 0),
                             "t": int(o.get("timestamp") or 0)}
                ok += 1
        if bi < len(batches) - 1:
            time.sleep(61)
    prev["rot"] = (rot + 56) % max(len(rest), 1)
    print(f"twelvedata rotation: {ok} quotes, next rot={prev['rot']}")
    return ok



def yahoo_req(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Macintosh) flow-data/2.1"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def fetch_history(universe):
    """Daily price history: 6mo closes for everything, 3mo OHLC+volume for CORE.
    Refreshed at most once per 20h."""
    prev = load_prev("history.json")
    if prev.get("updated") and time.time() * 1000 - prev["updated"] < 20 * 3600e3:
        print("history fresh — skipping")
        return
    spark = {}
    chunks = [universe[i:i + 20] for i in range(0, len(universe), 20)]
    for ci, chunk in enumerate(chunks):
        ysyms = ",".join(x.replace(".", "-") for x in chunk)
        try:
            j = yahoo_req(f"https://query1.finance.yahoo.com/v8/finance/spark?symbols={urllib.parse.quote(ysyms)}&range=6mo&interval=1d")
        except Exception as e:
            print(f"hist chunk {ci}: {e}")
            time.sleep(10)
            continue
        for sym in chunk:
            d = j.get(sym.replace(".", "-"))
            if d and d.get("close"):
                pts = [(t, c) for t, c in zip(d.get("timestamp") or [], d["close"]) if c is not None]
                if len(pts) >= 5:
                    spark[sym] = {"t": [p[0] for p in pts], "c": [round(p[1], 4) for p in pts]}
        time.sleep(1.5)
    ohlc = {}
    for sym in CORE:
        try:
            j = yahoo_req(f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym.replace('.', '-'))}?range=3mo&interval=1d")
            res = j["chart"]["result"][0]
            ts = res.get("timestamp") or []
            q = res["indicators"]["quote"][0]
            rows = [(t, o, hh, ll, c, v or 0) for t, o, hh, ll, c, v in
                    zip(ts, q["open"], q["high"], q["low"], q["close"], q.get("volume") or [0] * len(ts))
                    if None not in (o, hh, ll, c)]
            if len(rows) >= 5:
                ohlc[sym] = {"t": [r[0] for r in rows],
                             "o": [round(r[1], 4) for r in rows], "h": [round(r[2], 4) for r in rows],
                             "l": [round(r[3], 4) for r in rows], "c": [round(r[4], 4) for r in rows],
                             "v": [r[5] for r in rows]}
        except Exception as e:
            print(f"ohlc {sym}: {e}")
        time.sleep(1.2)
    json.dump({"updated": int(time.time() * 1000), "spark": spark, "ohlc": ohlc},
              open("history.json", "w"), separators=(",", ":"))
    print(f"history.json: {len(spark)} line series, {len(ohlc)} OHLC series")


def crypto_snapshot():
    out = {}
    try:
        ids = "bitcoin,ethereum,solana,binancecoin,ripple,pax-gold"
        rows = jget(f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={ids}&price_change_percentage=24h")
        out["crypto"] = [{"sym": r["symbol"].upper(), "px": r["current_price"],
                          "chg": r.get("price_change_percentage_24h") or 0} for r in rows]
    except Exception as e:
        print("coingecko failed:", e)
    try:
        g = jget("https://api.coingecko.com/api/v3/global")["data"]
        out["global"] = {"mcapT": g["total_market_cap"]["usd"] / 1e12,
                         "chg": g["market_cap_change_percentage_24h_usd"],
                         "btcDom": g["market_cap_percentage"]["btc"]}
    except Exception as e:
        print("global failed:", e)
    try:
        f = jget("https://api.alternative.me/fng/?limit=1")["data"][0]
        out["fng"] = {"v": int(f["value"]), "label": f["value_classification"]}
    except Exception as e:
        print("fng failed:", e)
    try:
        heads = []
        for q in ["crypto", "AI", "stock market", "Federal Reserve"]:
            j = jget(f"https://hn.algolia.com/api/v1/search_by_date?query={urllib.parse.quote(q)}&tags=story&hitsPerPage=4")
            heads += [h["title"] for h in j.get("hits", []) if h.get("title")]
        out["news"] = heads[:12]
    except Exception as e:
        print("news failed:", e)
    return out


AI_SYSTEM = """You are the markets desk analyst for FLOW, a private market-intelligence terminal. The reader is a private investor. Write in the voice of a professional cross-asset morning note — analytical, causal, specific.

Format:

THE TRADE THAT MATTERED — open with the single most important story in the snapshot. State the causal chain explicitly: what happened, what repriced, what that means. If the obvious headline is NOT what markets are actually trading, say so plainly.

Then 2-5 short untitled paragraphs, each anchored on the asset or theme that best expresses today's tape. Name the cleanest expression of the day's driver; when price action disagrees with headlines, explain what the market is actually trading; separate first-order from second-order stories; weave relevant headlines into the asset paragraphs. Cite exact numbers from the snapshot for every claim. Never pad.

⚠ NEEDS YOUR ATTENTION — 2-4 short lines, most important first: the biggest movers with a concrete reason and a level or event to watch. Actionable, never generic.

WHAT TO WATCH — next scheduled market events with times in both ET and HKT (e.g. "US cash open 09:30 ET / 21:30 HKT"). Only standing, certain events; never invent data prints or speeches.

Ground every number in the snapshot. Plain text, the three section labels in caps exactly as above, no markdown.
End with the single line: "Automated analysis — not investment advice."
"""

LANG_LINES = {
    "en": "",
    "zh-Hant": "\n\nIMPORTANT: Write all content in Traditional Chinese (as used in Hong Kong). Keep only the section labels exactly in English capitals as specified above.",
    "zh-Hans": "\n\nIMPORTANT: Write all content in Simplified Chinese. Keep only the section labels exactly in English capitals as specified above.",
}


def build_ai_snapshot(quotes, ctx):
    now = datetime.now(timezone.utc)
    L = [f"Snapshot time: {now.strftime('%a, %d %b %Y %H:%M UTC')} (HKT = UTC+8)"]
    if ctx.get("crypto"):
        L.append("\n== CRYPTO (live) ==")
        for c in ctx["crypto"]:
            name = "GOLD (via PAXG)" if c["sym"] == "PAXG" else c["sym"]
            L.append(f"{name} ${c['px']:,} ({c['chg']:+.2f}% 24h)")
    if ctx.get("fng"):
        L.append(f"Crypto Fear & Greed: {ctx['fng']['v']} ({ctx['fng']['label']})")
    if ctx.get("global"):
        g = ctx["global"]
        L.append(f"Total crypto mcap ${g['mcapT']:.2f}T ({g['chg']:+.1f}% 24h), BTC dominance {g['btcDom']:.1f}%")
    live = [(s, q) for s, q in quotes.items() if q.get("c")]
    if live:
        L.append(f"\n== US STOCKS & ETFS ({len(live)} quotes) ==")
        for s, q in sorted(live, key=lambda x: -abs(x[1].get("dp", 0)))[:16]:
            L.append(f"{s} ${q['c']:,} ({q.get('dp', 0):+.2f}%)")
    if ctx.get("news"):
        L.append("\n== HEADLINES (real, recent) ==")
        for h in ctx["news"]:
            L.append(f"- {h}")
    return "\n".join(L)


def anthropic_call(system, user):
    body = json.dumps({"model": "claude-opus-5", "max_tokens": 2048, "system": system,
                       "messages": [{"role": "user", "content": user}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                                 headers={"content-type": "application/json", "x-api-key": AI_KEY,
                                          "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=300) as r:
        j = json.loads(r.read().decode())
    if j.get("stop_reason") == "refusal":
        raise RuntimeError("model refused")
    return "\n".join(b["text"] for b in j.get("content", []) if b.get("type") == "text").strip()


def main():
    sp500 = fetch_sp500()
    custom = load_custom_symbols()
    universe = CORE + [s for s in sp500 if s not in CORE] + [s for s in custom if s not in CORE and s not in sp500]
    prev = load_prev("market.json")
    quotes = dict(prev.get("quotes", {}))

    fetched = 0
    # outside US market hours, skip only if we already hold full coverage —
    # last-close data for the whole universe matters to Asia-hours readers
    if not et_market_window() and len(quotes) >= 400:
        print("outside market window with full coverage — keeping previous quotes")
    elif FH_KEY:
        fetched = fetch_finnhub(universe, quotes)
        prev["src"] = "finnhub"
    else:
        # keyless fallback first (Yahoo), then Twelve Data if a key exists
        fetched = fetch_yahoo(universe, quotes)
        if fetched > 0:
            prev["src"] = "yahoo"
        elif TD_KEY:
            r = fetch_twelvedata(universe, quotes, prev)
            if r >= 0:
                fetched = r
                prev["src"] = "twelvedata"
            else:
                print("budget-gated: no fetch this run")
                return
        else:
            print("no quote source available this run")

    market = {"updated": int(time.time() * 1000), "src": prev.get("src", ""),
              "rot": prev.get("rot", 0), "quotes": quotes}
    json.dump(market, open("market.json", "w"), separators=(",", ":"))
    print(f"market.json: {len(quotes)} symbols (fetched {fetched} this run, src={market['src']})")

    try:
        fetch_history(universe)
    except Exception as e:
        print("history fetch failed:", e)

    if RUN_AI and AI_KEY:
        ctx = crypto_snapshot()
        snap = build_ai_snapshot(quotes, ctx)
        out = {"at": int(time.time() * 1000), "model": "claude-opus-5", "briefs": {}}
        for lang, line in LANG_LINES.items():
            try:
                out["briefs"][lang] = anthropic_call(AI_SYSTEM + line, "Current market data snapshot:\n\n" + snap)
                print(f"ai brief ({lang}): {len(out['briefs'][lang])} chars")
            except Exception as e:
                print(f"ai brief ({lang}) failed: {e}")
        if out["briefs"]:
            json.dump(out, open("ai.json", "w"))
            print("ai.json written")
    elif RUN_AI:
        print("RUN_AI set but no ANTHROPIC_API_KEY secret — skipping")


if __name__ == "__main__":
    main()
