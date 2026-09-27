# EtsyPulse Lite Cloud — free route

Lite Cloud does not use a 24/7 VPS. GitHub Actions starts at approximately
03:23 and 15:23 Europe/Istanbul time, reads public Etsy listing data, and
publishes a static Market Radar to GitHub Pages. Your PC and the trading VPS
remain untouched.

## Boundaries

- It tracks a capped 400-listing pool and holds compact 35-day observations.
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
3. Open **Actions → EtsyPulse Lite Cloud → Run workflow** once. The first run
   creates the baseline; the next run produces comparable movement estimates.
4. The GitHub Pages URL appears in the successful deployment details.

## Files

```text
.github/workflows/etsypulse-lite.yml
radar_lite.py
LITE_CLOUD_README.md
```

## Local verification

```powershell
python -m unittest tests.test_radar_lite
```
