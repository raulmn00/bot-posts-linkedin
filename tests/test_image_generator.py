"""Testes do ReplicateImageService com httpx.MockTransport.

GCS (GoogleCloudStorageImpl) não é testado aqui porque depende de credenciais
reais — coberto via FakeGcsImageStorage em test_post_flow_full + smoke manual.
"""

import httpx
import pytest

from bot_posts_linkedin.services.image_generator import (
    HttpxReplicateImageService,
    ImageGenerationFailed,
    ImageTimeoutError,
)


def _make_handler(*responses: dict):
    """Sequência de respostas que cada chamada HTTP recebe.

    responses[0] = primeira chamada (POST /v1/models/.../predictions)
    responses[1+] = chamadas subsequentes (GET /v1/predictions/{id})
    """
    iterator = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        try:
            r = next(iterator)
        except StopIteration as e:
            raise AssertionError(f"chamada extra inesperada para {request.url}") from e
        return httpx.Response(r.get("status", 200), json=r["json"])

    return handler


def _make_service(transport, **kwargs) -> HttpxReplicateImageService:
    return HttpxReplicateImageService(
        api_token="t",
        model="black-forest-labs/flux-1.1-pro",
        timeout_seconds=kwargs.get("timeout_seconds", 60),
        poll_interval_seconds=kwargs.get("poll_interval_seconds", 0.01),
        transport=transport,
    )


@pytest.mark.asyncio
async def test_succeeded_retorna_url() -> None:
    transport = httpx.MockTransport(
        _make_handler(
            {"json": {"id": "abc", "status": "starting"}},
            {"json": {"id": "abc", "status": "processing"}},
            {"json": {"id": "abc", "status": "succeeded", "output": ["https://img/1.png"]}},
        )
    )
    service = _make_service(transport)
    url = await service.generate("um prompt")
    assert url == "https://img/1.png"
    await service.close()


@pytest.mark.asyncio
async def test_output_pode_ser_string_simples() -> None:
    transport = httpx.MockTransport(
        _make_handler(
            {"json": {"id": "abc", "status": "starting"}},
            {"json": {"id": "abc", "status": "succeeded", "output": "https://img/2.png"}},
        )
    )
    service = _make_service(transport)
    url = await service.generate("prompt")
    assert url == "https://img/2.png"


@pytest.mark.asyncio
async def test_status_failed_levanta() -> None:
    transport = httpx.MockTransport(
        _make_handler(
            {"json": {"id": "abc", "status": "starting"}},
            {"json": {"id": "abc", "status": "failed", "error": "modelo travou"}},
        )
    )
    service = _make_service(transport)
    with pytest.raises(ImageGenerationFailed, match="modelo travou"):
        await service.generate("prompt")


@pytest.mark.asyncio
async def test_timeout_levanta() -> None:
    # Polling com timeout muito curto + statuses "processing" eternos.
    transport = httpx.MockTransport(
        _make_handler(
            {"json": {"id": "abc", "status": "starting"}},
            *[{"json": {"id": "abc", "status": "processing"}} for _ in range(50)],
        )
    )
    service = _make_service(transport, timeout_seconds=1, poll_interval_seconds=0.05)
    with pytest.raises(ImageTimeoutError, match="excedeu"):
        await service.generate("prompt")


@pytest.mark.asyncio
async def test_output_vazio_levanta() -> None:
    transport = httpx.MockTransport(
        _make_handler(
            {"json": {"id": "abc", "status": "starting"}},
            {"json": {"id": "abc", "status": "succeeded", "output": None}},
        )
    )
    service = _make_service(transport)
    with pytest.raises(ImageGenerationFailed, match="output inesperado"):
        await service.generate("prompt")


@pytest.mark.asyncio
async def test_validate_credentials_passa_com_200() -> None:
    transport = httpx.MockTransport(
        _make_handler({"json": {"type": "user", "username": "raul"}})
    )
    service = _make_service(transport)
    await service.validate_credentials()


@pytest.mark.asyncio
async def test_validate_credentials_levanta_em_401() -> None:
    transport = httpx.MockTransport(
        _make_handler({"status": 401, "json": {"detail": "Unauthorized"}})
    )
    service = _make_service(transport)
    with pytest.raises(httpx.HTTPStatusError):
        await service.validate_credentials()


def test_modelo_sem_owner_levanta() -> None:
    with pytest.raises(ValueError, match="owner/name"):
        HttpxReplicateImageService(api_token="t", model="sem-barra", timeout_seconds=60)
