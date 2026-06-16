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
    """App com todos os fakes plugados — zero IO externo."""
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
    return app, flow, telegram, posts, chats, settings


def _message_payload(chat_id: str | int, text: str, message_id: int = 1) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": message_id,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id},
            "text": text,
        },
    }


def _callback_payload(
    chat_id: str | int,
    data: str,
    cb_id: str = "cb1",
    message_id: int = 10,
) -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": cb_id,
            "from": {"id": chat_id},
            "data": data,
            "message": {
                "message_id": message_id,
                "chat": {"id": chat_id, "type": "private"},
            },
        },
    }


@pytest.mark.asyncio
async def test_webhook_sem_secret_retorna_401(app_with_fakes) -> None:
    app, *_ = app_with_fakes
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/telegram/webhook", json={})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_webhook_secret_errado_retorna_401(app_with_fakes) -> None:
    app, *_ = app_with_fakes
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post(
            "/telegram/webhook",
            json={},
            headers={"X-Telegram-Bot-Api-Secret-Token": "errado"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_webhook_chat_id_alheio_retorna_200_silent(app_with_fakes) -> None:
    app, _flow, telegram, posts, _chats, settings = app_with_fakes
    headers = {"X-Telegram-Bot-Api-Secret-Token": settings.telegram_webhook_secret}
    payload = _message_payload(chat_id="9999999999", text="[GERAR-POST] x")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/telegram/webhook", json=payload, headers=headers)
    assert r.status_code == 200
    # Nada disparado — silêncio absoluto.
    assert telegram.sent_messages == []


@pytest.mark.asyncio
async def test_webhook_comando_valido_dispara_handle_command(app_with_fakes) -> None:
    app, flow, telegram, _posts, _chats, settings = app_with_fakes
    headers = {"X-Telegram-Bot-Api-Secret-Token": settings.telegram_webhook_secret}
    payload = _message_payload(chat_id=settings.telegram_chat_id, text="[GERAR-POST] teste")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/telegram/webhook", json=payload, headers=headers)
    assert r.status_code == 200
    # Webhook responde 200 imediato; geração roda em background. Aguardar pra inspecionar.
    await flow.wait_pending()
    # Insumos + post de aprovação
    assert len(telegram.sent_messages) == 2
    assert "Insumos coletados" in telegram.sent_messages[0]["text"]
    assert "🇧🇷" in telegram.sent_messages[1]["text"]


@pytest.mark.asyncio
async def test_webhook_assunto_vazio_envia_help(app_with_fakes) -> None:
    app, _flow, telegram, _posts, _chats, settings = app_with_fakes
    headers = {"X-Telegram-Bot-Api-Secret-Token": settings.telegram_webhook_secret}
    payload = _message_payload(chat_id=settings.telegram_chat_id, text="[GERAR-POST]")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/telegram/webhook", json=payload, headers=headers)
    assert r.status_code == 200
    assert len(telegram.sent_messages) == 1
    assert "Como usar" in telegram.sent_messages[0]["text"]


@pytest.mark.asyncio
async def test_webhook_texto_livre_sem_pendente_e_silencioso(app_with_fakes) -> None:
    app, _flow, telegram, _posts, _chats, settings = app_with_fakes
    headers = {"X-Telegram-Bot-Api-Secret-Token": settings.telegram_webhook_secret}
    payload = _message_payload(chat_id=settings.telegram_chat_id, text="oi tudo bem")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/telegram/webhook", json=payload, headers=headers)
    assert r.status_code == 200
    assert telegram.sent_messages == []


@pytest.mark.asyncio
async def test_webhook_callback_approve_dispara_aprovacao(app_with_fakes) -> None:
    app, flow, telegram, posts, _chats, settings = app_with_fakes
    headers = {"X-Telegram-Bot-Api-Secret-Token": settings.telegram_webhook_secret}
    # Setup: comando primeiro pra ter um post pra aprovar.
    await flow.handle_command(settings.telegram_chat_id, "teste", use_github=False)
    await flow.wait_pending()
    from bot_posts_linkedin.domain.states import PostStatus

    post = (await posts.list_by_status(PostStatus.AWAITING_APPROVAL))[0]

    payload = _callback_payload(
        chat_id=settings.telegram_chat_id, data=f"approve:{post.id}", cb_id="cb-x"
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as ac:
        r = await ac.post("/telegram/webhook", json=payload, headers=headers)
    assert r.status_code == 200
    await flow.wait_pending()  # Fase F: aguarda publicação em background
    published = await posts.list_by_status(PostStatus.PUBLISHED)
    assert len(published) == 1
    assert any(c["id"] == "cb-x" for c in telegram.answered_callbacks)
