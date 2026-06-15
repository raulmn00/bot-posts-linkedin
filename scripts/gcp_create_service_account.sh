#!/usr/bin/env bash
# Cria a service account de produção do Cloud Run + concede os roles mínimos.
# Idempotente — re-rodar não erra.
#
# Pré-requisitos:
#   gcloud auth login
#   gcloud config set project <PROJECT_ID>  (ou GCP_PROJECT_ID no .env)
#
# O que esse script garante:
#   - SA bot-posts-prod@$PROJECT.iam.gserviceaccount.com existe
#   - SA tem 5 roles a nível de projeto:
#       roles/datastore.user           (Firestore — G.2)
#       roles/storage.objectAdmin      (GCS upload + signed URL)
#       roles/cloudtasks.enqueuer      (Cloud Tasks — G.3)
#       roles/secretmanager.secretAccessor  (ler os 7 secrets no boot)
#   - SA tem roles/iam.serviceAccountTokenCreator SOBRE SI MESMA — necessário
#     pra google-cloud-storage gerar signed URLs em Cloud Run (metadata server
#     fornece OAuth token sem private key; signing é feito via IAM API).
#
# Sem o token creator, signed URLs falham com RefreshError em prod (mesmo bug
# que tivemos em dev quando usamos gcloud ADC).

set -euo pipefail

# Carrega vars do .env se existir (lê GCP_PROJECT_ID, etc.).
# Usa eval em vez de `source <(grep ...)` — process substitution + set -euo
# pipefail tem race e o source não capta as vars no subprocess.
if [[ -f .env ]]; then
  eval "$(grep -E '^(GCP_PROJECT_ID|GCP_REGION)=' .env)"
fi

PROJECT_ID="${GCP_PROJECT_ID:-${1:-}}"
if [[ -z "$PROJECT_ID" ]]; then
  echo "ERRO: defina GCP_PROJECT_ID no .env ou passe como argumento: $0 <project-id>" >&2
  exit 1
fi

SA_NAME="bot-posts-prod"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "==> Garantindo SA $SA_EMAIL"
if ! gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" \
    --project="$PROJECT_ID" \
    --display-name="Bot posts LinkedIn (produção Cloud Run)"
  echo "    criada"
else
  echo "    já existe"
fi

echo "==> Aplicando roles no projeto"
ROLES=(
  "roles/datastore.user"
  "roles/storage.objectAdmin"
  "roles/cloudtasks.enqueuer"
  "roles/secretmanager.secretAccessor"
)
for role in "${ROLES[@]}"; do
  echo "    + $role"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SA_EMAIL" \
    --role="$role" \
    --condition=None \
    --quiet >/dev/null
done

echo "==> Concedendo roles/iam.serviceAccountTokenCreator sobre a própria SA"
# Esse role é em CIMA da SA (não do projeto) — permite que ela "self-sign"
# pra gerar signed URLs do GCS sem precisar de key file.
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --project="$PROJECT_ID" \
  --member="serviceAccount:$SA_EMAIL" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --quiet >/dev/null

echo ""
echo "✅ SA pronta: $SA_EMAIL"
echo "   Use no deploy: gcloud run deploy ... --service-account=$SA_EMAIL"
