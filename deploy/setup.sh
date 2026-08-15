#!/usr/bin/env bash
# deploy/setup.sh — One-shot GCP setup: Cloud Run Job + Scheduler + Secrets + GCS + Vertex access.
# ALL configuration comes from config/.env (gitignored). Nothing personal lives in this file.
# Prereqs: gcloud authed; Vertex Model Garden access approved; config/.env filled.
# Optional overrides: GCP_PROJECT (defaults to VERTEXAI_PROJECT), GCP_REGION (default us-central1),
#                     JOB_SCHEDULE (default "0 8 * * 1-5"), JOB_TIMEZONE (default America/New_York).
# Idempotent: safe to re-run after changing config/.env or the code.
set -euo pipefail

# --- Load config/.env ---------------------------------------------------------
ENV_FILE="$(dirname "$0")/../config/.env"
[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found. Copy config/.env.example and fill it."; exit 1; }
set -a; source "$ENV_FILE"; set +a

# --- Validate required values -------------------------------------------------
for VAR in VERTEXAI_PROJECT VERTEXAI_LOCATION LLM_FAST_MODEL LLM_SMART_MODEL LLM_DEEP_MODEL \
           ALPACA_API_KEY ALPACA_SECRET_KEY FINNHUB_API_KEY; do
  [ -n "${!VAR:-}" ] || { echo "ERROR: $VAR is empty in config/.env"; exit 1; }
done
[ "${ALPACA_PAPER:-}" = "true" ] || { echo "ERROR: ALPACA_PAPER must be true (research guardrail)"; exit 1; }
# Delivery is optional by design — the app reports it as skipped and still writes
# the reports. Warn rather than fail, because a scheduled job nobody reads is a
# quieter failure than a setup script that stops.
[ -n "${SMTP_HOST:-}" ] && [ -n "${REPORT_EMAIL_TO:-}" ] \
  || echo "WARNING: SMTP_HOST/REPORT_EMAIL_TO unset — the job will run but email nobody."

# --- Derived settings ---------------------------------------------------------
PROJECT_ID="${GCP_PROJECT:-$VERTEXAI_PROJECT}"
REGION="${GCP_REGION:-us-central1}"
REPO="trading-agent"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/agent:latest"
JOB="ta-daily"
BUCKET="gs://${PROJECT_ID}-trading-reports"
SA_NAME="ta-runner"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULE="${JOB_SCHEDULE:-0 8 * * 1-5}"
TIMEZONE="${JOB_TIMEZONE:-America/New_York}"

gcloud config set project "$PROJECT_ID"

# --- APIs ---------------------------------------------------------------------
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com storage.googleapis.com aiplatform.googleapis.com

# --- Infra --------------------------------------------------------------------
gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION" || true
gcloud storage buckets create "$BUCKET" --location="$REGION" || true
gcloud iam service-accounts create "$SA_NAME" --display-name="trading agent job" || true

# --- Permissions: GCS (reports/journal) + Vertex (invoke Claude) ---------------
gcloud storage buckets add-iam-policy-binding "$BUCKET" \
  --member="serviceAccount:${SA}" --role=roles/storage.objectAdmin
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}" --role=roles/aiplatform.user

# --- Secrets: pushed from config/.env values; empty ones skipped ---------------
push_secret () {
  local NAME="$1" VALUE="${!1:-}"
  [ -n "$VALUE" ] || { echo "skip secret $NAME (empty)"; return 0; }
  printf "%s" "$VALUE" | gcloud secrets create "$NAME" --data-file=- 2>/dev/null \
    || printf "%s" "$VALUE" | gcloud secrets versions add "$NAME" --data-file=-
  gcloud secrets add-iam-policy-binding "$NAME" \
    --member="serviceAccount:${SA}" --role=roles/secretmanager.secretAccessor
}
# Every name here is optional — push_secret skips the empty ones and the app
# degrades the matching source rather than failing. Two of these are not
# credentials but personal data: SEC_USER_AGENT and REPORT_EMAIL_TO are email
# addresses, and Secret Manager keeps them out of the job's world-readable env
# metadata. SMTP_HOST/SMTP_PORT are plain config and go in as env vars below.
# There are no REDDIT_* or TELEGRAM_* entries: the Reddit API application was
# rejected and no social source replaced it, and delivery is email.
SECRET_NAMES=(ALPACA_API_KEY ALPACA_SECRET_KEY FINNHUB_API_KEY FRED_API_KEY \
              SEC_USER_AGENT \
              SMTP_USER SMTP_APP_PASSWORD SMTP_FROM REPORT_EMAIL_TO)
for S in "${SECRET_NAMES[@]}"; do push_secret "$S"; done

SET_SECRETS=""
for S in "${SECRET_NAMES[@]}"; do
  [ -n "${!S:-}" ] && SET_SECRETS+="${SET_SECRETS:+,}${S}=${S}:latest"
done

# --- Build & push image --------------------------------------------------------
gcloud builds submit --tag "$IMAGE" .

# --- Cloud Run Job: non-secret config as env; container auths to Vertex via SA --
# `deploy` rather than `create || update`: create fails on the second run, and
# the update fallback only swapped the image — so a changed secret or env var
# silently did not reach the job. deploy is create-or-update with the full spec.
#
# --max-retries=0 is a cost guardrail, not an oversight. A failed run has
# usually already spent its tokens; Cloud Run's default of 3 would spend them
# twice more. Reports and journal are mirrored to GCS before delivery, so the
# cheap recovery from a delivery failure is `--stage report`, not a full re-run.
ENV_VARS="REPORTS_BUCKET=${BUCKET},ALPACA_PAPER=true"
ENV_VARS+=",VERTEXAI_PROJECT=${VERTEXAI_PROJECT},VERTEXAI_LOCATION=${VERTEXAI_LOCATION}"
ENV_VARS+=",LLM_FAST_MODEL=${LLM_FAST_MODEL},LLM_SMART_MODEL=${LLM_SMART_MODEL},LLM_DEEP_MODEL=${LLM_DEEP_MODEL}"
ENV_VARS+=",DEEP_TICKER_CAP=${DEEP_TICKER_CAP:-5},DEBATE_ROUNDS=${DEBATE_ROUNDS:-1}"
ENV_VARS+=",SMTP_HOST=${SMTP_HOST:-smtp.gmail.com},SMTP_PORT=${SMTP_PORT:-587}"

gcloud run jobs deploy "$JOB" --image "$IMAGE" --region "$REGION" \
  --service-account "$SA" --task-timeout=30m --memory=1Gi --max-retries=0 \
  --set-env-vars "$ENV_VARS" \
  ${SET_SECRETS:+--set-secrets "$SET_SECRETS"}

# --- Scheduler trigger ---------------------------------------------------------
# The scheduler authenticates as the same SA, so it needs permission to invoke
# the job it triggers. Without this the cron fires and gets a silent 403.
gcloud run jobs add-iam-policy-binding "$JOB" --region "$REGION" \
  --member="serviceAccount:${SA}" --role=roles/run.invoker

TRIGGER_URI="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}:run"
gcloud scheduler jobs create http "${JOB}-trigger" --location "$REGION" \
  --schedule "$SCHEDULE" --time-zone "$TIMEZONE" \
  --uri "$TRIGGER_URI" \
  --http-method POST --oauth-service-account-email "$SA" \
  || gcloud scheduler jobs update http "${JOB}-trigger" --location "$REGION" \
       --schedule "$SCHEDULE" --time-zone "$TIMEZONE" \
       --uri "$TRIGGER_URI" \
       --http-method POST --oauth-service-account-email "$SA"

echo "Done. Test run: gcloud run jobs execute $JOB --region $REGION --wait"
