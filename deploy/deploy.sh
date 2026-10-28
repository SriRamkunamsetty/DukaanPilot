#!/usr/bin/env bash
# One-command deploy of DukaanPilot to Google Cloud Run.
#
#   export PROJECT_ID=my-project
#   export TELEGRAM_BOT_TOKEN=123456:ABC...     # from @BotFather
#   ./deploy/deploy.sh
#
# Safe to re-run: every step checks before it creates. Nothing secret is printed.
set -euo pipefail

: "${PROJECT_ID:?Set PROJECT_ID to your Google Cloud project id}"
: "${TELEGRAM_BOT_TOKEN:?Set TELEGRAM_BOT_TOKEN (create a bot with @BotFather)}"
REGION="${REGION:-asia-south1}"            # Mumbai: closest to Indian shop owners
SERVICE="${SERVICE:-dukaanpilot}"
MODEL="${DUKAANPILOT_MODEL:-gemini-3.5-flash}"
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"
RUNTIME_SA_NAME="${RUNTIME_SA_NAME:-dukaanpilot-run}"
SCHEDULER_SA_NAME="${SCHEDULER_SA_NAME:-dukaanpilot-scheduler}"
RUNTIME_SA="${RUNTIME_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_SA="${SCHEDULER_SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

say() { printf '\n==> %s\n' "$*"; }
gc() { gcloud --project "$PROJECT_ID" "$@"; }

say "Enabling APIs"
gc services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com firestore.googleapis.com secretmanager.googleapis.com \
  cloudscheduler.googleapis.com cloudtrace.googleapis.com

say "Firestore database (Native mode, ${REGION})"
if ! gc firestore databases describe --database='(default)' >/dev/null 2>&1; then
  gc firestore databases create --location="$REGION" --type=firestore-native
fi

say "Service accounts (least privilege)"
for pair in "${RUNTIME_SA_NAME}:DukaanPilot runtime" "${SCHEDULER_SA_NAME}:DukaanPilot scheduler"; do
  name="${pair%%:*}"; label="${pair#*:}"
  gc iam service-accounts describe "${name}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1 \
    || gc iam service-accounts create "$name" --display-name="$label"
done
for role in roles/aiplatform.user roles/datastore.user roles/logging.logWriter roles/cloudtrace.agent; do
  gc projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${RUNTIME_SA}" \
    --role="$role" --condition=None --quiet >/dev/null
done

say "Secrets (Secret Manager)"
put_secret() { # name, value
  if gc secrets describe "$1" >/dev/null 2>&1; then
    printf '%s' "$2" | gc secrets versions add "$1" --data-file=- >/dev/null
  else
    printf '%s' "$2" | gc secrets create "$1" --replication-policy=automatic --data-file=- >/dev/null
  fi
  gc secrets add-iam-policy-binding "$1" --member="serviceAccount:${RUNTIME_SA}" \
    --role=roles/secretmanager.secretAccessor --quiet >/dev/null
}
ensure_random_secret() { # name: create only if missing, so re-runs keep working webhooks
  gc secrets describe "$1" >/dev/null 2>&1 || put_secret "$1" "$(openssl rand -hex 32)"
  gc secrets add-iam-policy-binding "$1" --member="serviceAccount:${RUNTIME_SA}" \
    --role=roles/secretmanager.secretAccessor --quiet >/dev/null
}
put_secret dukaanpilot-telegram-token "$TELEGRAM_BOT_TOKEN"
ensure_random_secret dukaanpilot-telegram-webhook-secret
ensure_random_secret dukaanpilot-internal-secret

# The service URL is predictable, which lets Cloud Run know its own OIDC audience at deploy time.
PROJECT_NUMBER="$(gc projects describe "$PROJECT_ID" --format='value(projectNumber)')"
SERVICE_URL="https://${SERVICE}-${PROJECT_NUMBER}.${REGION}.run.app"

say "Deploying to Cloud Run (${SERVICE_URL})"
# --allow-unauthenticated is required because Telegram cannot send a Google identity token.
# The app itself rejects anything without the webhook secret or a Scheduler OIDC token.
gc run deploy "$SERVICE" --source . --region "$REGION" \
  --service-account "$RUNTIME_SA" \
  --allow-unauthenticated \
  --memory 1Gi --cpu 1 --timeout 120 --concurrency 20 \
  --min-instances 0 --max-instances 3 \
  --set-env-vars "DUKAANPILOT_ENV=prod,DUKAANPILOT_STORE=firestore,DUKAANPILOT_MODEL=${MODEL},GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${VERTEX_LOCATION},DUKAANPILOT_SESSION_DB_URL=sqlite+aiosqlite:////tmp/sessions.db,DUKAANPILOT_DB_PATH=/tmp/unused.db,SCHEDULER_AUDIENCE=${SERVICE_URL},SCHEDULER_SERVICE_ACCOUNT=${SCHEDULER_SA}" \
  --set-secrets "TELEGRAM_BOT_TOKEN=dukaanpilot-telegram-token:latest,TELEGRAM_WEBHOOK_SECRET=dukaanpilot-telegram-webhook-secret:latest,INTERNAL_SHARED_SECRET=dukaanpilot-internal-secret:latest"

ACTUAL_URL="$(gc run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
if [ "$ACTUAL_URL" != "$SERVICE_URL" ]; then
  echo "Note: the real URL is ${ACTUAL_URL}; updating the Scheduler audience to match."
  gc run services update "$SERVICE" --region "$REGION" --update-env-vars "SCHEDULER_AUDIENCE=${ACTUAL_URL}"
  SERVICE_URL="$ACTUAL_URL"
fi

say "Pointing the Telegram webhook at the service"
WEBHOOK_SECRET="$(gc secrets versions access latest --secret=dukaanpilot-telegram-webhook-secret)"
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${SERVICE_URL}/webhooks/telegram" \
  --data-urlencode "secret_token=${WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message","callback_query"]' \
  --data-urlencode "drop_pending_updates=true" >/dev/null
echo "Webhook set."

say "Cloud Scheduler: hourly briefing + stock watch"
JOB="${SERVICE}-hourly"
SCHED_ARGS=(--location "$REGION" --schedule "0 * * * *" --time-zone "Asia/Kolkata"
  --uri "${SERVICE_URL}/internal/jobs/run?job=all" --http-method POST
  --oidc-service-account-email "$SCHEDULER_SA" --oidc-token-audience "$SERVICE_URL"
  --attempt-deadline 180s)
if gc scheduler jobs describe "$JOB" --location "$REGION" >/dev/null 2>&1; then
  gc scheduler jobs update http "$JOB" "${SCHED_ARGS[@]}"
else
  gc scheduler jobs create http "$JOB" "${SCHED_ARGS[@]}"
fi

say "Done"
echo "Service:   ${SERVICE_URL}"
echo "Health:    curl ${SERVICE_URL}/healthz"
echo "Next:      create your shop and get a join code:  python scripts/create_shop.py --help"
