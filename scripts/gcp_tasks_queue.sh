#!/usr/bin/env bash
# Cria/configura a Cloud Tasks queue `bot-post-jobs` (Fase G.3).
# Já criamos uma queue no Passo 2, mas o `update` re-aplica config (retries,
# rate limits) que talvez não estivessem corretos pro nosso uso.
#
# Idempotente — re-rodar não erra.

set -euo pipefail

if [[ -f .env ]]; then
  eval "$(grep -E '^(GCP_PROJECT_ID|GCP_REGION|CLOUD_TASKS_QUEUE)=' .env)"
fi

PROJECT_ID="${GCP_PROJECT_ID:?GCP_PROJECT_ID não definido (.env)}"
REGION="${GCP_REGION:-southamerica-east1}"
QUEUE="${CLOUD_TASKS_QUEUE:-bot-post-jobs}"

echo "==> Garantindo queue $QUEUE em $REGION (projeto $PROJECT_ID)"

if gcloud tasks queues describe "$QUEUE" --location="$REGION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  echo "    já existe — aplicando config"
  ACTION=update
else
  echo "    criando do zero"
  ACTION=create
fi

# Config:
# - max-attempts=3: limita retry em caso de falha (default era 100)
# - min-backoff=30s, max-backoff=10min: espalha retries
# - max-concurrent-dispatches=1: garante 1 task processada por vez (alinhado com max-instances=1)
# - max-dispatches-per-second=5: throttle saudável
gcloud tasks queues "$ACTION" "$QUEUE" \
  --location="$REGION" \
  --project="$PROJECT_ID" \
  --max-attempts=3 \
  --min-backoff=30s \
  --max-backoff=600s \
  --max-doublings=4 \
  --max-concurrent-dispatches=1 \
  --max-dispatches-per-second=5 \
  --quiet

echo ""
echo "✅ Queue $QUEUE configurada"
echo "   Inspeção: https://console.cloud.google.com/cloudtasks/queue/$REGION/$QUEUE?project=$PROJECT_ID"
