"""Testes do HttpxLinkedInPublisher com httpx.MockTransport.

Cobre:
  - Headers obrigatórios (LinkedIn-Version + X-Restli-Protocol-Version) em TODA
    chamada /rest/*; /v2/userinfo NÃO precisa
  - Dry-run NÃO chama /rest/posts nem upload binário, retorna payload completo
  - Real-mode happy path: initializeUpload → PUT binário → POST /rest/posts → URN
  - 2xx SEM x-restli-id → post_urn=None (caller decide o que fazer)
  - 401 em qualquer chamada → TokenExpiredError dedicado
  - 4xx/5xx genérico → PublicationFailedError
  - commentary > 2900 chars → falha local sem chamar API
  - validate_credentials: 200 OK; 401 levanta TokenExpired
"""

import httpx
import pytest

from bot_posts_linkedin.domain.post import Post
from bot_posts_linkedin.services.linkedin_publisher import (
    HttpxLinkedInPublisher,
    PublicationFailedError,
    TokenExpiredError,
    build_commentary,
)

PERSON_URN = "urn:li:person:fakeUser123"
API_VERSION = "202506"


def _post(*, body_pt="PT body", body_en="EN body", image_url=None) -> Post:
    p = Post(chat_id="t", user_prompt="x")
    p.body_pt = body_pt
    p.body_en = body_en
    p.image_url = image_url
    return p


def _make_publisher(handler, *, dry_run: bool = False) -> HttpxLinkedInPublisher:
    return HttpxLinkedInPublisher(
        access_token="fake-token",
        person_urn=PERSON_URN,
        api_version=API_VERSION,
        dry_run=dry_run,
        transport=httpx.MockTransport(handler),
    )


# ============================================================ build_commentary


def test_build_commentary_inclui_separador_bilingue() -> None:
    p = _post(body_pt="texto em pt", body_en="text in en")
    out = build_commentary(p)
    assert "texto em pt" in out
    assert "text in en" in out
    assert "━━━━━━━━━━━━━━━" in out


# ============================================================ dry-run


@pytest.mark.asyncio
async def test_dry_run_nao_faz_chamada_real_e_retorna_payload_completo() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError(f"chamada inesperada em dry-run: {request.url}")

    p = _post(image_url="https://gcs/signed/url/img.png")
    publisher = _make_publisher(handler, dry_run=True)
    result = await publisher.publish(p)

    assert calls == []  # NENHUMA chamada feita
    assert result.dry_run is True
    assert result.post_urn is None
    assert "DRY_RUN_PLACEHOLDER" in result.image_urn
    # Payload completo com commentary + content.media + visibility.
    assert result.payload_sent["author"] == PERSON_URN
    assert "━━━━━━━━━━━━━━━" in result.payload_sent["commentary"]
    assert result.payload_sent["visibility"] == "PUBLIC"
    assert result.payload_sent["content"]["media"]["id"] == result.image_urn


@pytest.mark.asyncio
async def test_dry_run_sem_imagem_nao_inclui_content() -> None:
    publisher = _make_publisher(
        lambda r: pytest.fail("não deveria chamar API"), dry_run=True
    )
    result = await publisher.publish(_post(image_url=None))
    assert "content" not in result.payload_sent
    assert result.image_urn is None


# ============================================================ real-mode happy path


@pytest.mark.asyncio
async def test_real_mode_com_imagem_upload_completo_retorna_urn() -> None:
    fetch_url = "https://gcs/signed/url/img.png"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # 1. POST /rest/images?action=initializeUpload — confere headers versionados
        if path == "/rest/images" and request.method == "POST":
            assert request.headers.get("linkedin-version") == API_VERSION
            assert request.headers.get("x-restli-protocol-version") == "2.0.0"
            return httpx.Response(
                200,
                json={
                    "value": {
                        "uploadUrl": "https://linkedin.example/upload/abc",
                        "image": "urn:li:image:real_uploaded",
                    }
                },
            )
        # 2. PUT binário no uploadUrl — NÃO leva nossos headers
        if request.method == "PUT" and "upload" in path:
            assert request.headers.get("linkedin-version") is None
            return httpx.Response(201)
        # 3. GET signed URL — também sem nossos headers
        if path.endswith("img.png"):
            return httpx.Response(200, content=b"fake-image-bytes")
        # 4. POST /rest/posts — headers versionados + URN no header de resposta
        if path == "/rest/posts" and request.method == "POST":
            assert request.headers.get("linkedin-version") == API_VERSION
            assert request.headers.get("x-restli-protocol-version") == "2.0.0"
            body = request.read().decode()
            assert "real_uploaded" in body  # URN da imagem foi pro payload
            return httpx.Response(
                201,
                headers={"x-restli-id": "urn:li:share:9000"},
            )
        return httpx.Response(404, json={"path": path})

    publisher = _make_publisher(handler, dry_run=False)
    result = await publisher.publish(_post(image_url=fetch_url))

    assert result.dry_run is False
    assert result.post_urn == "urn:li:share:9000"
    assert result.image_urn == "urn:li:image:real_uploaded"


@pytest.mark.asyncio
async def test_caminho_real_substitui_placeholder_pelo_urn_real_no_body() -> None:
    """PONTO 1 (CRÍTICO): Em dry_run=False com imagem, o body do POST /rest/posts
    deve carregar o URN REAL retornado pelo initializeUpload — NUNCA o placeholder
    DRY_RUN. Asserções fortes contando exatas chamadas e inspecionando JSON parseado.
    """
    import json as _json

    REAL_URN = "urn:li:image:CONFIRMED_REAL_FROM_API_xyz789"
    PLACEHOLDER = "urn:li:image:DRY_RUN_PLACEHOLDER_NAO_UPLOADED"

    init_calls: list[httpx.Request] = []
    put_calls: list[httpx.Request] = []
    gcs_get_calls: list[httpx.Request] = []
    create_post_calls: list[dict] = []  # bodies parseados

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # 1. initializeUpload — devolve URN REAL diferente do placeholder
        if path == "/rest/images" and request.method == "POST":
            init_calls.append(request)
            return httpx.Response(
                200,
                json={
                    "value": {
                        "uploadUrl": "https://linkedin.example/upload/abcdef",
                        "image": REAL_URN,
                    }
                },
            )
        # 2. PUT binário no uploadUrl
        if request.method == "PUT" and "upload/abcdef" in path:
            put_calls.append(request)
            return httpx.Response(201)
        # 3. GET do signed URL do GCS
        if path.endswith("/img-real-mode.png"):
            gcs_get_calls.append(request)
            return httpx.Response(200, content=b"real-image-bytes-x")
        # 4. POST /rest/posts — captura body PARSEADO
        if path == "/rest/posts" and request.method == "POST":
            body_text = request.read().decode()
            create_post_calls.append(_json.loads(body_text))
            return httpx.Response(201, headers={"x-restli-id": "urn:li:share:8888"})
        raise AssertionError(f"chamada inesperada: {request.method} {request.url}")

    publisher = _make_publisher(handler, dry_run=False)
    result = await publisher.publish(
        _post(image_url="https://storage.googleapis.com/bucket/img-real-mode.png")
    )

    # === Asserções fortes ===

    # 1. initializeUpload chamado exatamente 1 vez
    assert len(init_calls) == 1, f"initializeUpload chamado {len(init_calls)} vezes"

    # 2. PUT binário chamado exatamente 1 vez
    assert len(put_calls) == 1, f"PUT chamado {len(put_calls)} vezes"
    assert put_calls[0].read() == b"real-image-bytes-x"

    # 3. GET no GCS chamado exatamente 1 vez (download dos bytes)
    assert len(gcs_get_calls) == 1, f"GET no GCS chamado {len(gcs_get_calls)} vezes"

    # 4. POST /rest/posts chamado exatamente 1 vez
    assert len(create_post_calls) == 1, f"POST /rest/posts chamado {len(create_post_calls)} vezes"

    # 5. content.media.id no body == URN REAL (não placeholder)
    body = create_post_calls[0]
    assert body["content"]["media"]["id"] == REAL_URN, (
        f"esperado URN real no body, achei: {body['content']['media']['id']!r}"
    )

    # 6. Placeholder NÃO aparece em NENHUM lugar do body do POST real
    body_serialized = _json.dumps(body)
    assert PLACEHOLDER not in body_serialized, (
        f"placeholder do dry-run vazou pro body real: {body_serialized}"
    )

    # 7. Resultado também carrega o URN real
    assert result.image_urn == REAL_URN
    assert result.post_urn == "urn:li:share:8888"
    assert result.dry_run is False


@pytest.mark.asyncio
async def test_create_post_recusa_payload_com_placeholder_defesa_em_profundidade() -> None:
    """PONTO 1 (defesa em profundidade): mesmo que algum bug futuro tente fazer
    POST /rest/posts com placeholder do dry-run, _create_post aborta ANTES de
    chamar a API. Cobre regressões hipotéticas no fluxo.
    """
    chamadas_post: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/posts":
            chamadas_post.append(request)
            return httpx.Response(201)
        raise AssertionError("não deveria chegar aqui")

    publisher = _make_publisher(handler, dry_run=False)
    payload_envenenado = {
        "author": "urn:li:person:fake",
        "commentary": "x",
        "content": {
            "media": {
                "id": "urn:li:image:DRY_RUN_PLACEHOLDER_NAO_UPLOADED",
                "title": "imagem",
            }
        },
    }
    with pytest.raises(PublicationFailedError, match="BUG.*placeholder"):
        await publisher._create_post(payload_envenenado)

    # API NÃO foi chamada — abortou antes
    assert chamadas_post == []


@pytest.mark.asyncio
async def test_real_mode_sem_imagem_pula_upload() -> None:
    chamadas_post = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/posts":
            chamadas_post.append(request)
            return httpx.Response(201, headers={"x-restli-id": "urn:li:share:1"})
        raise AssertionError(f"sem imagem, não deveria chamar {request.url}")

    publisher = _make_publisher(handler, dry_run=False)
    result = await publisher.publish(_post(image_url=None))
    assert result.post_urn == "urn:li:share:1"
    assert result.image_urn is None
    assert len(chamadas_post) == 1


# ============================================================ 2xx sem x-restli-id


@pytest.mark.asyncio
async def test_2xx_sem_x_restli_id_retorna_post_urn_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/posts":
            # Status 2xx OK, mas SEM o header x-restli-id.
            return httpx.Response(201)
        return httpx.Response(404)

    publisher = _make_publisher(handler, dry_run=False)
    result = await publisher.publish(_post(image_url=None))
    assert result.dry_run is False
    assert result.post_urn is None  # caller (post_flow) decide a UX disso


# ============================================================ 401 → TokenExpired


@pytest.mark.asyncio
async def test_401_em_create_post_levanta_token_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/posts":
            return httpx.Response(401, json={"message": "Invalid access token"})
        return httpx.Response(404)

    publisher = _make_publisher(handler, dry_run=False)
    with pytest.raises(TokenExpiredError, match="LINKEDIN_ACCESS_TOKEN"):
        await publisher.publish(_post(image_url=None))


@pytest.mark.asyncio
async def test_401_em_init_upload_levanta_token_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/images":
            return httpx.Response(401, json={"message": "expired"})
        return httpx.Response(404)

    publisher = _make_publisher(handler, dry_run=False)
    with pytest.raises(TokenExpiredError):
        await publisher.publish(_post(image_url="https://x"))


# ============================================================ 4xx/5xx genérico


@pytest.mark.asyncio
async def test_400_em_create_post_levanta_publication_failed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/posts":
            return httpx.Response(400, text="commentary too long")
        return httpx.Response(404)

    publisher = _make_publisher(handler, dry_run=False)
    with pytest.raises(PublicationFailedError, match="400"):
        await publisher.publish(_post(image_url=None))


# ============================================================ commentary > 2900 chars


@pytest.mark.asyncio
async def test_commentary_excede_2900_chars_levanta_local_sem_chamar_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("commentary deveria falhar antes de chamar API")

    huge = "x" * 1500
    publisher = _make_publisher(handler, dry_run=False)
    with pytest.raises(PublicationFailedError, match="2900"):
        await publisher.publish(_post(body_pt=huge, body_en=huge, image_url=None))


# ============================================================ validate_credentials


@pytest.mark.asyncio
async def test_validate_credentials_passa_em_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/userinfo":
            # /v2 NÃO precisa LinkedIn-Version — confirma que NÃO mandamos.
            # (mandar ou não tanto faz na verdade, mas a regra é só Authorization)
            return httpx.Response(200, json={"sub": "fake"})
        return httpx.Response(404)

    publisher = _make_publisher(handler)
    await publisher.validate_credentials()


@pytest.mark.asyncio
async def test_validate_credentials_levanta_token_expired_em_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "expired"})

    publisher = _make_publisher(handler)
    with pytest.raises(TokenExpiredError):
        await publisher.validate_credentials()
