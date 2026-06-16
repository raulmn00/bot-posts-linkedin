#!/usr/bin/env bash
# Cria o composite index do Firestore que `find_active_for_chat` usa.
# Construção leva ~5min na primeira vez; subsequentes são idempotentes (no-op).
#
# Sem esse index, a query (chat_id == X AND status IN [...] ORDER BY created_at DESC)
# levanta FailedPrecondition com a URL pra criar o index manualmente. Esse script
# automatiza isso.

set -euo pipefail

# Carrega project_id do .env (eval evita race do process substitution + set -e).
if [[ -f .env ]]; then
  eval "$(grep -E '^GCP_PROJECT_ID=' .env)"
fi

PROJECT_ID="${GCP_PROJECT_ID:-${1:-}}"
if [[ -z "$PROJECT_ID" ]]; then
  echo "ERRO: defina GCP_PROJECT_ID no .env ou passe como argumento: $0 <project-id>" >&2
  exit 1
fi

echo "==> Criando composite index pra posts (chat_id + status + created_at)"
echo "    Construção pode levar ~5min na primeira vez. Re-rodar = no-op."
echo ""

gcloud firestore indexes composite create \
  --project="$PROJECT_ID" \
  --collection-group=posts \
  --query-scope=COLLECTION \
  --field-config=field-path=chat_id,order=ascending \
  --field-config=field-path=status,order=ascending \
  --field-config=field-path=created_at,order=descending \
  --async 2>&1 | tail -5

echo ""
echo "✅ Pedido de criação submetido. Acompanhe em:"
echo "   https://console.cloud.google.com/firestore/databases/-default-/indexes?project=$PROJECT_ID"
echo ""
echo "Quando estiver READY, find_active_for_chat funciona em prod."
