"""Testes do enforcement de MAX_REVISION_ITERATIONS (Fase E).

Cobre:
  - Reprovar antes do limite caminha pro fluxo normal (REVISING + motivo)
  - Reprovar NO limite substitui keyboard sem transicionar
  - Aprovar no limite funciona normal (handle_approval existente)
  - Cancelar no limite marca REJECTED com motivo
  - Cancelar idempotente (clique duplo OK)
  - Contador #N de M aparece no header da revisão
"""

from typing import NamedTuple

import pytest

from bot_posts_linkedin.config import get_settings
from bot_posts_linkedin.domain.rejection_cause import RejectionCause
from bot_posts_linkedin.domain.states import PostStatus
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
)

CHAT_ID = "333"


class _Bundle(NamedTuple):
    service: PostFlowService
    telegram: FakeTelegramClient
    posts: InMemoryPostStore
    chats: InMemoryChatStateStore
    anthropic: FakeAnthropicClient
    github: FakeGithubSearchService
    post_gen: FakePostGenerator
    replicate: FakeReplicateImageService
    gcs: FakeGcsImageStorage
    linkedin: FakeLinkedInPublisher
    task_queue: FakeTaskQueueClient


def _make_service() -> _Bundle:
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
    service = PostFlowService(
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
        settings=get_settings(),
    )
    task_queue.set_dispatch_target(service)
    return _Bundle(
        service, telegram, posts, chats, anthropic, github, post_gen, replicate, gcs, linkedin,
        task_queue,
    )


async def _setup_post_at_revision_count(b: _Bundle, count: int):
    """Cria um post + força revision_count diretamente — atalho pra evitar
    rodar N reprovações em testes (caro com a regen de body+imagem)."""
    await b.service.handle_command(CHAT_ID, "tema", False)
    await b.service.wait_pending()
    post = (await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL))[0]
    post.revision_count = count
    await b.posts.save(post)
    return post


@pytest.mark.asyncio
async def test_reprovar_antes_do_limite_caminha_normal() -> None:
    """revision_count < max → fluxo de reprovação padrão (REVISING + motivo)."""
    b = _make_service()
    settings = get_settings()
    post = await _setup_post_at_revision_count(b, settings.max_revision_iterations - 1)

    await b.service.handle_rejection(CHAT_ID, post.id, "cb1")

    # Transicionou pra REVISING
    revising = await b.posts.list_by_status(PostStatus.REVISING)
    assert len(revising) == 1
    # chat_state aguarda motivo
    state = await b.chats.get(CHAT_ID)
    assert state is not None
    assert state.awaiting_revision_for_post_id == post.id
    # Última edição é o "me diga o motivo"
    assert "motivo" in b.telegram.edited_messages[-1]["text"].lower()
    # E o keyboard NÃO mudou pro limit_reached
    last_kb = b.telegram.edited_messages[-1].get("reply_markup")
    assert last_kb is None  # edit_message_text sem reply_markup mantém o keyboard original


@pytest.mark.asyncio
async def test_reprovar_no_limite_substitui_keyboard_sem_transicionar() -> None:
    """revision_count == max → bot oferece [Aprovar última | Cancelar tudo]."""
    b = _make_service()
    settings = get_settings()
    post = await _setup_post_at_revision_count(b, settings.max_revision_iterations)

    await b.service.handle_rejection(CHAT_ID, post.id, "cb1")

    # NÃO transicionou — continua em AWAITING_APPROVAL
    awaiting = await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL)
    assert len(awaiting) == 1 and awaiting[0].id == post.id
    revising = await b.posts.list_by_status(PostStatus.REVISING)
    assert revising == []
    # chat_state NÃO foi criado (não tá esperando motivo)
    assert await b.chats.get(CHAT_ID) is None
    # Mensagem editada tem o footer do limite + keyboard novo
    last_edit = b.telegram.edited_messages[-1]
    assert "limite" in last_edit["text"].lower()
    kb = last_edit["reply_markup"]
    assert kb is not None
    button_texts = [b["text"] for row in kb["inline_keyboard"] for b in row]
    assert any("Aprovar" in t for t in button_texts)
    assert any("Cancelar" in t for t in button_texts)
    # E o callback_data do botão Cancelar tem o prefix cancel:
    cancel_buttons = [
        b for row in kb["inline_keyboard"] for b in row if "Cancelar" in b["text"]
    ]
    assert cancel_buttons[0]["callback_data"] == f"cancel:{post.id}"


@pytest.mark.asyncio
async def test_aprovar_no_limite_publica_normalmente() -> None:
    """Botão 'Aprovar essa última' usa o mesmo callback approve: e dispara publicação."""
    b = _make_service()
    settings = get_settings()
    post = await _setup_post_at_revision_count(b, settings.max_revision_iterations)

    # Simula o flow real: 1º reprovar (mostra limit keyboard), 2º clicar aprovar.
    await b.service.handle_rejection(CHAT_ID, post.id, "cb1")
    await b.service.handle_approval(CHAT_ID, post.id, "cb2")
    await b.service.wait_pending()  # Fase F: aguarda task de publish

    published = await b.posts.list_by_status(PostStatus.PUBLISHED)
    assert len(published) == 1 and published[0].id == post.id
    # Última edição é o "Publicado: ..." (não o footer do limite)
    assert "Publicado" in b.telegram.edited_messages[-1]["text"]
    assert len(b.linkedin.publish_calls) == 1


@pytest.mark.asyncio
async def test_cancelar_no_limite_marca_rejected() -> None:
    b = _make_service()
    settings = get_settings()
    post = await _setup_post_at_revision_count(b, settings.max_revision_iterations)
    await b.service.handle_rejection(CHAT_ID, post.id, "cb1")

    await b.service.handle_cancel(CHAT_ID, post.id, "cb2")

    rejected = await b.posts.list_by_status(PostStatus.REJECTED)
    assert len(rejected) == 1 and rejected[0].id == post.id
    assert rejected[0].rejection_cause == RejectionCause.USER_CANCELLED_AT_LIMIT
    # Sinal de controle NÃO contamina revision_feedback (que é só motivo humano).
    assert rejected[0].revision_feedback == []
    # Última mensagem editada tem o footer de cancelamento
    assert "Cancelado" in b.telegram.edited_messages[-1]["text"]
    assert any(c["id"] == "cb2" for c in b.telegram.answered_callbacks)


@pytest.mark.asyncio
async def test_cancelar_e_idempotente() -> None:
    """Clique duplo em Cancelar não erra — o segundo é noop."""
    b = _make_service()
    settings = get_settings()
    post = await _setup_post_at_revision_count(b, settings.max_revision_iterations)
    await b.service.handle_rejection(CHAT_ID, post.id, "cb1")
    await b.service.handle_cancel(CHAT_ID, post.id, "cb2")
    edits_apos_primeiro = len(b.telegram.edited_messages)

    # Segundo clique
    await b.service.handle_cancel(CHAT_ID, post.id, "cb3")

    # Status continua REJECTED (não muda — não há transição válida saindo de terminal)
    rejected = await b.posts.list_by_status(PostStatus.REJECTED)
    assert len(rejected) == 1
    # Nenhuma edição extra disparada
    assert len(b.telegram.edited_messages) == edits_apos_primeiro
    # Mas o answer_callback do segundo foi feito (UX: feedback imediato no botão)
    assert any(c["id"] == "cb3" for c in b.telegram.answered_callbacks)


def _make_service_with_max(max_iterations: int) -> _Bundle:
    """Variante de _make_service com MAX_REVISION_ITERATIONS customizado.

    Usa model_copy para sobrescrever só o campo que importa, sem ter que
    poluir o .env de teste com vars diferentes.
    """
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
    settings = get_settings().model_copy(
        update={"max_revision_iterations": max_iterations}
    )
    service = PostFlowService(
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
    task_queue.set_dispatch_target(service)
    return _Bundle(
        service, telegram, posts, chats, anthropic, github, post_gen, replicate, gcs, linkedin,
        task_queue,
    )


@pytest.mark.asyncio
async def test_semantica_com_max_iterations_igual_a_1_passo_a_passo() -> None:
    """Teste DIRETO da semântica: 'o user pode aplicar até N revisões antes do limit flow'.

    Com MAX=1, o passo a passo esperado é:
      Step 0 — gerar post:            revision_count=0, AWAITING_APPROVAL
      Step 1 — reprovar #1 + motivo:  fluxo NORMAL (REVISING → regen) → count=1
      Step 2 — reprovar #2:           LIMIT FLOW (sem transição, keyboard novo)
      Step 3 — clicar Cancelar:       REJECTED com rejection_cause=USER_CANCELLED_AT_LIMIT

    Se o limit flow disparasse já na 1ª reprovação, o user com MAX=1 não conseguiria
    nem 1 revisão — seria off-by-one que estragaria a configuração.
    """
    b = _make_service_with_max(1)

    # Step 0 — gera post
    await b.service.handle_command(CHAT_ID, "tema", False)
    await b.service.wait_pending()
    post_id = (await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL))[0].id

    # Step 1 — primeira reprovação: revision_count é 0, max é 1, 0 < 1 → NORMAL
    await b.service.handle_rejection(CHAT_ID, post_id, "cb1")
    assert (await b.posts.list_by_status(PostStatus.REVISING))[0].id == post_id, (
        "1ª reprovação com count=0 < max=1 deveria caminhar normal (REVISING), "
        "não disparar limit flow"
    )
    state = await b.chats.get(CHAT_ID)
    assert state is not None and state.awaiting_revision_for_post_id == post_id

    # Aplica o motivo da 1ª revisão → revision_count vira 1
    await b.service.handle_free_text(CHAT_ID, "motivo da primeira")
    await b.service.wait_pending()
    after_first = (await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL))[0]
    assert after_first.revision_count == 1
    assert "motivo da primeira" in after_first.revision_feedback

    # Step 2 — segunda reprovação: revision_count=1, max=1, 1 >= 1 → LIMIT FLOW
    edits_antes = len(b.telegram.edited_messages)
    await b.service.handle_rejection(CHAT_ID, post_id, "cb2")
    # NÃO transicionou pra REVISING
    assert (await b.posts.list_by_status(PostStatus.REVISING)) == []
    # Continua em AWAITING_APPROVAL
    assert (await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL))[0].id == post_id
    # Edit substituiu keyboard com footer do limite
    assert len(b.telegram.edited_messages) == edits_antes + 1
    last_edit = b.telegram.edited_messages[-1]
    assert "limite" in last_edit["text"].lower()
    button_texts = [
        btn["text"] for row in last_edit["reply_markup"]["inline_keyboard"] for btn in row
    ]
    assert any("Cancelar" in t for t in button_texts)

    # Step 3 — clica Cancelar → REJECTED com cause estruturada
    await b.service.handle_cancel(CHAT_ID, post_id, "cb3")
    rejected = await b.posts.list_by_status(PostStatus.REJECTED)
    assert len(rejected) == 1 and rejected[0].id == post_id
    assert rejected[0].rejection_cause == RejectionCause.USER_CANCELLED_AT_LIMIT
    # E revision_feedback contém SÓ o motivo humano da 1ª revisão, sem strings sintéticas.
    assert rejected[0].revision_feedback == ["motivo da primeira"]


@pytest.mark.asyncio
async def test_contador_aparece_no_header_da_revisao() -> None:
    """Header da revisão: '📝 Revisão #1 de 5 (motivo: ...)'"""
    b = _make_service()
    settings = get_settings()
    await b.service.handle_command(CHAT_ID, "tema", False)
    await b.service.wait_pending()
    post = (await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL))[0]
    await b.service.handle_rejection(CHAT_ID, post.id, "cb1")
    await b.service.handle_free_text(CHAT_ID, "muito formal")
    await b.service.wait_pending()

    nova_msg = b.telegram.sent_messages[-1]
    expected = f"Revisão #1 de {settings.max_revision_iterations}"
    assert expected in nova_msg["text"]
    assert "muito formal" in nova_msg["text"]
