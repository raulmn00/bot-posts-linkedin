"""Endpoint POST /internal/process-task — consumido pelo Cloud Tasks (Fase G.3).

Cloud Tasks faz HTTP POST aqui com:
  - Header `Authorization: Bearer <OIDC token assinado pela SA>`
  - Body JSON `{"action": str, "payload": dict}`

Validação:
  1. Verifica que o token é um OIDC válido emitido pelo Google
  2. Audience bate com o esperado (URL do worker)
  3. Email do SA bate com a SA configurada

Endpoint `--allow-unauthenticated` no Cloud Run mas a barreira real é o OIDC —
sem token válido vira 401, mesmo que alguém descubra a URL.

Em dev local, o worker geralmente não é chamado direto — FakeTaskQueueClient
executa síncrono. Mas se quiser testar a auth, vale ter um curl no smoke.
"""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from google.auth.transport import requests as gauth_requests
from google.oauth2 import id_token

from bot_posts_linkedin.config import Settings, get_settings
from bot_posts_linkedin.services.post_flow import PostFlowService

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_post_flow(request: Request) -> PostFlowService:
    """Injetado via app.state — testes podem fazer override."""
    service = getattr(request.app.state, "post_flow", None)
    if service is None:
        raise RuntimeError("PostFlowService não inicializado")
    return service


def _verify_oidc_token(
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """Valida o OIDC token enviado pelo Cloud Tasks.

    Esperado: header `Authorization: Bearer <jwt>` onde jwt é um Google-signed
    ID token com audience = URL do worker + email = SA do app.

    Em dev/test, `cloud_tasks_oidc_audience` pode estar vazio — nesse caso a
    rota é desabilitada (retorna 503) pra não ficar acessível sem proteção.
    """
    expected_audience = _expected_audience(settings)
    if not expected_audience:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="worker desabilitado — APP_BASE_URL não configurado",
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token")

    token = authorization.removeprefix("Bearer ").strip()
    try:
        payload = id_token.verify_oauth2_token(
            token, gauth_requests.Request(), audience=expected_audience
        )
    except ValueError as exc:
        logger.warning("OIDC token rejeitado: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid OIDC token"
        ) from exc

    # Confere que o emissor é o SA que esperamos (defesa em profundidade).
    expected_email = _expected_invoker_email(settings)
    actual_email = payload.get("email")
    if expected_email and actual_email != expected_email:
        logger.warning(
            "OIDC email mismatch: esperado=%s recebido=%s", expected_email, actual_email
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="unexpected invoker"
        )


def _expected_audience(settings: Settings) -> str:
    """URL completa do worker — usada como audience do OIDC."""
    base = (settings.app_base_url or "").rstrip("/")
    if not base or base.startswith("http://localhost"):
        return ""
    return f"{base}{settings.cloud_tasks_worker_path}"


def _expected_invoker_email(settings: Settings) -> str:
    """Email da SA esperada como invoker. Vazio em dev local (não valida email)."""
    if not settings.gcp_project_id or settings.env != "prod":
        return ""
    return f"bot-posts-prod@{settings.gcp_project_id}.iam.gserviceaccount.com"


@router.post("/internal/process-task", dependencies=[Depends(_verify_oidc_token)])
async def process_task(
    request: Request,
    flow: Annotated[PostFlowService, Depends(_get_post_flow)],
) -> Response:
    """Processa uma task enfileirada pelo Cloud Tasks.

    Idempotência: cada handler interno do post_flow já é idempotente
    (handle_approval cobre clique duplo, _run_publish guarda contra status
    inesperado, etc.) — Cloud Tasks retry seguro.
    """
    body: dict[str, Any] = await request.json()
    action = body.get("action")
    payload = body.get("payload") or {}

    if not action or not isinstance(action, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="action obrigatório (string)",
        )

    logger.info("process-task action=%s payload_keys=%s", action, list(payload.keys()))
    await flow.dispatch_task(action, payload)
    return Response(status_code=status.HTTP_200_OK)
