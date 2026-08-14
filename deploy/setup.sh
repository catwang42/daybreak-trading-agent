#!/usr/bin/env bash
# One-shot GCP setup for the daily job. FILL VARIABLES, review each block, run once.
# Claude Code: verify current gcloud flags before running; syntax drifts.
set -euo pipefail
PROJECT_ID="your-project"
REGION="us-central1"            # or asia-southeast1 (Singapore)
REPO="trading-agent"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/agent:latest"
JOB="ta-daily"
BUCKET="gs://${PROJECT_ID}-trading-reports"
SA="ta-runner@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULE="0 8 * * 1-5"          # weekdays 08:00 America/New_York (pre-market)

gcloud config set project "$PROJECT_ID"
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  artifactregistry.googleapis.com secretmanager.googleapis.com \
  cloudbuild.googleapis.com storage.googleapis.com

gcloud artifacts repositories create "$REPO" --repository-format=docker --location="$REGION" || true
gcloud storage buckets create "$BUCKET" --location="$REGION" || true
gcloud iam service-accounts create ta-runner --display-name="trading agent job" || true
gcloud storage buckets add-iam-policy-binding "$BUCKET" --member="serviceAccount:${SA}" --role=roles/storage.objectAdmin

# Secrets: repeat for every key in config/.env.example (values read interactively)
for S in LLM_FAST_MODEL LLM_SMART_MODEL ANTHROPIC_API_KEY ALPACA_API_KEY ALPACA_SECRET_KEY FINNHUB_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID; do
  printf "Enter value for %s: " "$S"; read -r V
  printf "%s" "$V" | gcloud secrets create "$S" --data-file=- 2>/dev/null || printf "%s" "$V" | gcloud secrets versions add "$S" --data-file=-
  gcloud secrets add-iam-policy-binding "$S" --member="serviceAccount:${SA}" --role=roles/secretmanager.secretAccessor
done

gcloud builds submit --tag "$IMAGE" .

gcloud run jobs create "$JOB" --image "$IMAGE" --region "$REGION" \
  --service-account "$SA" --task-timeout=30m --memory=1Gi \
  --set-env-vars "REPORTS_BUCKET=${BUCKET},ALPACA_PAPER=true" \
  --set-secrets "$(printf 'LLM_FAST_MODEL=LLM_FAST_MODEL:latest,LLM_SMART_MODEL=LLM_SMART_MODEL:latest,ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest,ALPACA_API_KEY=ALPACA_API_KEY:latest,ALPACA_SECRET_KEY=ALPACA_SECRET_KEY:latest,FINNHUB_API_KEY=FINNHUB_API_KEY:latest,TELEGRAM_BOT_TOKEN=TELEGRAM_BOT_TOKEN:latest,TELEGRAM_CHAT_ID=TELEGRAM_CHAT_ID:latest')"

gcloud scheduler jobs create http "${JOB}-trigger" --location "$REGION" \
  --schedule "$SCHEDULE" --time-zone "America/New_York" \
  --uri "https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}:run" \
  --http-method POST --oauth-service-account-email "$SA"

echo "Done. Test: gcloud run jobs execute $JOB --region $REGION --wait"
