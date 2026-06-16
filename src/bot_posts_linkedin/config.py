from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configurações da aplicação.

    Em dev lê o arquivo .env na raiz; em prod (Cloud Run) lê variáveis de ambiente
    injetadas pelo Secret Manager. Falha no boot se faltar alguma var obrigatória.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- App ---
    env: Literal["dev", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    app_base_url: str = "http://localhost:8080"

    # --- Telegram ---
    telegram_bot_token: str = Field(min_length=10)
    telegram_chat_id: str = Field(min_length=1)
    telegram_webhook_secret: str = Field(min_length=16)

    # --- LinkedIn ---
    linkedin_access_token: str = Field(min_length=1)
    linkedin_person_urn: str = Field(min_length=1)
    # Versão da REST API do LinkedIn — env var, NÃO hardcoded.
    # LinkedIn aposenta versões a cada ~12 meses; mudar aqui evita redeploy.
    # Faltar o header LinkedIn-Version é causa nº1 de 426/400 enigmático.
    linkedin_api_version: str = "202506"
    # SEGURANÇA: default true. Quando true, o publisher monta o payload COMPLETO
    # mas NÃO faz POST real no LinkedIn — envia o JSON pra inspeção no Telegram
    # e marca o post como SIMULATED. Mudar pra false publica de verdade no perfil.
    linkedin_dry_run: bool = True

    @field_validator("linkedin_person_urn")
    @classmethod
    def _person_urn_format(cls, v: str) -> str:
        # URN do LinkedIn sempre vem nesse formato — falhar cedo aqui evita erro só na publicação.
        if not v.startswith("urn:li:person:"):
            raise ValueError("LINKEDIN_PERSON_URN deve começar com 'urn:li:person:'")
        return v

    # --- Anthropic (LLM + web search nativo) ---
    anthropic_api_key: str = Field(min_length=10)
    anthropic_model: str = "claude-sonnet-4-6"
    # Semântica: o user pode APLICAR até N revisões antes de ser forçado a decidir
    # aprovar/cancelar. Com MAX=5, ele faz revisões #1..#5 normais; ao reprovar a
    # 6ª vez (já com revision_count=5), o limit flow aparece. Com MAX=1, ele faz 1
    # revisão e na 2ª tentativa de reprovação aparece o limit flow.
    # O check é `revision_count >= max` em handle_rejection.
    max_revision_iterations: int = Field(default=5, ge=1, le=20)
    # Quantas buscas o tool web_search_20250305 pode disparar por chamada — tuning de custo.
    web_search_max_uses: int = Field(default=3, ge=1, le=10)
    # TTL do chat_state quando aguardando motivo de reprovação. 24h cobre "sair e voltar
    # no mesmo dia" sem deixar mensagem da semana seguinte virar motivo órfão.
    revision_pending_ttl_hours: int = Field(default=24, ge=1, le=168)

    # --- Replicate (Flux 1.1 Pro) ---
    replicate_api_token: str = Field(min_length=10)
    replicate_image_model: str = "black-forest-labs/flux-1.1-pro"
    # Timeout duro do polling (succeeded/failed). Estourou → REJECTED.
    replicate_timeout_seconds: int = Field(default=60, ge=10, le=300)

    # --- Geração do post ---
    # Path do arquivo com o system prompt customizado (PT-BR, persona do Raul).
    # Editar o arquivo é mais limpo que pôr multi-linha no .env.
    post_generation_system_prompt_path: str = "prompts/post_generation_system.txt"

    # --- GitHub (busca em repos públicos quando flag [GITHUB] presente) ---
    github_token: str = Field(min_length=10)
    github_username: str = "raulmn00"

    # --- GCP ---
    gcp_project_id: str = Field(min_length=1)
    gcp_region: str = "southamerica-east1"
    gcs_bucket_name: str = Field(min_length=1)
    cloud_tasks_queue: str = "bot-post-jobs"
    # Path do endpoint que processa as tasks enfileiradas (chamado pelo Cloud Tasks).
    cloud_tasks_worker_path: str = "/internal/process-task"
    # Em Cloud Run essa var não é setada — usa a identity da metadata. Por isso opcional.
    google_application_credentials: str | None = None
    # TTL (minutos) das URLs assinadas geradas pro Telegram acessar a imagem do GCS.
    # 7 dias cobre folgado o ciclo de aprovação humana.
    gcs_signed_url_ttl_minutes: int = Field(default=60 * 24 * 7, ge=10, le=60 * 24 * 30)

    # --- Firestore ---
    firestore_collection_posts: str = "posts"
    firestore_collection_chat_states: str = "chat_states"
    # G.3: dedup por update_id do Telegram. TTL aplicado via campo expires_at +
    # configuração de TTL no Firestore (script gcp_firestore_ttl_setup.sh).
    firestore_collection_processed_updates: str = "processed_updates"
    processed_updates_ttl_minutes: int = Field(default=10, ge=1, le=60)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cache de processo. Settings só são instanciadas uma vez."""
    return Settings()  # type: ignore[call-arg]
