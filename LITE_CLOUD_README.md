# EtsyPulse Lite Cloud — free route

Lite Cloud does not use a 24/7 VPS. GitHub Actions starts at approximately
every hour at :23 Europe/Istanbul time, reads public Etsy listing data, and
publishes static EtsyPulse to GitHub Pages. GitHub may delay scheduled jobs.
Your PC and the trading VPS
remain untouched.

## Boundaries

- It tracks a capped 400-listing pool, retaining at most 75 observations per
  listing (roughly three days at hourly cadence) and removing listings unseen
  for 35 days. The 24-hour comparison fits within this bounded history.
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
LITE_CLOUD_README.md
```

## Local verification

```powershell
python -m unittest discover -s tests -v
```

## API quota protection

- Schedule: once per hour, about 24 runs per day.
- Current request count: four searches and up to two listing-detail batches;
  normally six calls per run (144/day). A hard cap of eight calls per run
  allows for pool growth (192/day maximum, excluding other applications).
- Requests are spaced at least 1.1 seconds apart.
- Etsy's live `x-limit-per-day` and `x-remaining-today` headers determine the
  daily reserve: 10% of the limit, with a 25-call minimum. A run stops before
  spending that reserve, keeping all previously completed observations.
- If Etsy returns 429, collection stops immediately. `Retry-After` is stored;
  later runs do not call Etsy before it expires. There is no retry loop.
- Safe counts and remaining quota appear in `site/data/status.json` and the
  Actions summary output. Credentials are never included.
- If quota headers are missing, the daily quota is reported as unknown. The
  per-run request cap and pacing still apply.
- Faster polling cannot force Etsy's view counter to update. View ratios still
  need a changing counter and sufficient observations.

Official references: [Etsy rate limits](https://developers.etsy.com/documentation/essentials/rate-limits/)
and [GitHub scheduled workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule).
