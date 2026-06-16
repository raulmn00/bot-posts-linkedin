"""Testes do endpoint /internal/process-task (Fase G.3).

Sem OIDC real (precisaria certs do Google) — testamos o caminho de erro (401 sem
token, 401 com token mal-formado, 503 se APP_BASE_URL não configurado).
O happy path com OIDC real é coberto via smoke manual em prod.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from bot_posts_linkedin.config import get_settings
from bot_posts_linkedin.main import create_app
from bot_posts_linkedin.services.post_flow import PostFlowService
from bot_posts_linkedin.store.chat_state_memory import InMemoryChatStateStore
from bot_posts_linkedin.store.memory import InMemoryPostStore
from tests.fakes import (
    FakeAnthropicClient,
    FakeGcsImageStorage,
    FakeGithubSearchService,
    FakeLinkedInPublisher,
    FakePostGenerator,
    FakeReplicateImageService,
    FakeTaskQueueClient,
    FakeTelegramClient,
    FakeUpdateDedupStore,
)


@pytest.fixture
def app_with_fakes():
    """Mesma fixture do webhook — replicada pra independência do test file."""
    settings = get_settings()
    telegram = FakeTelegramClient()
    posts = InMemoryPostStore()
    chats = InMemoryChatStateStore()
    anthropic = FakeAnthropicClient()
    github = FakeGithubSearchService()
    post_gen = FakePostGenerator()
    replicate = FakeReplicateImageService()
    gcs = FakeGcsImageStorage()
    linkedin = FakeLinkedInPublisher(dry_run=False)
    task_queue = FakeTaskQueueClient()
    dedup = FakeUpdateDedupStore()
    flow = PostFlowService(
        post_store=posts,
        chat_state_store=chats,
        telegram_client=telegram,
        anthropic_client=anthropic,
        github_search=github,
        post_generator=post_gen,
        replicate_image=replicate,
        gcs_image=gcs,
        linkedin_publisher=linkedin,
        task_queue=task_queue,
        settings=settings,
    )
    task_queue.set_dispatch_target(flow)
    app = create_app(post_flow=flow, update_dedup=dedup)
    return app, flow, settings


@pytest.mark.asyncio
async def test_worker_sem_app_base_url_retorna_503(app_with_fakes) -> None:
    """Em dev local (app_base_url=localhost), o worker é desabilitado pra
    evitar exposição sem proteção."""
    app, _flow, _settings = app_with_fakes
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/internal/process-task",
            json={"action": "run_generation", "payload": {}},
            headers={"Authorization": "Bearer something"},
        )
    # APP_BASE_URL em dev é localhost → 503
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_worker_sem_token_retorna_401(app_with_fakes, monkeypatch) -> None:
    """Quando APP_BASE_URL está setado mas Authorization header falta."""
    # Monkey-patch a config pra simular APP_BASE_URL público (sem isso é 503).
    app, _flow, _settings = app_with_fakes
    monkeypatch.setattr(
        _settings, "app_base_url", "https://example.run.app", raising=False
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/internal/process-task",
            json={"action": "run_generation", "payload": {}},
        )
    # Sem header = 401
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_worker_token_invalido_retorna_401(app_with_fakes, monkeypatch) -> None:
    app, _flow, _settings = app_with_fakes
    monkeypatch.setattr(
        _settings, "app_base_url", "https://example.run.app", raising=False
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/internal/process-task",
            json={"action": "run_generation", "payload": {}},
            headers={"Authorization": "Bearer not_a_valid_oidc_token"},
        )
    assert r.status_code == 401
