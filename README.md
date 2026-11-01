# health-app

Personal health dashboard. Same system as budget-app: a single-page web app on Cloudflare,
Supabase for data and login, row-level security so only a signed-in user can read anything.

## Data pipeline

`sync/uh_sync.py` pulls Ultrahuman daily metrics (scores, sleep stages, overnight heart rate,
HRV and temperature) and upserts them into Supabase. The GitHub Action in
`.github/workflows/uh-sync.yml` runs it every morning and evening. The first run backfills
everything from 2025-02-01. Later runs only fetch missing days plus the last three, because
the ring syncs late and recent days are not final.

### One-time setup

In this repo: Settings > Secrets and variables > Actions > New repository secret. Add:

| Secret | Value |
|---|---|
| `UH_TOKEN` | Ultrahuman Partner API token (the one HealthSync uses) |
| `UH_EMAIL` | The email on the Ultrahuman account |
| `SUPABASE_URL` | `https://dnwcfrvjqdfledhsvizn.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Supabase > Project Settings > API Keys > secret key (service role) |

Then Actions > Ultrahuman sync > Run workflow.

The service key bypasses row-level security. It lives only in GitHub secrets and must never
go into the app's `index.html`.

## Tables (Supabase project zone2-tracker)

`health_uh_daily`, `health_uh_night`, `health_activities`, `health_activity_streams`.
All have RLS on with one policy: authenticated users have full access.
