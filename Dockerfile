# syntax=docker/dockerfile:1.7
# Imagem do bot-posts-linkedin pro Cloud Run (Fase G.1).
# Multi-stage: builder com uv resolve deps no .venv; runtime só carrega o venv pronto.
# Sem deps dev (pytest/ruff) — `uv sync --no-dev`.

# =============================================================================
# Stage 1: builder — instala deps de produção via uv
# =============================================================================
FROM python:3.13-slim AS builder

# Copia o binário do uv direto da imagem oficial (não precisa pip install).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Otimização de cache: copia só os manifestos primeiro. Se nada mudar nos
# lockfiles, o layer de deps fica em cache mesmo com mudanças no src.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# =============================================================================
# Stage 2: runtime — slim Python + venv montado
# =============================================================================
FROM python:3.13-slim AS runtime

# Cria usuário não-root pra rodar o app (boa prática de segurança em Cloud Run).
RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --home-dir /app --shell /bin/false app

WORKDIR /app

# Copia o venv pronto do builder + o source da aplicação.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src ./src
COPY --chown=app:app prompts ./prompts

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER app

# Cloud Run injeta $PORT (default 8080). O fail-fast do lifespan vai validar
# Replicate + GCS + LinkedIn antes de aceitar requests — se algo falhar, o
# container morre e o Cloud Run faz restart automático.
EXPOSE 8080
CMD ["sh", "-c", "uvicorn bot_posts_linkedin.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
