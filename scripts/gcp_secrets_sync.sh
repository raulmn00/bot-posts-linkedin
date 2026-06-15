#!/usr/bin/env bash
# Cria (ou adiciona nova versão) dos 7 secrets do .env no Secret Manager.
# Idempotente — secrets pré-existentes recebem uma nova versão.
#
# Pré-requisitos:
#   gcloud auth login
#   .env populado localmente
#   Secret Manager API habilitada (já foi no Passo 2)

set -euo pipefail

if [[ ! -f .env ]]; then
  echo "ERRO: .env não encontrado no diretório atual" >&2
  exit 1
fi

# Carrega só GCP_PROJECT_ID do .env (eval evita race do process substitution + set -e).
eval "$(grep -E '^GCP_PROJECT_ID=' .env)"

PROJECT_ID="${GCP_PROJECT_ID:-${1:-}}"
if [[ -z "$PROJECT_ID" ]]; then
  echo "ERRO: defina GCP_PROJECT_ID no .env ou passe como argumento: $0 <project-id>" >&2
  exit 1
fi

# Mapeamento ENV_VAR_DO_DOTENV:secret-name-no-gcp.
# Pares colon-separated em vez de `declare -A` — macOS vem com bash 3.2 que
# não tem arrays associativos. Bash 3.2-compatible.
SECRETS=(
  "TELEGRAM_BOT_TOKEN:telegram-bot-token"
  "TELEGRAM_CHAT_ID:telegram-chat-id"
  "TELEGRAM_WEBHOOK_SECRET:telegram-webhook-secret"
  "LINKEDIN_ACCESS_TOKEN:linkedin-access-token"
  "LINKEDIN_PERSON_URN:linkedin-person-urn"
  "ANTHROPIC_API_KEY:anthropic-api-key"
  "REPLICATE_API_TOKEN:replicate-api-token"
  "GITHUB_TOKEN:github-token"
)

echo "==> Sincronizando ${#SECRETS[@]} secrets pro projeto $PROJECT_ID"
for pair in "${SECRETS[@]}"; do
  env_var="${pair%%:*}"
  secret_name="${pair##*:}"
  # Lê o valor do .env (pega só o que vem após o primeiro = pra não estragar valores com =)
  value=$(grep -E "^${env_var}=" .env | head -1 | cut -d= -f2-)
  if [[ -z "$value" ]]; then
    echo "    ⚠ $env_var está vazio no .env — PULANDO $secret_name"
    continue
  fi

  if gcloud secrets describe "$secret_name" --project="$PROJECT_ID" >/dev/null 2>&1; then
    # Já existe — adiciona nova versão (não sobrescreve histórico).
    printf "%s" "$value" | gcloud secrets versions add "$secret_name" \
      --project="$PROJECT_ID" \
      --data-file=- >/dev/null
    echo "    ↻ $secret_name (nova versão)"
  else
    # Cria do zero.
    printf "%s" "$value" | gcloud secrets create "$secret_name" \
      --project="$PROJECT_ID" \
      --replication-policy=automatic \
      --data-file=- >/dev/null
    echo "    + $secret_name (criado)"
  fi
done

echo ""
echo "✅ 7 secrets sincronizados. Use no deploy via --set-secrets."
