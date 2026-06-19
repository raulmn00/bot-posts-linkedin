#!/usr/bin/env bash
# Sec-9: habilita Container Analysis API no Artifact Registry.
#
# O que isso faz: a partir do próximo push pra o Artifact Registry, GCP escaneia
# automaticamente camadas do container contra CVEs do banco do OSV. Resultados
# aparecem em Artifact Registry > Image > Vulnerabilities. Free tier inclui
# scanning de imagens, mas cada scan consome quota — pra projetos pequenos
# (1 imagem, 1 push por release) o custo é zero.
#
# Roda 1× por projeto. Idempotente.

set -euo pipefail

if [[ -f .env ]]; then
  eval "$(grep -E '^GCP_PROJECT_ID=' .env)"
fi

PROJECT_ID="${GCP_PROJECT_ID:?GCP_PROJECT_ID não definido (.env)}"

echo "==> Habilitando Container Analysis API no projeto $PROJECT_ID"

gcloud services enable \
  containeranalysis.googleapis.com \
  containerscanning.googleapis.com \
  --project="$PROJECT_ID" \
  --quiet

echo ""
echo "✅ Container Analysis ativo. A próxima imagem pushada pro Artifact Registry"
echo "   será escaneada automaticamente."
echo ""
echo "Pra revisar achados (após próximo deploy):"
echo "  gcloud artifacts docker images list-vulnerabilities \\"
echo "    us-central1-docker.pkg.dev/$PROJECT_ID/cloud-run-source-deploy/bot-posts-linkedin \\"
echo "    --project=$PROJECT_ID"
echo ""
echo "Ou via console:"
echo "  https://console.cloud.google.com/artifacts?project=$PROJECT_ID"
