# EtsyPulse Lite Cloud — free route

Lite Cloud does not use a 24/7 VPS. GitHub Actions starts at approximately
every hour at :23 Europe/Istanbul time, reads public Etsy listing data, and
publishes static EtsyPulse to GitHub Pages. GitHub may delay scheduled jobs.
Your PC and the trading VPS
remain untouched.

## Boundaries

- Discovery retains up to 20,000 distinct digital candidates; monitoring
  retains up to 5,000 measured listing IDs with at most 48 samples each.
  Listings unseen for 35 days expire. Existing v1 histories are migrated.
- At most 2,000 IDs are requested per run: up to 1,000 strongest candidates,
  up to 500 new admissions, and remaining slots rotate oldest-request-first.
  When there are fewer new admissions, rotation uses the freed slots. Missing
  responses do not reset measurement times or block the rotation queue.
- Mature non-priority rows may be replaced once the pool is full. Newly
  admitted or migrated rows have a 36-hour grace period. Retired rows retain
  their last summary in discovery while bounded histories leave monitoring.
- Discovery continues with a full monitored pool. Each of the 32 configured
  niches has a relevance lane paging up to 20 pages (100 results/page) and a
  newest-first lane checking page one. Ten search requests run each hour;
  all 64 lanes are visited in roughly seven successful runs. Cursors persist.
  Short/end pages reset only that niche. This is not a complete Etsy index;
  pages can overlap as Etsy ranking changes. API maximum offset is 12,000.
- View changes need two real numeric counters. Missing counters, decreases,
  and missing baselines display `—` rather than a fabricated zero or ratio.
  Older interval metrics are cleared when the listing was not refreshed.
- `View artışı / 24s` is a rate normalised from the displayed comparison
  window, not the difference between the two most recent hourly runs.
- It is a periodic static radar, not the full always-on FastAPI dashboard.
- `state/radar_state.json` contains public observed listing values and is
  committed so the next free run has a comparison baseline.
- API secrets never appear in Git, the static page, logs, or the state file.
- Quantity movement is a public-signal estimate, never a claim about private
  Etsy orders.

## One-time GitHub configuration

1. In **Settings → Secrets and variables → Actions**, create:
   - `ETSY_KEYSTRING`
   - `ETSY_SHARED_SECRET`
2. In **Settings → Pages**, set the source to **GitHub Actions**.
3. Open **Actions → EtsyPulse Cloud → Run workflow** once. Comparisons need
   at least two hours of observations; a full 24-hour baseline is preferred.
4. The GitHub Pages URL appears in the successful deployment details.

## Files

```text
.github/workflows/etsypulse-lite.yml
radar_lite.py
research.py
config/research.json
LITE_CLOUD_README.md
```

## Local verification

```powershell
python -m unittest discover -s tests -v
python radar_lite.py --config config/research.json --state state/radar_state.json --site site
python radar_lite.py --render-only --state state/radar_state.json --site site
```

The collector command requires `ETSY_KEYSTRING` and `ETSY_SHARED_SECRET`
environment variables. Tests never require credentials or call live Etsy.
`--render-only` does not need credentials and makes no network requests.
Edit the JSON configuration, not hardcoded collector loops, to change scope.
Invalid capacities/pagination are rejected before network access.

## Reading and exporting the radar

- **Ölçüm takibi**: sampled listing counters and comparison quality, with
  100-row pages, search/filter/sort, and an unfiltered full monitoring CSV.
- **Keşfedilen adaylar**: all retained candidates, their discovery queries,
  discovery timestamps and monitored/unmonitored status. Its separate CSV
  downloads every retained candidate, not just the displayed page/filter.
- Header counts distinguish configured niches, niches actually visited,
  candidate capacity, tracked capacity, requested rows and fresh observations.
- Public JSON never includes sample history arrays; only current compact
  values are exported. The state uses compact JSON to bound Git file size.

## API quota protection

- Schedule: once per hour, about 24 runs per day.
- Request count: ten searches and up to twenty 100-listing detail batches;
  at most 30 calls/run, about 720 calls/day at hourly cadence. Smaller pools
  use fewer detail calls. All values are configurable with validated bounds.
- A separate app-level 1,000-request rolling daily cap includes manual runs
  and failed attempts, preventing repeated dispatches from bypassing limits.
- Requests are spaced at least 1.1 seconds apart.
- Etsy's live `x-limit-per-day` and `x-remaining-today` headers determine the
  daily reserve: 10% of the limit, with a 25-call minimum. A run stops before
  spending that reserve, keeping all previously completed observations and
  rolling back incomplete search cursors and candidate admissions.
- If Etsy returns 429, collection stops immediately. `Retry-After` is stored;
  later runs do not call Etsy before it expires. There is no retry loop.
- Safe counts and remaining quota appear in `site/data/status.json` and the
  Actions summary output. Credentials are never included.
- If quota headers are missing, the daily quota is reported as unknown. The
  per-run request cap and pacing still apply.
- Faster polling cannot force Etsy's view counter to update. View ratios still
  need a changing counter and sufficient observations.

Official references: [Etsy rate limits](https://developers.etsy.com/documentation/essentials/rate-limits/)
and [Etsy pagination](https://developer.etsy.com/documentation/essentials/urlsyntax/)
and [GitHub scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
