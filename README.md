# job-fetcher-setareh

Daily fetcher for Setareh's Claude job digest. Copied from [sinazar/job-fetcher](https://github.com/sinazar/job-fetcher) and retuned for coordinator / specialist roles in marketing, marketing operations, CRM, client onboarding, operations and project coordination.

**How it works**
1. GitHub Actions runs `fetch_jobs.py` at about 1:31 AM Toronto, 30 minutes after Sina's fetcher. Backup slots later in the morning cover GitHub's late starts; only the first run of the day does any work.
2. It pulls recently posted roles from JSearch (LinkedIn, Indeed, Glassdoor and company sites via Google for Jobs), We Work Remotely (sales & marketing and customer support feeds), Built In Toronto (marketing and operations, hybrid and in-office) and, optionally, Adzuna. RemoteOK is turned off because its free feed can't be narrowed to these roles.
3. It applies cheap keyword filters (title, seniority, GTA or Canada-remote), skips anything it has already seen, and commits the results (see **Data files** below).
4. At about 9:45 AM Toronto a Claude scheduled task ("Setareh daily job digest") reads every `data/daily/YYYY-MM-DD.json` from the last 3 days, skips job IDs already in the "Setareh Job Tracker" Google Sheet, scores the rest against her master resume, adds roles scoring 60+ to the tracker and emails her the digest.

No AI calls happen in this repo.

## Data files
Nothing in this list is ever deleted or overwritten, except the two "latest" convenience copies.

| Path | What it is |
|---|---|
| `data/runs/YYYY-MM-DDTHHMMSSZ.json` | One file per run, written once and never changed: the new roles that run found, with source counts and diagnostics. |
| `data/raw/YYYY-MM-DDTHHMMSSZ.json.gz` | The raw JSearch responses for that run, gzipped. |
| `data/daily/YYYY-MM-DD.json` | Every run from that day combined. The Claude digest reads these. |
| `data/latest.json` | A copy of the most recent run only. Overwritten each run. |
| `data/raw_jsearch_latest.json` | The most recent raw JSearch responses, used by `--replay`. Overwritten each run. |
| `data/seen_jobs.json` | IDs already pulled, so a role is reported only once. Pruned after 60 days. |

## Setup
1. RapidAPI key with a **JSearch** subscription (free BASIC plan, 200 requests/month), saved as the `RAPIDAPI_KEY` repository secret.
2. Optional: free Adzuna key from developer.adzuna.com → add `ADZUNA_APP_ID` and `ADZUNA_APP_KEY` secrets. If they're missing, Adzuna is skipped.
3. **Actions** tab → "Fetch jobs" → **Run workflow** to test. A `data/` commit should appear within a minute or two.

## Tuning
Everything is in `config.py`: JSearch queries, title include and exclude rules, GTA cities, allowed remote regions, Built In searches and We Work Remotely feeds. Keep the filters loose; Claude does the real scoring.

Quota: 4 JSearch queries × 1 page = 4 requests/day ≈ 120 of the 200 free monthly requests.

## Run locally
```
pip install -r requirements.txt
RAPIDAPI_KEY=... python fetch_jobs.py
```

## Tune filters without spending API credits
After editing `config.py`, run `python fetch_jobs.py --replay`. It re-applies the filters to the last saved JSearch responses with no JSearch calls and writes `data/replay.json`.
