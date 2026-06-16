"""Testes focados nas salvaguardas e novidades das Fases C/D.

Cobre:
  - mensagem de insumos sempre antes do post (Fase C)
  - github_findings só com use_github=True
  - falha na geração vira REJECTED + aviso (a)
  - discard pendente bloqueia free text (c)
  - discard estendido pra qualquer estado transitório (b)
"""

from typing import NamedTuple

import pytest

from bot_posts_linkedin.config import get_settings
from bot_posts_linkedin.domain.post import Post
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

CHAT_ID = "777"


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


# ============================================================ insights message


@pytest.mark.asyncio
async def test_insights_message_sem_github() -> None:
    b = _make_service()
    await b.service.handle_command(CHAT_ID, "rag híbrido", use_github=False)
    await b.service.wait_pending()

    insights = b.telegram.sent_messages[0]
    assert "Insumos coletados" in insights["text"]
    assert "Pesquisa web" in insights["text"]
    assert "GitHub" not in insights["text"]
    assert len(b.github.search_calls) == 0
    assert len(b.anthropic.research_calls) == 1


@pytest.mark.asyncio
async def test_use_github_injeta_author_context_na_pesquisa_web() -> None:
    """Sem author_context, o Claude tenta achar 'meu projeto' na web e se perde
    entre repos homônimos de outras pessoas. Com contexto, ele sabe que o GitHub
    do raulmn00 vai ser consultado em separado e foca em CONCEITOS técnicos."""
    b = _make_service()
    await b.service.handle_command(CHAT_ID, "meu agent-router", use_github=True)
    await b.service.wait_pending()

    assert len(b.anthropic.research_calls) == 1
    call = b.anthropic.research_calls[0]
    assert call["author_context"] is not None
    assert "raulmn00" in call["author_context"]
    assert "outra etapa" in call["author_context"].lower()


@pytest.mark.asyncio
async def test_sem_github_nao_injeta_author_context() -> None:
    b = _make_service()
    await b.service.handle_command(CHAT_ID, "rag híbrido", use_github=False)
    await b.service.wait_pending()

    call = b.anthropic.research_calls[0]
    assert call["author_context"] is None


@pytest.mark.asyncio
async def test_insights_message_com_github_lista_repos() -> None:
    b = _make_service()
    await b.service.handle_command(CHAT_ID, "agent router", use_github=True)
    await b.service.wait_pending()

    insights = b.telegram.sent_messages[0]
    assert "📚 GitHub" in insights["text"]
    assert "raulmn00/agent-router" in insights["text"]
    assert len(b.github.search_calls) == 1


@pytest.mark.asyncio
async def test_insights_inclui_fontes_da_pesquisa() -> None:
    b = _make_service()
    await b.service.handle_command(CHAT_ID, "tema qualquer", use_github=False)
    await b.service.wait_pending()

    insights_text = b.telegram.sent_messages[0]["text"]
    assert "Fontes" in insights_text
    assert "https://exemplo.com/fonte1" in insights_text


# ============================================================ failure path (a)


@pytest.mark.asyncio
async def test_falha_no_web_search_vai_para_rejected_com_aviso() -> None:
    b = _make_service()
    b.anthropic.set_research_error(RuntimeError("API timeout"))

    await b.service.handle_command(CHAT_ID, "x", use_github=False)
    await b.service.wait_pending()

    rejected = await b.posts.list_by_status(PostStatus.REJECTED)
    assert len(rejected) == 1
    assert rejected[0].rejection_cause == RejectionCause.GENERATION_FAILURE
    assert rejected[0].rejection_detail is not None
    assert "RuntimeError" in rejected[0].rejection_detail
    # revision_feedback continua vazio (não houve motivo humano de revisão).
    assert rejected[0].revision_feedback == []
    assert len(b.telegram.sent_messages) == 1
    assert "Falha ao gerar" in b.telegram.sent_messages[0]["text"]
    assert "RuntimeError" in b.telegram.sent_messages[0]["text"]


@pytest.mark.asyncio
async def test_falha_no_github_vai_para_rejected() -> None:
    b = _make_service()
    b.github.set_error(RuntimeError("github rate limit"))

    await b.service.handle_command(CHAT_ID, "x", use_github=True)
    await b.service.wait_pending()

    rejected = await b.posts.list_by_status(PostStatus.REJECTED)
    assert len(rejected) == 1
    assert "Falha ao gerar" in b.telegram.sent_messages[-1]["text"]


# ============================================================ salvaguarda (b)


@pytest.mark.asyncio
async def test_novo_comando_durante_pre_aprovacao_pergunta_descartar() -> None:
    b = _make_service()
    await b.service.handle_command(CHAT_ID, "primeiro", False)
    await b.service.wait_pending()
    msgs_antes = len(b.telegram.sent_messages)

    await b.service.handle_command(CHAT_ID, "segundo", False)
    await b.service.wait_pending()

    assert len(b.telegram.sent_messages) == msgs_antes + 1
    pergunta = b.telegram.sent_messages[-1]
    assert "pendente" in pergunta["text"].lower()
    assert pergunta["reply_markup"] is not None
    state = await b.chats.get(CHAT_ID)
    assert state is not None
    assert state.pending_new_command is not None
    assert state.pending_new_command.user_prompt == "segundo"


@pytest.mark.asyncio
async def test_novo_comando_durante_researching_pergunta_descartar() -> None:
    """Salvaguarda (b): mesmo em estado transitório, novo comando bloqueia."""
    b = _make_service()
    posts_in_progress = Post(
        chat_id=CHAT_ID,
        user_prompt="em progresso",
        status=PostStatus.RESEARCHING,
    )
    await b.posts.save(posts_in_progress)

    await b.service.handle_command(CHAT_ID, "novo comando", False)
    await b.service.wait_pending()

    todos = []
    for st in PostStatus:
        todos.extend(await b.posts.list_by_status(st))
    assert len(todos) == 1
    pergunta = b.telegram.sent_messages[0]
    assert "pendente" in pergunta["text"].lower()
    assert "researching" in pergunta["text"].lower()


# ============================================================ salvaguarda (c) / pendente Fase B


@pytest.mark.asyncio
async def test_texto_livre_durante_discard_pendente_bloqueia_e_avisa() -> None:
    b = _make_service()
    await b.service.handle_command(CHAT_ID, "primeiro", False)
    await b.service.wait_pending()
    p1 = (await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL))[0]
    await b.service.handle_rejection(CHAT_ID, p1.id, "cb1")
    await b.service.handle_command(CHAT_ID, "segundo", False)
    await b.service.wait_pending()

    state = await b.chats.get(CHAT_ID)
    assert state is not None and state.pending_new_command is not None

    msgs_antes = len(b.telegram.sent_messages)
    revision_count_antes = p1.revision_count

    await b.service.handle_free_text(CHAT_ID, "isso aqui era pra ser motivo")
    await b.service.wait_pending()

    assert len(b.telegram.sent_messages) == msgs_antes + 1
    aviso = b.telegram.sent_messages[-1]
    assert "pergunta pendente" in aviso["text"].lower()
    p1_atual = await b.posts.get(p1.id)
    assert p1_atual is not None
    assert p1_atual.revision_count == revision_count_antes
    state_depois = await b.chats.get(CHAT_ID)
    assert state_depois is not None
    assert state_depois.pending_new_command is not None


# ============================================================ extras


@pytest.mark.asyncio
async def test_concorrencia_em_handle_command_dispara_dedup() -> None:
    b = _make_service()
    await b.service.handle_command(CHAT_ID, "primeiro", False)
    await b.service.handle_command(CHAT_ID, "segundo", False)
    await b.service.wait_pending()

    awaiting = await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL)
    assert len(awaiting) == 1
    assert awaiting[0].user_prompt == "primeiro"


@pytest.mark.asyncio
async def test_close_dos_fakes_e_idempotente() -> None:
    b = _make_service()
    await b.telegram.close()
    await b.telegram.close()
    assert b.telegram.closed
    await b.anthropic.close()
    assert b.anthropic.closed
    await b.replicate.close()
    assert b.replicate.closed
