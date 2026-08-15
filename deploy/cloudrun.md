# Deploy — Cloud Run Jobs (recommended)

Why Jobs (not a Service): the agent is a scheduled batch task — run, write report, send the email, exit. Jobs scale to zero (pay only for run minutes), have no HTTP surface to secure, and pair natively with Cloud Scheduler. A ~15-min daily run on 1 vCPU/1GiB costs well under $1/month; Scheduler and Artifact Registry pennies; GCS pennies. LLM tokens are the real cost.

Architecture: Cloud Scheduler (cron, America/New_York) → Cloud Run Job (this container) → Secret Manager (keys) → GCS bucket (reports/ + journal/) → SMTP → your inbox.

## One-time setup (`bash deploy/setup.sh`, or paste the blocks manually)
1. Project + APIs: run.googleapis.com, cloudscheduler.googleapis.com, artifactregistry.googleapis.com, secretmanager.googleapis.com, cloudbuild.googleapis.com, storage.googleapis.com (+ aiplatform.googleapis.com if using Vertex AI models).
2. Artifact Registry docker repo; build & push with Cloud Build.
3. Secrets: one Secret Manager entry per credential in config/.env.example (Alpaca, Finnhub, FRED, SEC contact, `SMTP_USER`/`SMTP_APP_PASSWORD`/`SMTP_FROM`/`REPORT_EMAIL_TO`). `SMTP_HOST`/`SMTP_PORT` are plain config and go in as env vars.
4. GCS bucket for reports/journal; service account with roles/secretmanager.secretAccessor + roles/storage.objectAdmin (+ roles/aiplatform.user for Vertex) **and roles/run.invoker on the job**, which is what lets Scheduler trigger it.
5. Create the Job with `--set-secrets` for every credential and `REPORTS_BUCKET` env.
6. Scheduler: e.g. `0 8 * * 1-5` America/New_York (pre-market) triggering the Job run endpoint via OAuth service account.

## Each code change
`gcloud builds submit --tag "$IMAGE" .` → `gcloud run jobs deploy ta-daily --image "$IMAGE" ...` (or just re-run `deploy/setup.sh`, which is idempotent).
Manual test run: `gcloud run jobs execute ta-daily --region "$REGION" --wait`
Logs: Cloud Logging, filter `resource.type=cloud_run_job`.

## Notes
- Job timeout: set 30m (default 10m may be tight for 5 deep tickers).
- `--max-retries=0`, deliberately. A failed task has usually already spent its LLM tokens, and the default of 3 would spend them twice more. Reports and journal reach GCS before the email is sent, so recovering from a delivery failure is `--stage report` (no market data, no LLM calls), not a full re-run.
- Deploy with `gcloud run jobs deploy`, not `create`. `create` fails once the job exists, and falling back to `update --image` silently leaves stale secrets and env vars in place — exactly the failure you would not notice, because the job keeps succeeding with the old address.
- Persistence: the container's disk is discarded when the task exits. The journal is restored from `gs://<bucket>/journal/journal.jsonl` at startup and mirrored back afterwards, because the source-accuracy tracker grades signals against weeks of it. Without the round trip every source looks permanently untested.
- Gmail as the SMTP relay needs 2-Step Verification on and a 16-character **App Password**; the account password is refused. Port 587 = STARTTLS, 465 = implicit TLS.
- Using Claude via Vertex AI keeps LLM billing inside this GCP project (enable the models in Model Garden, set VERTEXAI_PROJECT/LOCATION, use `vertex_ai/...` model strings in env).
- Verify current gcloud syntax with `gcloud run jobs deploy --help`; commands drift. Flags used here were checked against SDK 501.0.0.
