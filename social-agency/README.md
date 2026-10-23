# AI Social Media Agency

A self-hosted platform that runs Instagram creator and business accounts the way an agency would. You drop photos and videos into a folder. The agent analyses them, edits them, writes the copy, decides whether a human needs to look, schedules the post, publishes it through Meta's official API, confirms the post went live, pulls the metrics, and feeds the results into the next plan.

```
Upload → analyse (Claude vision) → edit (Pillow/FFmpeg) → copy + hashtags (Claude) → QC + risk check
      → approval routing (Mode 1/2/3) → schedule (best slot) → publish (Instagram API) → verify
      → insights sync → growth analysis / experiments → next week's plan and next month's strategy
```

## Architecture and trade-offs

| Component | Choice | Why |
|---|---|---|
| Backend API | Python 3.11+, FastAPI | The media work (Pillow, NumPy, FFmpeg) and the Claude SDK are both Python, so there is one language and one process model. |
| Database | SQLAlchemy 2. SQLite by default, PostgreSQL in production (`DATABASE_URL`) | Zero setup for one person. Postgres when you need concurrent workers or managed backups. |
| Job queue | Database-backed (`agency/jobs.py`) with idempotency keys, exponential backoff, a dead-letter state, and crash recovery | Saves running Redis/Celery for a workload of tens of jobs per hour. Jobs survive restarts. |
| Worker | `agency/worker.py` polling loop: folder scan → scheduler → periodic syncs/reports → jobs | Polling works with every sync client (Google Drive, Dropbox, OneDrive) and with network drives. |
| Media | Pillow + NumPy (images), FFmpeg (video) | Corrections are measured and deterministic, with no generative edits to faces, bodies or products. |
| AI | Claude via the official `anthropic` SDK, structured outputs, server-side refusal fallback | Vision analysis, copy, audits, plans, comment triage. Results are cached by content hash. Spend is tracked and capped. |
| Instagram | Instagram API with Instagram Login (`graph.instagram.com`) | Official OAuth, publishing, insights and comments for Business and Creator accounts. No passwords, no scraping. |
| Dashboard | Static single-page app (vanilla JS + Chart.js) served by FastAPI | No build step, which keeps it simple to operate. |
| Reports | JSON snapshots, CSV export, PDF export (ReportLab) | |

Security: Fernet-encrypted OAuth tokens, scrypt password hashes, signed session cookies, owner/editor/viewer roles, CSRF header check, signed time-limited public media URLs, secrets scrubbed from every log, audit entry and error message, and EXIF/GPS stripped from exports.

## Quick start (local)

```bash
cd social-agency
pip install -r requirements.txt        # ffmpeg must also be installed (apt install ffmpeg / brew install ffmpeg)
cp .env.example .env                   # fill in what you have; everything is optional for a first look
python run.py                          # dashboard on http://localhost:8000 + background worker
python -m pytest                       # 19 end-to-end tests, all offline (fake Graph API and fake Claude)
```

On the first visit, create the owner login. Then go to **Integrations → Add an account** for each Instagram account. Each account gets its own `Social Media/<Account>/Inbox` folder inside `DATA_DIR`, or inside `WATCH_ROOT` if you set it.

## What you need to do to go live

1. **Meta app** (about 30 minutes, needs your Facebook developer login):
   1. Go to developers.facebook.com → Create app → type *Business* → add the **Instagram** product → **API setup with Instagram login**.
   2. Under *Business login settings*, add the OAuth redirect URI shown on the Integrations page: `https://<your-domain>/api/integrations/instagram/callback`.
   3. Copy the Instagram app ID and secret into `.env` as `META_APP_ID` and `META_APP_SECRET`.
   4. Permissions used: `instagram_business_basic`, `instagram_business_content_publish`, `instagram_business_manage_insights`, `instagram_business_manage_comments`. While the app is in development mode, add your own Instagram accounts as testers (App roles → Instagram testers) and accept the invites in Instagram. Public use beyond your own accounts needs Meta App Review.
   5. Each Instagram account must be a **Professional** account (Creator or Business).
2. **Public HTTPS address.** Instagram downloads the media from your server when it publishes. Deploy behind HTTPS (Caddy, nginx, Fly.io, Render, a VPS) and set `PUBLIC_BASE_URL`. For testing, a tunnel such as `cloudflared tunnel --url http://localhost:8000` works.
3. **Claude API key.** Set `ANTHROPIC_API_KEY`. Without it everything still runs on heuristics, but no post can auto-publish.
4. **Folder sync.** Point the Google Drive, Dropbox or OneDrive desktop app at `WATCH_ROOT`, or set `WATCH_ROOT` to the folder it already syncs.
5. **Production secrets.** Set `APP_ENV=production`, `SECRET_KEY` and `TOKEN_ENCRYPTION_KEY`. Generate the key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.

## Onboarding questions (answer once per account in Brand Settings)

1. Instagram username, and whether it is a creator or business account
2. Category and niche
3. Products, services, offers or personal-brand positioning
4. Target audience: countries, cities, age groups
5. Primary objectives (followers, reach, engagement, traffic, leads, sales, awareness)
6. Brand voice, colours, fonts, creative references
7. Competitors and inspirational accounts
8. Website, landing pages, preferred calls to action
9. Posts per week, and what content you have available
10. Markets, languages, business priorities

Also decide per account: the autonomy mode, and whether everything you upload is your own content. The second one sets the `uploads_are_owned` rule, which confirms content rights.

## Folder convention

```
Social Media/
  Creator Account/Inbox/              → assigned to that account
  Creator Account/Inbox/Paris Trip/   → sub-folder = campaign; its photos become one carousel, its clips one Reel
  Creator Account/Approved/           → same account, rights pre-confirmed
  Business Account/Inbox/
  Shared Assets/                      → review queue unless tagged
  Brand Assets/                       → never published
  Published/  Failed/                 → copies of final exports, written by the agent
```

You can also assign an account with a filename tag (`@business-account__photo.jpg`) or a sidecar file (`photo.jpg.json` containing `{"account": "business-account", "rights_confirmed": true}`). Conflicting signals send the asset to the review queue. Files must be unchanged across two scans before they are processed. `.part`, `.crdownload` and zero-byte files are ignored. Duplicates are detected by SHA-256. Originals are never modified, moved or deleted.

## Autonomy modes

| Mode | Behaviour |
|---|---|
| 1 Approval required | Everything is prepared. You approve each post. |
| 2 Limited autonomy | Posts in allowed formats and pillars that pass QC publish automatically. Everything else waits for you. |
| 3 Full automation | Every eligible post publishes automatically, except formats you block. |

In **every** mode these always go to a human: AI-unavailable or fallback copy, failed QC, a QC score below the threshold, claims, promotions or sensitive topics in the copy, sensitive visual flags, unconfirmed rights, uncertain account assignment, and assets without an AI visual review. The **emergency pause** (top bar) stops the scheduler, cancels queued publish jobs, and is re-checked right before the final `media_publish` call.

## Acceptance checklist

| Item | Status |
|---|---|
| Connect authorised Instagram accounts | **Implemented and tested against a fake Graph API** (OAuth, state anti-replay, long-lived token, refresh, encrypted storage). **Blocked on your Meta app credentials** for a live test. |
| Upload media to a designated folder | **Implemented and tested** (folder, filename tag, sidecar, dashboard drag-and-drop upload, incomplete-upload wait, dedupe, corrupt files). |
| Detect and classify new assets | **Implemented and tested.** Claude vision classification is tested with a fake client. Live use needs `ANTHROPIC_API_KEY`. Heuristic fallback is tested. |
| Usable image and video edits | **Implemented and tested** (measured corrections, Instagram ratios, 1080 px JPEG, 1080×1920 H.264/AAC Reels, dead-time trim, loudness normalisation, cover selection, blurred fill). Automatic subtitles are **partially supported**: they need the optional `faster-whisper`, and Reels without subtitles are flagged. Mid-clip pause removal and multi-clip narrative editing beyond concatenation are not implemented. |
| Brand-aligned captions, keywords, hashtags | **Implemented and tested with a fake Claude client** (2–3 angles, selection reason, alt text, cover title, claims to verify). Copy quality has not been evaluated with a real key. |
| Weekly and monthly content calendar | **Implemented and tested** (best times from your own history, falling back to labelled defaults; pillar balancing adjusted by measured performance; content-gap slots; AI monthly strategy with a rule-based fallback). |
| Schedule and publish eligible content | **Implemented and tested against a fake Graph API** (image, carousel, Reel with processing polls, publishing-limit check, duplicate-caption check, conflict check). **Blocked on Meta credentials and a public HTTPS URL** for live publishing. |
| Verify publishing outcomes and handle failures | **Implemented and tested**: a post is marked published only after the media is read back; rate-limit retries reuse the same container; permanent failures go to the failed queue with alerts; recovery covers "published but not recorded". |
| Dashboard shows actual, timestamped data | **Implemented.** Every value comes from synced snapshots with freshness timestamps. Empty states are honest. |
| Audience demographics and geography | **Implemented and tested** (`follower_demographics` and `engaged_audience_demographics` by country, city, age and gender, as Instagram reports them). Instagram withholds these below 100 followers or engagements. |
| Unavailable metrics explicitly identified | **Implemented and tested** (per-metric fallback, an availability table, "Data unavailable from connected source"; impressions is marked retired in v22). |
| Weekly and monthly reports, exportable | **Implemented and tested** (CSV, PDF, automatic Monday and 1st-of-month generation). |
| Growth engine uses actual results | **Implemented**: baselines per format, relative reach, hook-style analysis, bounded pillar re-weighting, learning context passed to copy generation, experiment register with evaluation. Facts and hypotheses are kept separate. It becomes useful once there is history; thresholds require at least 3 posts. |
| Security, retries, logs, emergency pause | **Implemented and tested** (pause, rate-limit retry, permanent failure, account isolation, CSRF, roles, secret scrubbing). Not yet externally security-audited. |
| Community management | **Implemented and tested** (comment sync, rule + AI triage, escalation, lead alerts, template auto-reply limited to praise/questions and never in Mode 1). DMs are **not implemented**: they need the messaging permission and App Review. |
| Competitor intelligence | **Partially supported.** Competitor metrics need `business_discovery`, which the Facebook-Login variant of the API exposes but the Instagram-Login variant does not. Listed competitors are used as qualitative context only. |
| Business outcomes | **Partially supported**: CSV import of GA4 or other analytics with a mandatory attribution-method note. There is no direct GA4 or Shopify connector yet. |

## Project layout

```
agency/
  config.py        settings, pricing, secret bootstrap      db.py / models.py   schema
  ingest.py        folder watcher                           media_probe.py      metadata
  analysis.py      measured stats + Claude vision           editing.py          image/video pipelines
  captions.py      copy engine                              qc.py               quality + risk checks
  publishing.py    approval routing, scheduling, publish    planner.py          weekly/monthly planning
  instagram.py     Graph API client                         connections.py      tokens
  analytics.py     sync + aggregation + definitions         growth.py           insights, experiments
  audit_report.py  profile audit + health score             community.py        comments
  reports.py       weekly/monthly + CSV/PDF                 ai.py               Claude wrapper, cache, budget
  jobs.py          queue                                    worker.py           background loop
  api/app.py       REST API + OAuth callback + dashboard    web/                dashboard
tests/             end-to-end tests with a fake Graph API and a fake Claude client
```

## Operations

- **Health:** `GET /healthz`. The System Health page shows the worker heartbeat, queue, failed jobs (with retry), storage, AI spend per workflow and account, and the audit trail.
- **Backups:** with SQLite, back up `DATA_DIR` (database, `.secrets`, `storage/`) every night, e.g. `sqlite3 data/agency.db ".backup data/backup.db"` plus `rsync`. With Postgres, use `pg_dump` or managed snapshots, and back up `storage/` separately. **Keep `TOKEN_ENCRYPTION_KEY` safe**: without it the stored tokens cannot be decrypted, and you would have to reconnect every account.
- **Recovery:** jobs are idempotent. A crashed worker's jobs are re-queued after 15 minutes. Failed posts can be retried from the queue. Publishing reuses an existing container and checks Instagram before publishing again.
- **Schema changes:** add numbered statements to `MIGRATIONS` in `agency/db.py`. For larger Postgres deployments, move to Alembic.
- **Cost control:** analysis is cached per file hash, images are downscaled before upload to Claude, and `AI_MONTHLY_BUDGET_USD` is a hard stop (the pipeline degrades to heuristics plus human review). `AI_MODEL_LIGHT` lets you put comment triage on a cheaper model.
- **Alerts:** set `ALERT_WEBHOOK_URL` (e.g. a Slack incoming webhook) to receive warnings and errors.
