#!/usr/bin/env bash
# Configura TTL nativo do Firestore na collection processed_updates (Fase G.3).
# TTL field = `expires_at`. Docs com expires_at < now são deletados automaticamente
# pelo Firestore em até 24h após expiração (sem cobrança extra).

set -euo pipefail

if [[ -f .env ]]; then
  eval "$(grep -E '^(GCP_PROJECT_ID|FIRESTORE_COLLECTION_PROCESSED_UPDATES)=' .env)"
fi

PROJECT_ID="${GCP_PROJECT_ID:?GCP_PROJECT_ID não definido (.env)}"
COLLECTION="${FIRESTORE_COLLECTION_PROCESSED_UPDATES:-processed_updates}"

echo "==> Configurando TTL no campo expires_at de /$COLLECTION (projeto $PROJECT_ID)"

gcloud firestore fields ttls update expires_at \
  --collection-group="$COLLECTION" \
  --project="$PROJECT_ID" \
  --enable-ttl \
  --quiet

echo ""
echo "✅ TTL ativo. Documentos com expires_at no passado serão limpos automaticamente."
echo "   Inspeção: https://console.cloud.google.com/firestore/databases/-default-/ttl?project=$PROJECT_ID"
