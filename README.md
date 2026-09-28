# Averixa Market Radar Lite

Free, scheduled Etsy market research dashboard. GitHub Actions collects a compact public-market snapshot every hour at :23 and deploys the static dashboard to GitHub Pages. Runs may be delayed by GitHub.

Every saved listing ID is refreshed even when it disappears from keyword search results. Searches fill empty slots in the stable 400-listing pool. The dashboard shows how many listings were actually refreshed and distinguishes unchanged counters, missing view data, and unavailable comparisons.

The collector reads Etsy's quota headers, retains a 10% daily reserve (at least 25 calls), and makes at most 8 calls per run at no more than one call per 1.1 seconds. Refreshing a full pool requires 8 calls: about 192 per day. Other applications sharing the API key use the same quota. A quota pause preserves the previous complete snapshot.

No API keys or local database files are committed. See `LITE_CLOUD_README.md` for setup.
