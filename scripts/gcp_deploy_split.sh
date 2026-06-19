#!/usr/bin/env bash
# Sec-11 (Tier 2): deploy SPLIT em 2 services Cloud Run.
#
#   bot-posts-public   → ROLE=public  → ingress=all          → /telegram/*
#   bot-posts-worker   → ROLE=worker  → ingress=internal     → /internal/*
#
# Por que: o webhook do Telegram tem que ser público (Telegram sai da internet
# pra nos chamar). Mas o /internal/process-task só é invocado pelo Cloud Tasks
# (sai do GCP), então pode ter ingress=internal — o que elimina QUALQUER tráfego
# externo conseguir chegar nele, mesmo com token OIDC válido.
#
# OIDC continua válido (defesa em profundidade): mesmo que o worker fosse
# acidentalmente exposto, o token signing + audience matching já bloqueia.
# Ingress=internal é a segunda camada.
#
# Roda quantas vezes quiser. Idempotente. Após primeiro deploy:
#   1. Worker é deployado primeiro (audience precisa existir)
#   2. WORKER_BASE_URL fica conhecida
#   3. Public é deployado com WORKER_BASE_URL apontando pro worker
#   4. APP_BASE_URL do public é setado em 2-pass

set -euo pipefail

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
PUBLIC_NAME="bot-posts-public"
WORKER_NAME="bot-posts-worker"

# Env vars comuns a ambos services (excluindo ROLE, APP_BASE_URL e WORKER_BASE_URL).
COMMON_ENV=(
  "ENV=prod"
  "LOG_LEVEL=INFO"
  "LINKEDIN_DRY_RUN=false"
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
SECRETS_JOINED=$(IFS=,; echo "${SET_SECRETS[*]}")

# ============================================================================
# STEP 1 — Deploy do WORKER (ingress=internal, role=worker)
# ============================================================================
echo "==> [1/3] Deploy do WORKER ($WORKER_NAME) — ingress=internal"

WORKER_EXISTING_URL=$(gcloud run services describe "$WORKER_NAME" \
  --project="$PROJECT_ID" --region="$REGION" \
  --format='value(status.url)' 2>/dev/null || echo "")

# Worker primeiro deploy: APP_BASE_URL é placeholder (worker em si não usa).
# WORKER_BASE_URL será atualizada em 2-pass.
WORKER_ENV=("${COMMON_ENV[@]}")
WORKER_ENV+=("ROLE=worker")
WORKER_ENV+=("APP_BASE_URL=${WORKER_EXISTING_URL:-https://placeholder.run.app}")
WORKER_ENV+=("WORKER_BASE_URL=${WORKER_EXISTING_URL:-https://placeholder.run.app}")
WORKER_ENV_JOINED=$(IFS=,; echo "${WORKER_ENV[*]}")

# --ingress=internal-and-cloud-load-balancing — só tráfego DENTRO do GCP (incluindo
# Cloud Tasks). Internet pública NÃO consegue chegar nesse URL nem com OIDC válido.
gcloud run deploy "$WORKER_NAME" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --source=. \
  --platform=managed \
  --service-account="$SA_EMAIL" \
  --no-allow-unauthenticated \
  --ingress=internal-and-cloud-load-balancing \
  --max-instances=1 \
  --min-instances=0 \
  --no-cpu-throttling \
  --cpu=1 \
  --memory=512Mi \
  --timeout=300 \
  --port=8080 \
  --set-env-vars="$WORKER_ENV_JOINED" \
  --set-secrets="$SECRETS_JOINED" \
  --quiet

WORKER_URL=$(gcloud run services describe "$WORKER_NAME" \
  --project="$PROJECT_ID" --region="$REGION" \
  --format='value(status.url)')

# 2-pass do worker se primeiro deploy.
if [[ -z "$WORKER_EXISTING_URL" ]]; then
  echo "    primeiro deploy: setando WORKER_BASE_URL=$WORKER_URL no worker"
  gcloud run services update "$WORKER_NAME" \
    --project="$PROJECT_ID" --region="$REGION" \
    --update-env-vars="APP_BASE_URL=$WORKER_URL,WORKER_BASE_URL=$WORKER_URL" \
    --quiet >/dev/null
fi

echo "    worker URL: $WORKER_URL"

# ============================================================================
# STEP 2 — Permission: Cloud Tasks SA pode invokar o worker
# ============================================================================
echo "==> [2/3] Concedendo roles/run.invoker pra SA no worker"
gcloud run services add-iam-policy-binding "$WORKER_NAME" \
  --project="$PROJECT_ID" --region="$REGION" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/run.invoker" \
  --quiet >/dev/null

# ============================================================================
# STEP 3 — Deploy do PUBLIC (ingress=all, role=public)
# ============================================================================
echo "==> [3/3] Deploy do PUBLIC ($PUBLIC_NAME) — ingress=all"

PUBLIC_EXISTING_URL=$(gcloud run services describe "$PUBLIC_NAME" \
  --project="$PROJECT_ID" --region="$REGION" \
  --format='value(status.url)' 2>/dev/null || echo "")

PUBLIC_ENV=("${COMMON_ENV[@]}")
PUBLIC_ENV+=("ROLE=public")
PUBLIC_ENV+=("APP_BASE_URL=${PUBLIC_EXISTING_URL:-https://placeholder.run.app}")
PUBLIC_ENV+=("WORKER_BASE_URL=$WORKER_URL")
PUBLIC_ENV_JOINED=$(IFS=,; echo "${PUBLIC_ENV[*]}")

gcloud run deploy "$PUBLIC_NAME" \
  --project="$PROJECT_ID" \
  --region="$REGION" \
  --source=. \
  --platform=managed \
  --service-account="$SA_EMAIL" \
  --allow-unauthenticated \
  --ingress=all \
  --max-instances=1 \
  --min-instances=0 \
  --no-cpu-throttling \
  --cpu=1 \
  --memory=512Mi \
  --timeout=300 \
  --port=8080 \
  --set-env-vars="$PUBLIC_ENV_JOINED" \
  --set-secrets="$SECRETS_JOINED" \
  --quiet

PUBLIC_URL=$(gcloud run services describe "$PUBLIC_NAME" \
  --project="$PROJECT_ID" --region="$REGION" \
  --format='value(status.url)')

# 2-pass do public se primeiro deploy.
if [[ -z "$PUBLIC_EXISTING_URL" ]]; then
  echo "    primeiro deploy: setando APP_BASE_URL=$PUBLIC_URL no public"
  gcloud run services update "$PUBLIC_NAME" \
    --project="$PROJECT_ID" --region="$REGION" \
    --update-env-vars="APP_BASE_URL=$PUBLIC_URL" \
    --quiet >/dev/null
fi

echo ""
echo "✅ Deploy SPLIT concluído"
echo "    public URL (Telegram aponta aqui): $PUBLIC_URL"
echo "    worker URL (Cloud Tasks chama):    $WORKER_URL"
echo ""
echo "Próximos passos:"
echo "  1. Registrar webhook do Telegram apontando pro PUBLIC:"
echo "       uv run python scripts/register_telegram_webhook.py $PUBLIC_URL"
echo "  2. Validar que worker NÃO responde de fora do GCP:"
echo "       curl -i $WORKER_URL/healthz   # deve dar 403 ou hang"
echo "  3. Mandar [GERAR-POST] do celular — aprovar publica no LinkedIn"
echo ""
echo "Notas Sec-11:"
echo "  - worker ingress=internal-and-cloud-load-balancing: só GCP fala"
echo "  - public ingress=all: necessário, Telegram precisa alcançar"
echo "  - Cloud Tasks → worker: usa internal network + OIDC do bot-posts-prod"
echo "  - OIDC audience do worker = $WORKER_URL/internal/process-task"
