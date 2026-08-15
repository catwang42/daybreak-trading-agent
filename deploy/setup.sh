#!/usr/bin/env bash
# deploy/setup.sh — One-shot GCP setup: Cloud Run Job + Scheduler + Secrets + GCS + Vertex access.
# ALL configuration comes from config/.env (gitignored). Nothing personal lives in this file.
# Prereqs: gcloud authed; Vertex Model Garden access approved; config/.env filled.
# Optional overrides: GCP_PROJECT (defaults to VERTEXAI_PROJECT), GCP_REGION (default us-central1),
#                     JOB_SCHEDULE (default "0 8 * * 1-5"), JOB_TIMEZONE (default America/New_York).
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
# degrades the matching source rather than failing. SEC_USER_AGENT is a contact
# address rather than a credential, but it goes through Secret Manager anyway so
# it stays out of the job's readable env metadata. There are no REDDIT_* entries:
# that API application was rejected and no social source replaced it.
SECRET_NAMES=(ALPACA_API_KEY ALPACA_SECRET_KEY FINNHUB_API_KEY FRED_API_KEY \
              SEC_USER_AGENT \
              TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID)
for S in "${SECRET_NAMES[@]}"; do push_secret "$S"; done

SET_SECRETS=""
for S in "${SECRET_NAMES[@]}"; do
  [ -n "${!S:-}" ] && SET_SECRETS+="${SET_SECRETS:+,}${S}=${S}:latest"
done

# --- Build & push image --------------------------------------------------------
gcloud builds submit --tag "$IMAGE" .

# --- Cloud Run Job: non-secret config as env; container auths to Vertex via SA --
gcloud run jobs create "$JOB" --image "$IMAGE" --region "$REGION" \
  --service-account "$SA" --task-timeout=30m --memory=1Gi \
  --set-env-vars "REPORTS_BUCKET=${BUCKET},ALPACA_PAPER=true,VERTEXAI_PROJECT=${VERTEXAI_PROJECT},VERTEXAI_LOCATION=${VERTEXAI_LOCATION},LLM_FAST_MODEL=${LLM_FAST_MODEL},LLM_SMART_MODEL=${LLM_SMART_MODEL},LLM_DEEP_MODEL=${LLM_DEEP_MODEL},DEEP_TICKER_CAP=${DEEP_TICKER_CAP:-5},DEBATE_ROUNDS=${DEBATE_ROUNDS:-1}" \
  --set-secrets "$SET_SECRETS" \
  || gcloud run jobs update "$JOB" --image "$IMAGE" --region "$REGION"

# --- Scheduler trigger ---------------------------------------------------------
gcloud scheduler jobs create http "${JOB}-trigger" --location "$REGION" \
  --schedule "$SCHEDULE" --time-zone "$TIMEZONE" \
  --uri "https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}:run" \
  --http-method POST --oauth-service-account-email "$SA" || true

echo "Done. Test run: gcloud run jobs execute $JOB --region $REGION --wait"
