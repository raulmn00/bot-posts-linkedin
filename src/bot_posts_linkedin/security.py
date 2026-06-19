"""Camada de segurança HTTP — Tier 1 da hardening.

Concentra todos os middlewares custom em um lugar só pra que `main.py`
fique responsável apenas pelo wire-up de dependências. Cada middleware
documenta o ataque que mitiga.
"""

import contextvars
import logging
import re
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)

# Contexto da request — usado pra carregar o trace_id pros logs.
trace_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")

# Limite generoso pra webhook do Telegram. Maior update do Telegram observado:
# ~50KB (mensagem grande com forward). 1MB cobre folgado e bloqueia ataques
# de DoS via payload imenso antes do uvicorn nem parsear.
_MAX_REQUEST_BYTES = 1024 * 1024  # 1MB

# Padrão pra extrair o trace ID do header X-Cloud-Trace-Context.
# Formato do GCP LB: `TRACE_ID/SPAN_ID;o=TRACE_TRUE`.
_TRACE_HEADER_RE = re.compile(r"^([a-f0-9]+)(?:/.*)?$")


# ============================================================================
# Middleware: limite de tamanho da request
# ============================================================================


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Rejeita 413 se Content-Length excede o limite.

    Mitigação: ataques de DoS via payload imenso. Mesmo que o app não use
    todo o body, parse + alocação consomem memória.
    """

    def __init__(self, app: FastAPI, max_bytes: int = _MAX_REQUEST_BYTES) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_bytes:
                    logger.warning(
                        "request bloqueada por excesso de tamanho: %s bytes > %s",
                        content_length, self._max_bytes,
                    )
                    return Response(status_code=413, content=b"")
            except ValueError:
                # Content-Length malformado — rejeita defensivamente.
                return Response(status_code=400, content=b"")
        return await call_next(request)


# ============================================================================
# Middleware: strip do header Server (não vaza versão do uvicorn/FastAPI)
# ============================================================================


class StripServerHeaderMiddleware(BaseHTTPMiddleware):
    """Remove o header `Server` da response.

    Mitigação: scanners automatizados (ex: Shodan) indexam servidores pela
    versão exata exposta. Esconder a versão não bloqueia ataque dirigido mas
    elimina o bot de listas oportunistas que filtram por uvicorn-X.Y.Z.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        if "server" in response.headers:
            del response.headers["server"]
        return response


# ============================================================================
# Middleware: trace correlation (Cloud Logging agrupa por trace_id)
# ============================================================================


class TraceContextMiddleware(BaseHTTPMiddleware):
    """Extrai `X-Cloud-Trace-Context` e bind no contextvar global.

    O Cloud Run LB injeta esse header em toda request. Logando o trace_id
    junto com a mensagem, o Cloud Logging agrupa automaticamente todas as
    log lines do mesmo request — útil pra debugar fluxos longos (worker +
    publicação) que envolvem várias chamadas API.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        raw = request.headers.get("x-cloud-trace-context", "")
        trace_id = ""
        if raw:
            m = _TRACE_HEADER_RE.match(raw)
            if m:
                trace_id = m.group(1)
        token = trace_id_ctx.set(trace_id)
        try:
            return await call_next(request)
        finally:
            trace_id_ctx.reset(token)


class _TraceLogFilter(logging.Filter):
    """Injeta `trace_id` em cada LogRecord pra o formatter usar."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_ctx.get() or "-"
        return True


def install_trace_log_filter(root_logger: logging.Logger) -> None:
    """Anexa o filter que injeta trace_id em todos os handlers do root."""
    flt = _TraceLogFilter()
    for handler in root_logger.handlers:
        handler.addFilter(flt)


# ============================================================================
# Helpers de allowlist
# ============================================================================


def derive_allowed_hosts(app_base_url: str) -> list[str]:
    """Constrói allowlist do TrustedHostMiddleware a partir da URL pública.

    Em prod, o app_base_url é a URL do Cloud Run (ex: bot-posts-linkedin-xxx-rj.a.run.app).
    Em dev local, devolve uma allowlist permissiva — TrustedHostMiddleware
    aceita coringa só com `*` ou subdomínios `*.domain`, então pra dev
    devolvemos wildcards pra localhost variants.
    """
    parsed = urlparse(app_base_url) if app_base_url else None
    host = parsed.hostname if parsed and parsed.hostname else ""

    # Dev local: aceita qualquer Host (testes ASGITransport usam "test", uvicorn local
    # usa "127.0.0.1" ou "localhost"). `*` é o wildcard total do Starlette.
    if not host or host in {"localhost", "127.0.0.1"}:
        return ["*"]

    return [host, "localhost", "127.0.0.1", "testserver"]


# ============================================================================
# Setup unificado — chamado por main.py
# ============================================================================


def install_security_middlewares(app: FastAPI, app_base_url: str) -> None:
    """Aplica todos os middlewares de segurança HTTP em ordem.

    Ordem importa — Starlette aplica em ordem REVERSA da adição:
      1. TraceContext (mais externo, captura todo request)
      2. StripServerHeader (depois do response)
      3. RequestSizeLimit (cedo, antes do parse)
      4. Secweb security headers (depois do response)
      5. TrustedHost (cedo, antes de qualquer trabalho)
    """
    # Secweb: pacote ativamente mantido, implementa MDN/OWASP defaults.
    # Skip CSP detalhado — o bot não tem UI HTML, só responde JSON 200 ou erros.
    # Nota: pacote tem case-sensitive PascalCase (Secweb, não secweb).
    # XFrame: classe real é `XFrame`, NÃO `XFrameOptions` (esse é um Union type alias).
    from Secweb.ReferrerPolicy.ReferrerPolicyMiddleware import ReferrerPolicy  # noqa: PLC0415
    from Secweb.StrictTransportSecurity.StrictTransportSecurityMiddleware import (
        HSTS,  # noqa: PLC0415
    )
    from Secweb.XContentTypeOptions.XContentTypeOptionsMiddleware import (  # noqa: PLC0415
        XContentTypeOptions,
    )
    from Secweb.XFrameOptions.XFrameOptionsMiddleware import XFrame  # noqa: PLC0415

    # ORDEM REVERSA — o último adicionado é o primeiro executado.
    app.add_middleware(TraceContextMiddleware)
    app.add_middleware(StripServerHeaderMiddleware)
    app.add_middleware(RequestSizeLimitMiddleware)

    # Secweb security headers — API moderna (1.30+):
    #   ReferrerPolicy: lista de policy strings
    #   XFrame: string 'DENY' ou 'SAMEORIGIN' (default 'DENY')
    #   XContentTypeOptions: sem opção (sempre 'nosniff')
    #   HSTS: TypedDict
    app.add_middleware(ReferrerPolicy, Option=["strict-origin-when-cross-origin"])
    app.add_middleware(XFrame, Option="DENY")
    app.add_middleware(XContentTypeOptions)
    # HSTS forte: 1 ano, inclui subdomínios. Cloud Run já é HTTPS-only.
    app.add_middleware(
        HSTS,
        Option={"max-age": 31536000, "includeSubDomains": True, "preload": False},
    )

    # TrustedHost — primeiro a executar; rejeita Host header fora do allowlist.
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=derive_allowed_hosts(app_base_url),
    )
