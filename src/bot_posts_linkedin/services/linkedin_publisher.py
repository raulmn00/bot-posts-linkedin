"""Cliente do LinkedIn REST API — publicação de posts.

Dois modos:
  - dry_run=True (default no .env): monta payload completo, mas NÃO chama
    POST /rest/posts. Retorna PublicationResult(dry_run=True) pra inspeção.
    Imagem NÃO é uploaded — usa placeholder no payload pra evitar side effect
    mesmo em modo seguro (LinkedIn manteria uma imagem solta no nosso "asset
    bucket" deles, ainda que não publicada).
  - dry_run=False: faz upload da imagem + cria post real no perfil.

Fluxo real (3 chamadas, todas com headers versionados):
  1. POST /rest/images?action=initializeUpload → uploadUrl + URN da imagem
  2. PUT bytes da imagem no uploadUrl (URL pre-signed externa — sem headers)
  3. POST /rest/posts com commentary + content.media (URN da imagem)
  4. Header x-restli-id na resposta carrega o URN do post criado.
     PODE estar ausente em alguns 2xx — caller decide o que fazer.

Headers OBRIGATÓRIOS em qualquer chamada /rest/* — faltar é causa nº1 de 426/400:
  - LinkedIn-Version: {LINKEDIN_API_VERSION configurada via env}
  - X-Restli-Protocol-Version: 2.0.0
  - Authorization: Bearer {access_token}

Notas:
  - /v2/userinfo é da API antiga (não-versionada) e usa só Authorization.
  - LinkedIn aposenta versões a cada ~12 meses; mudar LINKEDIN_API_VERSION no
    env evita redeploy de código.
"""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from bot_posts_linkedin.domain.post import Post

_USERINFO_PATH = "/v2/userinfo"
_INIT_UPLOAD_PATH = "/rest/images?action=initializeUpload"
_POSTS_PATH = "/rest/posts"
_DRY_RUN_IMAGE_URN_PLACEHOLDER = "urn:li:image:DRY_RUN_PLACEHOLDER_NAO_UPLOADED"

# Margem sob o limite oficial de 3000 chars do commentary — separador + emojis
# de bandeira somam alguns chars; queremos folga pra evitar 400.
_COMMENTARY_MAX_CHARS = 2900


@dataclass(frozen=True)
class PublicationResult:
    dry_run: bool
    payload_sent: dict[str, Any]  # JSON exato que foi (ou seria) postado em /rest/posts
    post_urn: str | None  # None em dry-run OU em 2xx-sem-x-restli-id
    image_urn: str | None  # URN real OU placeholder em dry-run


class TokenExpiredError(RuntimeError):
    """LinkedIn retornou 401 — access_token expirou (vida útil 60 dias)."""


class PublicationFailedError(RuntimeError):
    """LinkedIn retornou 4xx/5xx genérico, ou commentary local excedeu o limite."""


@runtime_checkable
class LinkedInPublisher(Protocol):
    async def publish(self, post: Post) -> PublicationResult: ...

    async def validate_credentials(self) -> None: ...

    async def close(self) -> None: ...


def build_commentary(post: Post) -> str:
    """Monta o texto bilíngue do `commentary`.

    Usa o mesmo separador `━━━━━━━━━━━━━━━` que aparece no Telegram —
    preserva a intenção visual original do bilíngue em mensagem única.
    """
    body_pt = post.body_pt or ""
    body_en = post.body_en or ""
    return f"{body_pt}\n\n━━━━━━━━━━━━━━━\n\n{body_en}"


class HttpxLinkedInPublisher:
    _BASE_URL = "https://api.linkedin.com"

    def __init__(
        self,
        *,
        access_token: str,
        person_urn: str,
        api_version: str,
        dry_run: bool,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._person_urn = person_urn
        self._dry_run = dry_run
        self._api_version = api_version
        # Transport é compartilhado entre o cliente principal E os sub-clients que
        # fazem GET da imagem do GCS / PUT no uploadUrl. Em prod é None (httpx
        # usa default); em testes é MockTransport — sem isso, sub-clients
        # tentariam rede de verdade e quebrariam.
        self._transport = transport
        self._client = httpx.AsyncClient(
            base_url=self._BASE_URL,
            timeout=30.0,
            transport=transport,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def _versioned_headers(self) -> dict[str, str]:
        # Aplicados em TODA chamada /rest/*. /v2/userinfo NÃO precisa.
        return {
            "LinkedIn-Version": self._api_version,
            "X-Restli-Protocol-Version": "2.0.0",
        }

    async def publish(self, post: Post) -> PublicationResult:
        commentary = build_commentary(post)
        if len(commentary) > _COMMENTARY_MAX_CHARS:
            raise PublicationFailedError(
                f"commentary excede {_COMMENTARY_MAX_CHARS} chars: {len(commentary)}"
            )

        image_urn: str | None = None
        if post.image_url:
            if self._dry_run:
                # Não sobe imagem real — evita side effect mesmo em modo seguro.
                image_urn = _DRY_RUN_IMAGE_URN_PLACEHOLDER
            else:
                image_urn = await self._upload_image(post.image_url)

        payload = _build_post_payload(
            author_urn=self._person_urn,
            commentary=commentary,
            image_urn=image_urn,
        )

        if self._dry_run:
            return PublicationResult(
                dry_run=True,
                payload_sent=payload,
                post_urn=None,
                image_urn=image_urn,
            )

        post_urn = await self._create_post(payload)
        return PublicationResult(
            dry_run=False,
            payload_sent=payload,
            post_urn=post_urn,
            image_urn=image_urn,
        )

    async def _upload_image(self, signed_url: str) -> str:
        # 1. Inicializa upload — recebe uploadUrl pre-signed + URN da imagem
        init_body = {"initializeUploadRequest": {"owner": self._person_urn}}
        r = await self._client.post(
            _INIT_UPLOAD_PATH,
            json=init_body,
            headers=self._versioned_headers(),
        )
        _check_response(r, context="initializeUpload")
        data = r.json().get("value", {})
        upload_url = data.get("uploadUrl")
        image_urn = data.get("image")
        if not upload_url or not image_urn:
            raise PublicationFailedError(
                f"initializeUpload OK mas sem uploadUrl/image: {data!r}"
            )

        # 2. Baixa a imagem do GCS signed URL (sub-cliente sem Authorization).
        # Compartilha self._transport pra MockTransport funcionar em tests.
        async with httpx.AsyncClient(timeout=30.0, transport=self._transport) as fetcher:
            img_resp = await fetcher.get(signed_url)
            img_resp.raise_for_status()
            image_bytes = img_resp.content

        # 3. PUT no uploadUrl. URL externa pre-signed — sem nossos headers.
        async with httpx.AsyncClient(timeout=60.0, transport=self._transport) as uploader:
            put_resp = await uploader.put(upload_url, content=image_bytes)
            if put_resp.status_code >= 400:
                snippet = put_resp.text[:200] if put_resp.text else "(sem body)"
                raise PublicationFailedError(
                    f"upload binário falhou: {put_resp.status_code} {snippet}"
                )

        return image_urn

    async def _create_post(self, payload: dict[str, Any]) -> str | None:
        # Defesa em profundidade: o placeholder do dry-run NUNCA pode chegar
        # num POST real. Se a estrutura do código mudar e abrir essa janela,
        # falha aqui antes de mandar pra API (que rejeitaria com 400 de qualquer
        # jeito, mas com erro confuso). Hoje o fluxo em publish() já garante isso.
        if _DRY_RUN_IMAGE_URN_PLACEHOLDER in str(payload):
            raise PublicationFailedError(
                "BUG: payload com placeholder de dry-run chegou no caminho real. "
                "Isso indica defeito de fluxo — abortando antes de chamar a API."
            )
        r = await self._client.post(
            _POSTS_PATH,
            json=payload,
            headers=self._versioned_headers(),
        )
        _check_response(r, context="createPost")
        # 2xx pode vir SEM x-restli-id (raro mas acontece). Retorna None —
        # caller decide marcar PUBLISHED com aviso "sem link" em vez de assumir.
        return r.headers.get("x-restli-id")

    async def validate_credentials(self) -> None:
        # /v2/userinfo é da API antiga e basta com Authorization (sem versioned headers).
        r = await self._client.get(_USERINFO_PATH)
        if r.status_code == 401:
            raise TokenExpiredError(
                "401 em /v2/userinfo — LINKEDIN_ACCESS_TOKEN precisa ser renovado"
            )
        r.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()


def _build_post_payload(
    *,
    author_urn: str,
    commentary: str,
    image_urn: str | None,
) -> dict[str, Any]:
    """Payload exato do POST /rest/posts."""
    payload: dict[str, Any] = {
        "author": author_urn,
        "commentary": commentary,
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if image_urn:
        payload["content"] = {
            "media": {
                "title": "imagem do post",
                "id": image_urn,
            }
        }
    return payload


def _check_response(r: httpx.Response, *, context: str) -> None:
    """Levanta TokenExpiredError em 401, PublicationFailedError em outros 4xx/5xx."""
    if r.status_code == 401:
        raise TokenExpiredError(
            f"401 em {context} — LINKEDIN_ACCESS_TOKEN precisa ser renovado"
        )
    if r.status_code >= 400:
        body = r.text[:500] if r.text else "(sem body)"
        raise PublicationFailedError(f"{context} status {r.status_code}: {body}")
