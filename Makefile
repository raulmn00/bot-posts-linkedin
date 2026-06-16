.PHONY: install dev test lint fmt clean \
        gcp-create-sa gcp-secrets-sync gcp-deploy gcp-register-webhook gcp-logs \
        gcp-toggle-dry-run gcp-toggle-real \
        gcp-firestore-indexes gcp-firestore-ttl gcp-tasks-queue

# Instala dependências (produção + dev) via uv.
install:
	uv sync

# Sobe o FastAPI em modo reload na porta 8080.
dev:
	uv run uvicorn bot_posts_linkedin.main:app --reload --host 0.0.0.0 --port 8080

# Roda a suite de testes.
test:
	uv run pytest -v

# Lint sem alterar arquivos.
lint:
	uv run ruff check src tests

# Formata + aplica fixes automáticos do ruff.
fmt:
	uv run ruff format src tests
	uv run ruff check --fix src tests

clean:
	rm -rf .venv .pytest_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} +

# =============================================================================
# GCP / Cloud Run (Fase G.1)
# =============================================================================

# 1× por projeto: cria SA bot-posts-prod com roles + token-creator sobre si mesma.
gcp-create-sa:
	bash scripts/gcp_create_service_account.sh

# Lê os 7 valores secretos do .env e cria/atualiza no Secret Manager.
# Re-rodar adiciona uma nova versão (não substitui o histórico).
gcp-secrets-sync:
	bash scripts/gcp_secrets_sync.sh

# Cria composite index do Firestore pra find_active_for_chat (G.2).
# Rodar 1× por projeto antes do primeiro deploy com Firestore real.
# Construção leva ~5min mas é assíncrona — script retorna imediato.
gcp-firestore-indexes:
	bash scripts/gcp_firestore_indexes.sh

# Configura TTL nativo na collection processed_updates (G.3).
# Rodar 1× por projeto antes do deploy com dedup ativo.
gcp-firestore-ttl:
	bash scripts/gcp_firestore_ttl_setup.sh

# Cria/atualiza config da Cloud Tasks queue bot-post-jobs (G.3).
# Rodar 1× ou quando quiser reaplicar config (retries, throttle).
gcp-tasks-queue:
	bash scripts/gcp_tasks_queue.sh

# Build via Cloud Build + deploy no Cloud Run. Sai em DRY-RUN seguro por default.
gcp-deploy:
	bash scripts/gcp_deploy.sh

# Aponta o webhook do Telegram pra URL atual do Cloud Run (autodescobre).
# Usa `eval "$$(grep ...)"` em vez de `cut` — eval trata comentários inline do
# .env corretamente (ex: `GCP_REGION=southamerica-east1   # São Paulo...`).
gcp-register-webhook:
	@eval "$$(grep -E '^(GCP_PROJECT_ID|GCP_REGION)=' .env)"; \
	URL=$$(gcloud run services describe bot-posts-linkedin \
		--project="$$GCP_PROJECT_ID" --region="$${GCP_REGION:-southamerica-east1}" \
		--format='value(status.url)'); \
	echo "Registrando webhook em $$URL"; \
	uv run python scripts/register_telegram_webhook.py "$$URL"

# Tail dos logs do Cloud Run (últimos 50). Cmd+C pra parar.
gcp-logs:
	@eval "$$(grep -E '^(GCP_PROJECT_ID|GCP_REGION)=' .env)"; \
	gcloud run services logs read bot-posts-linkedin \
		--project="$$GCP_PROJECT_ID" --region="$${GCP_REGION:-southamerica-east1}" --limit=50

# Coloca o serviço em DRY-RUN (não publica de verdade).
gcp-toggle-dry-run:
	@eval "$$(grep -E '^(GCP_PROJECT_ID|GCP_REGION)=' .env)"; \
	gcloud run services update bot-posts-linkedin \
		--project="$$GCP_PROJECT_ID" --region="$${GCP_REGION:-southamerica-east1}" \
		--update-env-vars=LINKEDIN_DRY_RUN=true; \
	echo "✅ Cloud Run agora em DRY-RUN"

# ⚠️ Coloca o serviço em modo REAL (publica de verdade no perfil).
gcp-toggle-real:
	@eval "$$(grep -E '^(GCP_PROJECT_ID|GCP_REGION)=' .env)"; \
	gcloud run services update bot-posts-linkedin \
		--project="$$GCP_PROJECT_ID" --region="$${GCP_REGION:-southamerica-east1}" \
		--update-env-vars=LINKEDIN_DRY_RUN=false; \
	echo "⚠️  Cloud Run agora em modo REAL (publica de verdade)"
