# Health Dashboard

Daily health dashboard powered by Garmin Connect + Withings BPM Connect.

**Live dashboard:** https://hutcheew.github.io/health-dashboard

## What it shows
- Training readiness, HRV, sleep score, body battery
- Last run breakdown with GCT balance (left/right) per km
- GCT balance trend across recent runs
- Weekly mileage
- Blood pressure history (Withings BPM Connect)

## How it works
- GitHub Actions runs daily at 7am Melbourne time
- Fetches data from Garmin Connect + Withings APIs
- Generates index.html and commits to this repo
- GitHub Pages serves it publicly

## Manual trigger
Go to Actions tab > Update Health Dashboard > Run workflow