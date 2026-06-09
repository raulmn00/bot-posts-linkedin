import pytest

from bot_posts_linkedin.domain.post import Post
from bot_posts_linkedin.domain.states import PostStatus
from bot_posts_linkedin.store.base import PostStore
from bot_posts_linkedin.store.memory import InMemoryPostStore


@pytest.mark.asyncio
async def test_inmemory_satisfaz_protocolo_poststore() -> None:
    # Garante que o stub respeita a interface — quebra cedo se a assinatura divergir.
    assert isinstance(InMemoryPostStore(), PostStore)


@pytest.mark.asyncio
async def test_save_e_get() -> None:
    store = InMemoryPostStore()
    post = Post(chat_id="t", user_prompt="teste")
    await store.save(post)
    recuperado = await store.get(post.id)
    assert recuperado is not None
    assert recuperado.id == post.id
    assert recuperado.user_prompt == "teste"


@pytest.mark.asyncio
async def test_get_inexistente_retorna_none() -> None:
    store = InMemoryPostStore()
    assert await store.get("nao-existe") is None


@pytest.mark.asyncio
async def test_list_by_status_filtra_corretamente() -> None:
    store = InMemoryPostStore()
    p1 = Post(chat_id="t", user_prompt="draft 1")
    p2 = Post(chat_id="t", user_prompt="approved 1", status=PostStatus.APPROVED)
    p3 = Post(chat_id="t", user_prompt="draft 2")
    for p in (p1, p2, p3):
        await store.save(p)

    drafts = await store.list_by_status(PostStatus.DRAFT)
    approved = await store.list_by_status(PostStatus.APPROVED)
    assert len(drafts) == 2
    assert len(approved) == 1


@pytest.mark.asyncio
async def test_find_active_for_chat_ignora_terminais_e_outros_chats() -> None:
    store = InMemoryPostStore()
    terminal = Post(chat_id="A", user_prompt="velho publicado", status=PostStatus.PUBLISHED)
    ativo = Post(chat_id="A", user_prompt="em revisao", status=PostStatus.REVISING)
    outro_chat = Post(chat_id="B", user_prompt="em outro chat", status=PostStatus.GENERATING)
    for p in (terminal, ativo, outro_chat):
        await store.save(p)

    achado = await store.find_active_for_chat("A")
    assert achado is not None
    assert achado.id == ativo.id
    assert await store.find_active_for_chat("nao-existe") is None


@pytest.mark.asyncio
async def test_find_active_for_chat_retorna_mais_recente() -> None:
    from datetime import UTC, datetime, timedelta

    store = InMemoryPostStore()
    velho = Post(chat_id="A", user_prompt="velho", status=PostStatus.RESEARCHING)
    velho.created_at = datetime.now(UTC) - timedelta(hours=2)
    novo = Post(chat_id="A", user_prompt="novo", status=PostStatus.GENERATING)
    await store.save(velho)
    await store.save(novo)

    achado = await store.find_active_for_chat("A")
    assert achado is not None
    assert achado.id == novo.id


@pytest.mark.asyncio
async def test_mutacao_externa_nao_afeta_storage() -> None:
    # Espelha o comportamento do Firestore: o que está dentro do store é independente
    # da referência que o caller continua segurando depois do save.
    store = InMemoryPostStore()
    post = Post(chat_id="t", user_prompt="original")
    await store.save(post)
    post.user_prompt = "modificado-fora"
    recuperado = await store.get(post.id)
    assert recuperado is not None
    assert recuperado.user_prompt == "original"
