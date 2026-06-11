from datetime import UTC, datetime, timedelta

import pytest

from bot_posts_linkedin.domain.chat_state import ChatState, PendingNewCommand
from bot_posts_linkedin.store.chat_state_memory import InMemoryChatStateStore


def _state(chat_id: str, *, hours_ttl: int = 24, **extras: object) -> ChatState:
    return ChatState(
        chat_id=chat_id,
        expires_at=datetime.now(UTC) + timedelta(hours=hours_ttl),
        **extras,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_save_e_get() -> None:
    store = InMemoryChatStateStore()
    state = _state("123", awaiting_revision_for_post_id="abc")
    await store.save(state)
    fetched = await store.get("123")
    assert fetched is not None
    assert fetched.chat_id == "123"
    assert fetched.awaiting_revision_for_post_id == "abc"


@pytest.mark.asyncio
async def test_get_expirado_retorna_none() -> None:
    store = InMemoryChatStateStore()
    expirado = ChatState(
        chat_id="123",
        awaiting_revision_for_post_id="abc",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await store.save(expirado)
    assert await store.get("123") is None


@pytest.mark.asyncio
async def test_delete_idempotente() -> None:
    store = InMemoryChatStateStore()
    await store.delete("inexistente")  # não deve erro
    await store.save(_state("123"))
    await store.delete("123")
    assert await store.get("123") is None


@pytest.mark.asyncio
async def test_pending_new_command_persiste_e_volta() -> None:
    store = InMemoryChatStateStore()
    state = _state(
        "123",
        awaiting_revision_for_post_id="abc",
        pending_new_command=PendingNewCommand(user_prompt="novo", use_github=True),
    )
    await store.save(state)
    fetched = await store.get("123")
    assert fetched is not None
    assert fetched.pending_new_command is not None
    assert fetched.pending_new_command.user_prompt == "novo"
    assert fetched.pending_new_command.use_github is True


@pytest.mark.asyncio
async def test_mutacao_externa_nao_afeta_storage() -> None:
    store = InMemoryChatStateStore()
    state = _state("123", awaiting_revision_for_post_id="abc")
    await store.save(state)
    state.awaiting_revision_for_post_id = "modificado"
    fetched = await store.get("123")
    assert fetched is not None
    assert fetched.awaiting_revision_for_post_id == "abc"
