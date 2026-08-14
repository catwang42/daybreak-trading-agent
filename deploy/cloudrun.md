# Deploy — Cloud Run Jobs (recommended)

Why Jobs (not a Service): the agent is a scheduled batch task — run, write report, send Telegram, exit. Jobs scale to zero (pay only for run minutes), have no HTTP surface to secure, and pair natively with Cloud Scheduler. A ~15-min daily run on 1 vCPU/1GiB costs well under $1/month; Scheduler and Artifact Registry pennies; GCS pennies. LLM tokens are the real cost.

Architecture: Cloud Scheduler (cron, America/New_York) → Cloud Run Job (this container) → Secret Manager (keys) → GCS bucket (reports/ + journal/) → Telegram.

## One-time setup (fill variables, run deploy/setup.sh, or paste blocks manually)
1. Project + APIs: run.googleapis.com, cloudscheduler.googleapis.com, artifactregistry.googleapis.com, secretmanager.googleapis.com, cloudbuild.googleapis.com, storage.googleapis.com (+ aiplatform.googleapis.com if using Vertex AI models).
2. Artifact Registry docker repo; build & push with Cloud Build.
3. Secrets: one Secret Manager entry per key in config/.env.example (LLM, Alpaca, Finnhub, Telegram...).
4. GCS bucket for reports/journal; service account with roles/secretmanager.secretAccessor + roles/storage.objectAdmin (+ roles/aiplatform.user for Vertex).
5. Create the Job with --set-secrets for every key and REPORTS_BUCKET env.
6. Scheduler: e.g. `0 8 * * 1-5` America/New_York (pre-market) triggering the Job run endpoint via OAuth service account.

## Each code change
gcloud builds submit → gcloud run jobs update ta-daily --image ...
Manual test run: gcloud run jobs execute ta-daily --wait
Logs: Cloud Logging, filter resource.type=cloud_run_job

## Notes
- Job timeout: set 30m (default 10m may be tight for 5 deep tickers).
- Using Claude via Vertex AI keeps LLM billing inside this GCP project (enable the models in Model Garden, set VERTEXAI_PROJECT/LOCATION, use vertex_ai/... model strings in env).
- Verify current gcloud syntax with `gcloud run jobs create --help`; commands drift.
