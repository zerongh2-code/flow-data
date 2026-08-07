#!/usr/bin/env python3
"""FLOW cloud data engine.

Runs on a GitHub Actions schedule. Fetches US stock/ETF quotes from Twelve
Data (key in repo secrets, never in any browser) and writes market.json;
optionally (RUN_AI=1 + ANTHROPIC_API_KEY) generates the AI market brief in
three languages and writes ai.json. Both files are served publicly by GitHub
Pages and consumed by the FLOW terminal as its keyless data source.
"""
import json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

TD_KEY = os.environ.get("TWELVEDATA_KEY", "")
AI_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
RUN_AI = os.environ.get("RUN_AI", "") == "1"

SYMBOLS = [
    # priority
    "AAPL", "NVDA", "MSFT", "TSLA", "META", "AMZN", "GOOGL", "SPY",
    # core US
    "AVGO", "TSM", "LLY", "JPM", "V", "XOM", "AMD", "PLTR", "ORCL", "CRWD",
    "NFLX", "UNH", "CAT", "GE", "VRT", "SMCI", "MU", "ASML", "COIN", "NEE",
    "CEG", "ARM",
    # China ADRs
    "BABA", "PDD", "TCEHY", "JD", "BIDU", "NTES", "NIO", "LI", "XPEV",
    # ETFs
    "QQQ", "VOO", "IWM", "SMH", "IBIT", "ETHA", "GLD", "TLT", "EEM", "XLE",
    "XLF", "ARKK", "URA", "BOTZ", "JEPI", "KWEB", "FXI", "MCHI", "ASHR",
]


def jget(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "flow-data/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def et_market_window():
    """True during US pre-market through after-hours (Mon-Fri ~08:00-21:00 ET)."""
    now = datetime.now(timezone.utc)
    # ET is UTC-4 (EDT) or UTC-5 (EST); use a generous UTC window covering both
    if now.weekday() >= 5:
        return False
    h = now.hour
    return 12 <= h or h < 2  # 12:00 UTC ≈ 08:00 EDT → 01:59 UTC ≈ 21:59 EST


def fetch_quotes():
    prev = {}
    if os.path.exists("market.json"):
        try:
            prev = json.load(open("market.json")).get("quotes", {})
        except Exception:
            pass
    if not TD_KEY:
        print("no TWELVEDATA_KEY — skipping stock fetch")
        return prev, False
    if not et_market_window() and prev:
        print("outside market window — keeping previous quotes")
        return prev, False
    quotes = dict(prev)
    batches = [SYMBOLS[i:i + 8] for i in range(0, len(SYMBOLS), 8)]
    for bi, batch in enumerate(batches):
        url = f"https://api.twelvedata.com/quote?symbol={','.join(batch)}&apikey={TD_KEY}"
        j = None
        for attempt in range(3):   # retry the SAME batch on rate limit — never skip it
            try:
                j = jget(url)
            except Exception as e:
                print(f"batch {bi} attempt {attempt}: {e}")
                time.sleep(61)
                continue
            if j.get("code") == 429:
                print(f"batch {bi} attempt {attempt}: rate limited, retrying")
                j = None
                time.sleep(65)
                continue
            break
        if j is None:
            print(f"batch {bi}: gave up after retries")
            continue
        m = {j["symbol"]: j} if j.get("symbol") else j
        ok = 0
        for s in batch:
            o = m.get(s, {})
            if o.get("close"):
                quotes[s] = {
                    "c": float(o["close"]), "pc": float(o["previous_close"]),
                    "dp": float(o.get("percent_change") or 0),
                    "h": float(o.get("high") or 0), "l": float(o.get("low") or 0),
                    "t": int(o.get("timestamp") or 0),
                    "open": o.get("is_market_open", False),
                }
                ok += 1
            else:
                print(f"  {s}: {o.get('code')} {str(o.get('message'))[:60]}")
        print(f"batch {bi + 1}/{len(batches)}: {ok}/{len(batch)} quotes")
        if bi < len(batches) - 1:
            time.sleep(61)
    return quotes, True


def crypto_snapshot():
    """Keyless context for the AI brief."""
    out = {}
    try:
        ids = "bitcoin,ethereum,solana,binancecoin,ripple,pax-gold"
        rows = jget(f"https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&ids={ids}&price_change_percentage=24h")
        out["crypto"] = [
            {"sym": r["symbol"].upper(), "px": r["current_price"], "chg": r.get("price_change_percentage_24h") or 0}
            for r in rows]
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
        for q in ["crypto", "AI", "stock market"]:
            j = jget(f"https://hn.algolia.com/api/v1/search_by_date?query={urllib.request.quote(q)}&tags=story&hitsPerPage=4")
            heads += [h["title"] for h in j.get("hits", []) if h.get("title")]
        out["news"] = heads[:10]
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
    "zh-Hant": "\n\nIMPORTANT: Write all content in Traditional Chinese (繁體中文, as used in Hong Kong). Keep only the four section labels exactly in English capitals as specified above.",
    "zh-Hans": "\n\nIMPORTANT: Write all content in Simplified Chinese (简体中文). Keep only the four section labels exactly in English capitals as specified above.",
}


def build_ai_snapshot(quotes, ctx):
    L = [f"Snapshot time: {datetime.now(timezone.utc).strftime('%a, %d %b %Y %H:%M UTC')}"]
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
        L.append(f"\n== US STOCKS & ETFS ({len(live)} live quotes) ==")
        for s, q in sorted(live, key=lambda x: -abs(x[1]["dp"]))[:14]:
            L.append(f"{s} ${q['c']:,} ({q['dp']:+.2f}%)")
    if ctx.get("news"):
        L.append("\n== HEADLINES (real, recent) ==")
        for h in ctx["news"]:
            L.append(f"- {h}")
    return "\n".join(L)


def anthropic_call(system, user):
    body = json.dumps({
        "model": "claude-opus-5", "max_tokens": 2048,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body, method="POST",
        headers={"content-type": "application/json", "x-api-key": AI_KEY,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=300) as r:
        j = json.loads(r.read().decode())
    if j.get("stop_reason") == "refusal":
        raise RuntimeError("model refused")
    return "\n".join(b["text"] for b in j.get("content", []) if b.get("type") == "text").strip()


def main():
    quotes, fetched = fetch_quotes()
    market = {
        "updated": int(time.time() * 1000),
        "fetched_fresh": fetched,
        "quotes": quotes,
    }
    json.dump(market, open("market.json", "w"), separators=(",", ":"))
    print(f"market.json written: {len(quotes)} symbols, fresh={fetched}")

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
        print("RUN_AI set but no ANTHROPIC_API_KEY secret — skipping AI briefs")


if __name__ == "__main__":
    main()
