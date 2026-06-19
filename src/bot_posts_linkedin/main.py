import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bot_posts_linkedin.config import get_settings
from bot_posts_linkedin.security import (
    install_security_middlewares,
    install_trace_log_filter,
)
from bot_posts_linkedin.services.anthropic_client import HttpxAnthropicClient
from bot_posts_linkedin.services.github_search import GithubApiSearch
from bot_posts_linkedin.services.image_generator import (
    GoogleCloudStorageImpl,
    HttpxReplicateImageService,
)
from bot_posts_linkedin.services.linkedin_publisher import HttpxLinkedInPublisher
from bot_posts_linkedin.services.post_flow import PostFlowService
from bot_posts_linkedin.services.post_generator import ClaudePostGenerator
from bot_posts_linkedin.services.task_queue import GoogleCloudTasksClient
from bot_posts_linkedin.services.update_dedup import FirestoreUpdateDedupStore
from bot_posts_linkedin.store.chat_state_firestore import FirestoreChatStateStore
from bot_posts_linkedin.store.firestore import FirestorePostStore
from bot_posts_linkedin.telegram.client import HttpxTelegramClient
from bot_posts_linkedin.telegram.webhook import router as telegram_router
from bot_posts_linkedin.telegram.worker import router as worker_router

logger = logging.getLogger(__name__)


def _configure_logging() -> None:
    """Configura root logger pra emitir nossos `logger.info(...)` no stdout.

    Sem isso, em Cloud Run TODOS os logs do nosso código Python (services,
    handlers) são silenciados — só uvicorn e exceptions não-capturadas viram log.

    Formato inclui `trace_id` injetado pelo TraceContextMiddleware — quando
    presente, Cloud Logging agrupa todas as log lines do mesmo request,
    facilitando debug de fluxos longos (webhook → worker → publish).
    """
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s [trace=%(trace_id)s] %(name)s: %(message)s",
        stream=sys.stdout,
        force=True,  # sobrescreve config anterior (uvicorn pode ter setado algo)
    )
    install_trace_log_filter(logging.getLogger())


def create_app(
    *,
    post_flow: PostFlowService | None = None,
    update_dedup=None,  # type: ignore[no-untyped-def]
) -> FastAPI:
    """Factory do app.

    Em prod/dev (sem argumento): wire-up padrão com httpx + stores em memória,
    montado no lifespan + fail-fast nas credenciais de Replicate e GCS.
    Em testes: passa um PostFlowService já montado com fakes — vai direto pro
    app.state, sem depender do lifespan rodar (ASGITransport não roda).
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _configure_logging()
        settings = get_settings()
        app.state.settings = settings
        logger.info("Boot iniciado — env=%s, log_level=%s", settings.env, settings.log_level)

        if app.state.post_flow is None:
            telegram_client = HttpxTelegramClient(settings.telegram_bot_token)
            anthropic_client = HttpxAnthropicClient(
                api_key=settings.anthropic_api_key,
                model=settings.anthropic_model,
                web_search_max_uses=settings.web_search_max_uses,
            )
            github_search = GithubApiSearch(
                token=settings.github_token,
                username=settings.github_username,
                anthropic_client=anthropic_client,
            )
            post_generator = ClaudePostGenerator(
                anthropic_client=anthropic_client,
                system_prompt_path=settings.post_generation_system_prompt_path,
            )
            replicate_image = HttpxReplicateImageService(
                api_token=settings.replicate_api_token,
                model=settings.replicate_image_model,
                timeout_seconds=settings.replicate_timeout_seconds,
            )
            gcs_image = GoogleCloudStorageImpl(
                bucket_name=settings.gcs_bucket_name,
                signed_url_ttl_minutes=settings.gcs_signed_url_ttl_minutes,
                # Dev local: usa JSON da SA (tem private key, consegue assinar URLs).
                # Em Cloud Run essa var é None — o metadata server faz o signing via IAM.
                credentials_path=settings.google_application_credentials,
            )
            linkedin_publisher = HttpxLinkedInPublisher(
                access_token=settings.linkedin_access_token,
                person_urn=settings.linkedin_person_urn,
                api_version=settings.linkedin_api_version,
                dry_run=settings.linkedin_dry_run,
            )

            # Fail-fast no boot — credenciais Replicate, GCS e LinkedIn confirmadas
            # antes de servir requests. Erro aqui = container reinicia (Cloud Run) ou
            # `make dev` para com mensagem clara.
            await _validate_external_credentials(
                replicate_image, gcs_image, linkedin_publisher
            )

            # Log claro do modo de publicação — visível no Cloud Logging / `make dev`.
            mode = (
                "DRY-RUN (simulado)"
                if settings.linkedin_dry_run
                else "REAL (publica de verdade)"
            )
            logger.info("LinkedIn publication mode: %s", mode)

            # G.2: Firestore real em prod. Posts e chat_states sobrevivem a
            # restart do Cloud Run (era a limitação central do InMemory).
            post_store = FirestorePostStore(
                project_id=settings.gcp_project_id,
                collection=settings.firestore_collection_posts,
            )
            chat_state_store = FirestoreChatStateStore(
                project_id=settings.gcp_project_id,
                collection=settings.firestore_collection_chat_states,
            )

            # G.3: Cloud Tasks pra geração/publicação + dedup de update_id no webhook.
            worker_url = _build_worker_url(settings)
            sa_email = f"bot-posts-prod@{settings.gcp_project_id}.iam.gserviceaccount.com"
            task_queue = GoogleCloudTasksClient(
                project_id=settings.gcp_project_id,
                region=settings.gcp_region,
                queue=settings.cloud_tasks_queue,
                worker_url=worker_url,
                oidc_service_account_email=sa_email,
            )
            app.state.update_dedup = FirestoreUpdateDedupStore(
                project_id=settings.gcp_project_id,
                collection=settings.firestore_collection_processed_updates,
                ttl_minutes=settings.processed_updates_ttl_minutes,
            )

            app.state.post_flow = PostFlowService(
                post_store=post_store,
                chat_state_store=chat_state_store,
                telegram_client=telegram_client,
                anthropic_client=anthropic_client,
                github_search=github_search,
                post_generator=post_generator,
                replicate_image=replicate_image,
                gcs_image=gcs_image,
                linkedin_publisher=linkedin_publisher,
                task_queue=task_queue,
                settings=settings,
            )
            app.state.telegram_client = telegram_client
            app.state.anthropic_client = anthropic_client
            app.state.replicate_image = replicate_image
            app.state.linkedin_publisher = linkedin_publisher

        yield

        # Cleanup defensivo — getattr porque testes não setam esses atributos.
        for attr in (
            "telegram_client",
            "anthropic_client",
            "replicate_image",
            "linkedin_publisher",
        ):
            client = getattr(app.state, attr, None)
            if client is not None:
                await client.close()

    app = FastAPI(title="bot-posts-linkedin", version="0.1.0", lifespan=lifespan)
    # Pré-popula o state: testes passam post_flow=fake e pulam o branch real.
    app.state.post_flow = post_flow
    app.state.update_dedup = update_dedup
    app.state.telegram_client = None
    app.state.anthropic_client = None
    app.state.replicate_image = None
    app.state.linkedin_publisher = None

    # Sec hardening (Tier 1): security headers, host allowlist, request size,
    # server fingerprint hidden, trace correlation pra Cloud Logging.
    settings_ = get_settings()
    install_security_middlewares(app, app_base_url=settings_.app_base_url)

    # Sec-11 (Tier 2): monta apenas os routers do role atual. Em prod com split,
    # public service só tem /telegram/* (ingress=all, exposto); worker service só
    # tem /internal/* (ingress=internal, só GCP fala). Healthz fica nos dois pra
    # Cloud Run uptime check.
    role = settings_.role
    if role in ("all", "public"):
        app.include_router(telegram_router)
    if role in ("all", "worker"):
        app.include_router(worker_router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "env": get_settings().env}

    return app


def _build_worker_url(settings) -> str:  # type: ignore[no-untyped-def]
    """URL completa do worker — usada como `http_request.url` no Cloud Tasks.

    Em prod, app_base_url precisa ser a URL pública do Cloud Run (setada após o
    primeiro deploy via scripts/gcp_deploy.sh). Falha cedo se for localhost,
    porque o Cloud Tasks NÃO consegue chamar localhost.

    Sec-11: quando worker é service separado (ingress=internal), worker_base_url
    aponta pra URL do worker service. Cloud Tasks consegue chamar ingress=internal
    porque sai de dentro do GCP (não é tráfego de internet pública).
    """
    # Preferência: worker_base_url > app_base_url (modo monolito).
    base = (settings.worker_base_url or settings.app_base_url or "").rstrip("/")
    if not base or base.startswith("http://localhost"):
        raise RuntimeError(
            "WORKER_BASE_URL/APP_BASE_URL não configurado com URL pública do Cloud Run. "
            "Após o primeiro deploy, rode scripts/gcp_deploy.sh de novo pra setar."
        )
    return f"{base}{settings.cloud_tasks_worker_path}"


async def _validate_external_credentials(
    replicate_image: HttpxReplicateImageService,
    gcs_image: GoogleCloudStorageImpl,
    linkedin_publisher: HttpxLinkedInPublisher,
) -> None:
    """Valida Replicate + GCS + LinkedIn no boot. Falhas viram exceptions claras."""
    try:
        await replicate_image.validate_credentials()
        logger.info("Replicate credentials OK")
    except Exception as exc:
        raise RuntimeError(
            f"FAIL-FAST: credencial Replicate inválida — {type(exc).__name__}: {exc}"
        ) from exc
    try:
        await gcs_image.validate_bucket()
        logger.info("GCS bucket OK")
    except Exception as exc:
        raise RuntimeError(
            f"FAIL-FAST: bucket GCS inacessível — {type(exc).__name__}: {exc}"
        ) from exc
    try:
        await linkedin_publisher.validate_credentials()
        logger.info("LinkedIn credentials OK")
    except Exception as exc:
        raise RuntimeError(
            f"FAIL-FAST: credencial LinkedIn inválida — {type(exc).__name__}: {exc}. "
            "Token tem vida útil de 60 dias — talvez precise renovar."
        ) from exc


# App padrão usado pelo uvicorn no `make dev`.
app = create_app()
