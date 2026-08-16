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

# `source` is not a dotenv parser, and the difference is not academic:
# `KEY= value` assigns nothing and then runs `value` as a command, and
# `SEC_USER_AGENT=daybreak-research you@example.com` assigns the first word and
# runs the address as a command. Under `set -e` both abort this script; without
# it they would push a truncated secret and quietly disable a data source in
# cloud while everything still worked locally, because python-dotenv is lenient
# about exactly these two shapes. Parse it ourselves, applying the same
# inline-comment rule as config.py::_clean() so bash and the app agree on every
# value.
load_env () {
  local LINE KEY VAL
  while IFS= read -r LINE || [ -n "$LINE" ]; do
    LINE="${LINE#"${LINE%%[![:space:]]*}"}"
    case "$LINE" in ''|'#'*) continue ;; esac
    [ "${LINE#*=}" != "$LINE" ] || continue
    KEY="${LINE%%=*}"; VAL="${LINE#*=}"
    KEY="${KEY%"${KEY##*[![:space:]]}"}"
    case "$KEY" in ''|*[!A-Za-z0-9_]*) continue ;; esac
    VAL="$(printf '%s' "$VAL" | sed -E 's/(^|[[:space:]])#.*$//')"
    VAL="${VAL#"${VAL%%[![:space:]]*}"}"; VAL="${VAL%"${VAL##*[![:space:]]}"}"
    case "$VAL" in \"*\") VAL="${VAL#\"}"; VAL="${VAL%\"}" ;; \'*\') VAL="${VAL#\'}"; VAL="${VAL%\'}" ;; esac
    export "$KEY=$VAL"
  done < "$1"
}
load_env "$ENV_FILE"

# --- Validate required values -------------------------------------------------
for VAR in VERTEXAI_PROJECT VERTEXAI_LOCATION LLM_FAST_MODEL LLM_SMART_MODEL LLM_DEEP_MODEL \
           ALPACA_API_KEY ALPACA_SECRET_KEY FINNHUB_API_KEY; do
  [ -n "${!VAR:-}" ] || { echo "ERROR: $VAR is empty in config/.env"; exit 1; }
done
[ "${ALPACA_PAPER:-}" = "true" ] || { echo "ERROR: ALPACA_PAPER must be true (research guardrail)"; exit 1; }
# EDGAR's fair-access rules require a real contact address; signals/insider.py
# skips the source outright without one. A truncated value is the likely cause,
# so fail here rather than ship a permanently degraded Form 4 signal.
case "${SEC_USER_AGENT:-}" in
  "") echo "WARNING: SEC_USER_AGENT unset — the SEC Form 4 signal will skip itself." ;;
  *@*.*) ;;
  *) echo "ERROR: SEC_USER_AGENT has no contact address; EDGAR will refuse us. Expected 'name you@example.com'."; exit 1 ;;
esac
[ -n "${FRED_API_KEY:-}" ] || echo "WARNING: FRED_API_KEY unset — the macro-regime signal will skip itself."
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

# A freshly created service account is not immediately visible to the IAM APIs:
# the very next add-iam-policy-binding fails with "does not exist" until the
# identity has propagated. Wait for it rather than making the operator re-run
# the script and wonder which half applied.
echo "Waiting for ${SA} to propagate..."
for _ in $(seq 1 30); do
  gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 && break
  sleep 2
done
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 \
  || { echo "ERROR: ${SA} still not visible after 60s."; exit 1; }

# Propagation to the *policy* backends lags visibility, so bindings still race.
# Only retry the propagation class: a deterministic error retried ten times just
# buries the real message under ten copies of itself.
retry () {
  local N=0 OUT
  while :; do
    if OUT="$("$@" 2>&1)"; then printf '%s\n' "$OUT"; return 0; fi
    case "$OUT" in
      *"does not exist"*|*NOT_FOUND*|*"not found"*|*"Internal error"*|*503*|*"try again"*) ;;
      *) printf '%s\n' "$OUT" >&2; return 1 ;;
    esac
    N=$((N + 1))
    [ "$N" -lt 10 ] || { printf '%s\n' "$OUT" >&2; echo "ERROR: gave up after $N attempts: $*" >&2; return 1; }
    echo "  waiting for IAM propagation, retry $N/10..."
    sleep 5
  done
}

# --- Permissions: GCS (reports/journal) + Vertex (invoke Claude) ---------------
retry gcloud storage buckets add-iam-policy-binding "$BUCKET" \
  --member="serviceAccount:${SA}" --role=roles/storage.objectAdmin
# --condition=None is required, not cosmetic: on a shared project whose IAM
# policy already contains conditional bindings (someone else's), gcloud refuses
# to add an unconditional one in non-interactive mode unless you say so. This
# appends a binding for our SA only; nothing existing is touched.
retry gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SA}" --role=roles/aiplatform.user --condition=None

# --- Secrets: pushed from config/.env values; empty ones skipped ---------------
push_secret () {
  local NAME="$1" VALUE="${!1:-}"
  [ -n "$VALUE" ] || { echo "skip secret $NAME (empty)"; return 0; }
  printf "%s" "$VALUE" | gcloud secrets create "$NAME" --data-file=- 2>/dev/null \
    || printf "%s" "$VALUE" | gcloud secrets versions add "$NAME" --data-file=-
  retry gcloud secrets add-iam-policy-binding "$NAME" \
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
# The experiment ledger stamps every row with the commit that produced it, which
# is the only way a later change in the numbers can be attributed to a change in
# the code. The image carries neither git nor .git, and `gcloud builds submit
# --tag` takes no --build-arg, so the commit is set here instead — this script
# builds and deploys in one go, so the job's env and its image are the same code.
GIT_COMMIT="$(git -C "$(dirname "$0")/.." rev-parse --short=12 HEAD 2>/dev/null || echo unknown)"
ENV_VARS+=",GIT_COMMIT=${GIT_COMMIT}"

gcloud run jobs deploy "$JOB" --image "$IMAGE" --region "$REGION" \
  --service-account "$SA" --task-timeout=30m --memory=1Gi --max-retries=0 \
  --set-env-vars "$ENV_VARS" \
  ${SET_SECRETS:+--set-secrets "$SET_SECRETS"}

# --- Scheduler trigger ---------------------------------------------------------
# The scheduler authenticates as the same SA, so it needs permission to invoke
# the job it triggers. Without this the cron fires and gets a silent 403.
retry gcloud run jobs add-iam-policy-binding "$JOB" --region "$REGION" \
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
