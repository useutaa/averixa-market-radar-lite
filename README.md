# Averixa Market Radar Lite

Free, scheduled Etsy market research dashboard. GitHub Actions collects a compact public-market snapshot every hour at :23 and deploys the static dashboard to GitHub Pages. Runs may be delayed by GitHub.

Discovery and measured monitoring are separate. The default configuration rotates through 32 digital search niches, paginates relevance results, and regularly checks newest listings. Up to 20,000 unique candidates are retained independently of a 5,000-listing monitoring pool. Searches continue even when monitoring is full. Stronger measured candidates are requested frequently; the other IDs are refreshed oldest-attempt-first. New candidates receive at least 36 hours to develop a comparison baseline before they can be replaced.

The default run uses 10 search requests and up to 20 detail batches (2,000 IDs), capped at 30 requests: about 720 requests per scheduled day. A separate rolling 24-hour app budget caps scheduled **and manual** runs at 1,000 requests. Etsy's live quota headers, a 10% reserve (at least 25 calls), 1.1-second pacing and persisted Retry-After remain authoritative. Other applications share the same Etsy key quota. A quota pause rolls back discovery cursors, candidate admissions and incomplete observations together.

The dashboard has separate **Ölçüm takibi** and **Keşfedilen adaylar** tabs with pagination and full-pool CSV downloads. Candidate search counters are not sold-item counts or valid sales/view comparisons. Missing or stale measurements are shown explicitly, not as fabricated zero values. Existing v1 observations migrate without resetting the baseline.

Edit `config/research.json` to change niches and bounded capacities. This is a scoped public-market research sample, **not a complete mirror of every digital Etsy listing**. Search ranking changes and API pagination limits prevent such a completeness claim.

No API keys or local database files are committed. See `LITE_CLOUD_README.md` for setup.
