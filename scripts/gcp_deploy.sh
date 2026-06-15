#!/usr/bin/env bash
# Deploy do bot-posts-linkedin no Cloud Run.
# Usa --source pra Cloud Build buildar a imagem direto (Dockerfile na raiz).
#
# Aplica os 7 secrets + env vars não-secretas no serviço. Imprime a URL pública
# no final — copia daí pro register_telegram_webhook.py.
#
# Modo SEGURO no primeiro deploy: LINKEDIN_DRY_RUN=true em prod, mesmo que
# localmente esteja false. Depois você muda via:
#   gcloud run services update bot-posts-linkedin \
#     --region=$REGION --update-env-vars LINKEDIN_DRY_RUN=false

set -euo pipefail

# Carrega o que precisamos do .env (eval evita race com set -e).
if [[ -f .env ]]; then
  eval "$(grep -E '^(GCP_PROJECT_ID|GCP_REGION|GCS_BUCKET_NAME|GITHUB_USERNAME|LINKEDIN_API_VERSION|REPLICATE_IMAGE_MODEL|ANTHROPIC_MODEL)=' .env)"
fi

PROJECT_ID="${GCP_PROJECT_ID:?GCP_PROJECT_ID não definido (.env)}"
REGION="${GCP_REGION:-southamerica-east1}"
BUCKET="${GCS_BUCKET_NAME:?GCS_BUCKET_NAME não definido (.env)}"
GITHUB_USER="${GITHUB_USERNAME:-raulmn00}"
LINKEDIN_VERSION="${LINKEDIN_API_VERSION:-202506}"
REPLICATE_MODEL="${REPLICATE_IMAGE_MODEL:-black-forest-labs/flux-1.1-pro}"
ANTHROPIC_MODEL_VAL="${ANTHROPIC_MODEL:-claude-sonnet-4-6}"

SA_EMAIL="bot-posts-prod@${PROJECT_ID}.iam.gserviceaccount.com"
SERVICE_NAME="bot-posts-linkedin"

# Env vars não-secretas (literais que o Settings precisa).
ENV_VARS=(
  "ENV=prod"
  "LOG_LEVEL=INFO"
  "LINKEDIN_DRY_RUN=true"
  "LINKEDIN_API_VERSION=${LINKEDIN_VERSION}"
  "ANTHROPIC_MODEL=${ANTHROPIC_MODEL_VAL}"
  "REPLICATE_IMAGE_MODEL=${REPLICATE_MODEL}"
  "REPLICATE_TIMEOUT_SECONDS=60"
  "WEB_SEARCH_MAX_USES=3"
  "MAX_REVISION_ITERATIONS=5"
  "REVISION_PENDING_TTL_HOURS=24"
  "GCS_SIGNED_URL_TTL_MINUTES=10080"
  "GCP_PROJECT_ID=${PROJECT_ID}"
  "GCP_REGION=${REGION}"
  "GCS_BUCKET_NAME=${BUCKET}"
  "CLOUD_TASKS_QUEUE=bot-post-jobs"
  "GITHUB_USERNAME=${GITHUB_USER}"
  "FIRESTORE_COLLECTION_POSTS=posts"
  "POST_GENERATION_SYSTEM_PROMPT_PATH=prompts/post_generation_system.txt"
)

# Mapeamento ENV_VAR_NO_CONTAINER=secret-name:latest
SET_SECRETS=(
  "TELEGRAM_BOT_TOKEN=telegram-bot-token:latest"
  "TELEGRAM_CHAT_ID=telegram-chat-id:latest"
  "TELEGRAM_WEBHOOK_SECRET=telegram-webhook-secret:latest"
  "LINKEDIN_ACCESS_TOKEN=linkedin-access-token:latest"
  "LINKEDIN_PERSON_URN=linkedin-person-urn:latest"
  "ANTHROPIC_API_KEY=anthropic-api-key:latest"
  "REPLICATE_API_TOKEN=replicate-api-token:latest"
  "GITHUB_TOKEN=github-token:latest"
)

# Junta com vírgulas no formato esperado pelo gcloud.
ENV_VARS_JOINED=$(IFS=,; echo "${ENV_VARS[*]}")
SECRETS_JOINED=$(IFS=,; echo "${SET_SECRETS[*]}")

echo "==> Deploy $SERVICE_NAME em $REGION (projeto $PROJECT_ID)"
echo "    SA: $SA_EMAIL"
echo "    bucket: $BUCKET"
echo "    LINKEDIN_DRY_RUN=true (modo seguro no deploy)"
echo ""

gcloud run deploy "$SERVICE_NAME" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --source=. \
  --platform=managed \
  --service-account="$SA_EMAIL" \
  --allow-unauthenticated \
  --max-instances=1 \
  --min-instances=0 \
  --no-cpu-throttling \
  --cpu=1 \
  --memory=512Mi \
  --timeout=300 \
  --port=8080 \
  --set-env-vars="$ENV_VARS_JOINED" \
  --set-secrets="$SECRETS_JOINED" \
  --quiet

URL=$(gcloud run services describe "$SERVICE_NAME" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --format='value(status.url)')

echo ""
echo "✅ Deploy concluído"
echo "    URL: $URL"
echo ""
echo "Próximos passos:"
echo "  1. Registrar webhook do Telegram apontando pra essa URL:"
echo "       uv run python scripts/register_telegram_webhook.py $URL"
echo "  2. Mandar [GERAR-POST] de teste do celular (vai estar em DRY-RUN em prod)"
echo "  3. Conferir logs: gcloud run services logs read $SERVICE_NAME --region=$REGION --limit=50"
echo "  4. Quando confiante, virar pra REAL:"
echo "       gcloud run services update $SERVICE_NAME --region=$REGION --update-env-vars LINKEDIN_DRY_RUN=false"
