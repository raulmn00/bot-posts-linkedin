"""Testes específicos da Fase D — gather race-free + parse + failure modes.

Cobre cenários introduzidos na Fase D:
  - happy path manda foto + texto + insights
  - body falha → REJECTED (não tem foto enviada antes do failure notice)
  - BilingualParseError vira mensagem específica (não generica failure)
  - imagem falha mas body OK → post SEM foto + aviso (não REJECTED)
  - image_prompt falha (Q4(a)) → mesmo tratamento que image falha
  - revisão regenera body+imagem juntos (decisão MVP)
"""

from typing import NamedTuple

import pytest

from bot_posts_linkedin.config import get_settings
from bot_posts_linkedin.domain.states import PostStatus
from bot_posts_linkedin.services.post_flow import PostFlowService
from bot_posts_linkedin.services.post_generator import BilingualParseError
from bot_posts_linkedin.store.chat_state_memory import InMemoryChatStateStore
from bot_posts_linkedin.store.memory import InMemoryPostStore
from tests.fakes import (
    FakeAnthropicClient,
    FakeGcsImageStorage,
    FakeGithubSearchService,
    FakeLinkedInPublisher,
    FakePostGenerator,
    FakeReplicateImageService,
    FakeTelegramClient,
)

CHAT_ID = "555"


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
        settings=get_settings(),
    )
    return _Bundle(
        service, telegram, posts, chats, anthropic, github, post_gen, replicate, gcs, linkedin
    )


# ============================================================ happy gather


@pytest.mark.asyncio
async def test_happy_gather_dispara_body_e_imagem_em_paralelo() -> None:
    b = _make_service()
    await b.service.handle_command(CHAT_ID, "tema", False)
    await b.service.wait_pending()

    # Body chamado uma vez, image_prompt uma vez, replicate uma vez, gcs uma vez.
    assert len(b.post_gen.body_calls) == 1
    assert len(b.post_gen.image_prompt_calls) == 1
    assert len(b.replicate.generate_calls) == 1
    assert len(b.gcs.store_calls) == 1

    # Foto enviada antes do texto bilíngue + botões.
    assert len(b.telegram.sent_photos) == 1
    assert len(b.telegram.sent_messages) == 2  # insights + post

    post = (await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL))[0]
    assert post.image_url is not None
    assert post.image_gcs_path is not None
    assert post.image_prompt is not None


# ============================================================ body failure


@pytest.mark.asyncio
async def test_body_falha_vai_para_rejected_sem_enviar_foto() -> None:
    b = _make_service()
    b.post_gen.set_body_error(RuntimeError("LLM down"))

    await b.service.handle_command(CHAT_ID, "tema", False)
    await b.service.wait_pending()

    # Body falhou → REJECTED. Foto não foi enviada.
    rejected = await b.posts.list_by_status(PostStatus.REJECTED)
    assert len(rejected) == 1
    assert b.telegram.sent_photos == []
    # Insights foram enviadas (Fase C completou) + mensagem de falha.
    assert len(b.telegram.sent_messages) == 2
    assert "Falha ao gerar" in b.telegram.sent_messages[-1]["text"]


@pytest.mark.asyncio
async def test_bilingual_parse_error_dispara_mensagem_especifica() -> None:
    b = _make_service()
    b.post_gen.set_body_error(BilingualParseError("separador ausente"))

    await b.service.handle_command(CHAT_ID, "tema", False)
    await b.service.wait_pending()

    rejected = await b.posts.list_by_status(PostStatus.REJECTED)
    assert len(rejected) == 1
    msg = b.telegram.sent_messages[-1]["text"]
    # Mensagem é específica de parse — não a genérica de "falha".
    assert "formato bilíngue" in msg.lower()
    assert "===EN===" not in msg  # não vaza separator, só informa o problema
    assert "reformular" in msg.lower()


# ============================================================ image failure


@pytest.mark.asyncio
async def test_replicate_falha_post_vai_sem_foto_e_aviso() -> None:
    b = _make_service()
    b.replicate.set_error(RuntimeError("replicate 503"))

    await b.service.handle_command(CHAT_ID, "tema", False)
    await b.service.wait_pending()

    # NÃO foi pra REJECTED — post sem foto é OK pela diretiva.
    awaiting = await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL)
    assert len(awaiting) == 1
    post = awaiting[0]
    assert post.image_url is None
    assert post.image_gcs_path is None

    # Foto NÃO enviada.
    assert b.telegram.sent_photos == []
    # Mensagens: insights + aviso de imagem + post bilíngue + botões.
    assert len(b.telegram.sent_messages) == 3
    aviso = b.telegram.sent_messages[1]
    assert "não foi possível gerar a imagem" in aviso["text"].lower()
    assert "replicate" in aviso["text"].lower()
    # Post final tem botões.
    assert b.telegram.sent_messages[2]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_image_prompt_falha_post_vai_sem_foto() -> None:
    """Q4(a) do user: se image_prompt falhar, pipeline de imagem aborta cedo."""
    b = _make_service()
    b.post_gen.set_image_prompt_error(RuntimeError("LLM rejeitou prompt"))

    await b.service.handle_command(CHAT_ID, "tema", False)
    await b.service.wait_pending()

    # Body OK, imagem falhou no prompt — post sem foto.
    awaiting = await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL)
    assert len(awaiting) == 1
    assert b.telegram.sent_photos == []
    # Aviso de imagem está presente.
    avisos = [m for m in b.telegram.sent_messages if "imagem" in m["text"].lower()]
    assert len(avisos) == 1


@pytest.mark.asyncio
async def test_gcs_falha_post_vai_sem_foto() -> None:
    b = _make_service()
    b.gcs.set_error(RuntimeError("gcs forbidden"))

    await b.service.handle_command(CHAT_ID, "tema", False)
    await b.service.wait_pending()

    awaiting = await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL)
    assert len(awaiting) == 1
    assert b.telegram.sent_photos == []
    assert any("imagem" in m["text"].lower() for m in b.telegram.sent_messages)


# ============================================================ race-freedom


@pytest.mark.asyncio
async def test_tasks_de_body_e_imagem_nao_mutam_post_durante_gather() -> None:
    """Garantia da diretiva do user: tasks só retornam valores.

    Se uma das tasks mutar diretamente, vai ter race quando ambas terminarem
    quase juntas. Este teste apenas executa o happy path e confirma que
    estado final está consistente — bug de race vazaria como flake.
    Repetir múltiplas vezes amplia a chance de detectar.
    """
    for _ in range(10):
        b = _make_service()
        await b.service.handle_command(CHAT_ID, "tema", False)
        await b.service.wait_pending()
        post = (await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL))[0]
        assert post.body_pt and post.body_en
        assert post.image_url and post.image_gcs_path and post.image_prompt


# ============================================================ revisão regenera tudo (decisão MVP)


@pytest.mark.asyncio
async def test_revisao_regenera_body_e_imagem_juntos() -> None:
    """Decisão MVP registrada: qualquer feedback dispara regen total."""
    b = _make_service()
    await b.service.handle_command(CHAT_ID, "tema", False)
    await b.service.wait_pending()
    post = (await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL))[0]
    await b.service.handle_rejection(CHAT_ID, post.id, "cb")
    body_calls_antes = len(b.post_gen.body_calls)
    image_prompt_calls_antes = len(b.post_gen.image_prompt_calls)
    replicate_calls_antes = len(b.replicate.generate_calls)
    gcs_calls_antes = len(b.gcs.store_calls)

    await b.service.handle_free_text(CHAT_ID, "trocar tom e visual")
    await b.service.wait_pending()

    # Ambos os pipelines (body + image) foram disparados de novo.
    assert len(b.post_gen.body_calls) == body_calls_antes + 1
    assert len(b.post_gen.image_prompt_calls) == image_prompt_calls_antes + 1
    assert len(b.replicate.generate_calls) == replicate_calls_antes + 1
    assert len(b.gcs.store_calls) == gcs_calls_antes + 1


@pytest.mark.asyncio
async def test_revisao_com_imagem_falha_vai_sem_foto_e_mantem_post() -> None:
    b = _make_service()
    await b.service.handle_command(CHAT_ID, "tema", False)
    await b.service.wait_pending()
    post = (await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL))[0]
    await b.service.handle_rejection(CHAT_ID, post.id, "cb")

    # Próxima geração: imagem falha
    b.replicate.set_error(RuntimeError("replicate 500"))

    await b.service.handle_free_text(CHAT_ID, "motivo")
    await b.service.wait_pending()

    # Post de volta em AWAITING_APPROVAL (não REJECTED — imagem falha sozinha não rejeita).
    awaiting = await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL)
    assert len(awaiting) == 1
    novo = awaiting[0]
    assert novo.id == post.id
    assert novo.image_url is None
    assert novo.revision_count == 1
