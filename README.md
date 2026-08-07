# flow-data

Cloud data engine for the FLOW market terminal. GitHub Actions fetches stock
quotes every 15 minutes (Twelve Data, key in repo secrets) and generates AI
market briefs three times daily (Anthropic, optional secret). Outputs
`market.json` and `ai.json`, served publicly via GitHub Pages. Contains no
site code and no secrets.
