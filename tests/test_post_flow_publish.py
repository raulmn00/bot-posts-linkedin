"""Testes do handle_approval + publicação em background (Fase F).

Cobre:
  - Happy path real → PUBLISHED + edita msg com link
  - Dry-run → SIMULATED + envia payload em chunks + edita msg com SIMULATED_FOOTER
  - 2xx sem URN → PUBLISHED com aviso "sem link"
  - TokenExpired → REJECTED + cause=TOKEN_EXPIRED + mensagem direcionada
  - PublicationFailed → REJECTED + cause=PUBLICATION_FAILURE + mensagem genérica
  - Idempotência (duplo clique em Aprovar): publica só 1×
"""

from typing import NamedTuple

import pytest

from bot_posts_linkedin.config import get_settings
from bot_posts_linkedin.domain.rejection_cause import RejectionCause
from bot_posts_linkedin.domain.states import PostStatus
from bot_posts_linkedin.services.linkedin_publisher import (
    PublicationFailedError,
    PublicationResult,
)
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

CHAT_ID = "444"


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


def _make_service(*, linkedin_dry_run: bool = False) -> _Bundle:
    telegram = FakeTelegramClient()
    posts = InMemoryPostStore()
    chats = InMemoryChatStateStore()
    anthropic = FakeAnthropicClient()
    github = FakeGithubSearchService()
    post_gen = FakePostGenerator()
    replicate = FakeReplicateImageService()
    gcs = FakeGcsImageStorage()
    linkedin = FakeLinkedInPublisher(dry_run=linkedin_dry_run)
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


async def _gen_and_get_awaiting(b: _Bundle):
    await b.service.handle_command(CHAT_ID, "tema", False)
    await b.service.wait_pending()
    return (await b.posts.list_by_status(PostStatus.AWAITING_APPROVAL))[0]


# ============================================================ happy real


@pytest.mark.asyncio
async def test_happy_publica_e_edita_com_link() -> None:
    b = _make_service(linkedin_dry_run=False)
    post = await _gen_and_get_awaiting(b)

    await b.service.handle_approval(CHAT_ID, post.id, "cb1")
    await b.service.wait_pending()

    published = await b.posts.list_by_status(PostStatus.PUBLISHED)
    assert len(published) == 1
    assert published[0].linkedin_post_urn is not None
    # 2 edits: PUBLISHING_FOOTER (no clique) + footer com link (após publicar)
    edited_texts = [e["text"] for e in b.telegram.edited_messages]
    edited_texts_lower = [t.lower() for t in edited_texts]
    assert any("publicando" in t for t in edited_texts_lower)
    assert any("publicado" in t for t in edited_texts_lower)
    assert any("https://www.linkedin.com/feed/update/" in t for t in edited_texts)


# ============================================================ dry-run


@pytest.mark.asyncio
async def test_dry_run_NAO_marca_published_no_store_e_nao_faz_chamada_real() -> None:
    """PONTO 2: garantia explícita que dry-run não suja o audit.

    Asserções:
      - Post fica em SIMULATED (terminal), NÃO em PUBLISHED
      - list_by_status(PUBLISHED) vazia → não polui audit de "publicados"
      - linkedin_post_urn permanece None (nada foi de fato publicado)
      - O publisher recebeu chamada com dry_run=True (e não real)
    """
    b = _make_service(linkedin_dry_run=True)
    post = await _gen_and_get_awaiting(b)

    await b.service.handle_approval(CHAT_ID, post.id, "cb1")
    await b.service.wait_pending()

    # Status terminal correto
    simulated = await b.posts.list_by_status(PostStatus.SIMULATED)
    assert len(simulated) == 1
    assert simulated[0].id == post.id

    # NÃO foi marcado PUBLISHED — o audit fica limpo
    published = await b.posts.list_by_status(PostStatus.PUBLISHED)
    assert published == [], (
        f"dry-run sujou o store: {len(published)} post(s) marcados como PUBLISHED"
    )

    # Sem URN do LinkedIn — nada foi publicado de verdade
    assert simulated[0].linkedin_post_urn is None

    # Publisher foi chamado em modo dry_run=True (não em modo real)
    assert len(b.linkedin.publish_calls) == 1
    assert b.linkedin.publish_calls[0]["dry_run"] is True


@pytest.mark.asyncio
async def test_dry_run_marca_simulated_e_envia_payload_em_chunks() -> None:
    b = _make_service(linkedin_dry_run=True)
    post = await _gen_and_get_awaiting(b)

    sent_messages_antes = len(b.telegram.sent_messages)
    await b.service.handle_approval(CHAT_ID, post.id, "cb1")
    await b.service.wait_pending()

    # Status terminal SIMULATED, não PUBLISHED.
    simulated = await b.posts.list_by_status(PostStatus.SIMULATED)
    assert len(simulated) == 1
    published = await b.posts.list_by_status(PostStatus.PUBLISHED)
    assert published == []
    # Payload foi enviado em pelo menos 1 mensagem nova com header DRY RUN.
    new_msgs = b.telegram.sent_messages[sent_messages_antes:]
    assert len(new_msgs) >= 1
    payload_msg = new_msgs[0]
    assert "DRY RUN" in payload_msg["text"]
    assert payload_msg["parse_mode"] == "HTML"
    assert "<pre>" in payload_msg["text"]
    # Conteúdo do JSON no body — author + commentary + visibility.
    assert PostStatus.SIMULATED.value not in payload_msg["text"]  # payload em si, não o status
    full_text = "".join(m["text"] for m in new_msgs)
    assert '"author"' in full_text
    assert '"commentary"' in full_text
    assert '"visibility"' in full_text
    # Mensagem do post foi editada com SIMULATED_FOOTER.
    assert "SIMULADO" in b.telegram.edited_messages[-1]["text"]


# ============================================================ 2xx sem URN


@pytest.mark.asyncio
async def test_2xx_sem_urn_marca_published_mas_avisa() -> None:
    b = _make_service(linkedin_dry_run=False)
    # Configura resposta da Fake sem URN
    b.linkedin.set_real_response(
        PublicationResult(
            dry_run=False,
            payload_sent={"author": "urn:li:person:fake"},
            post_urn=None,  # API respondeu 2xx sem x-restli-id
            image_urn="urn:li:image:fake",
        )
    )

    post = await _gen_and_get_awaiting(b)
    await b.service.handle_approval(CHAT_ID, post.id, "cb1")
    await b.service.wait_pending()

    published = await b.posts.list_by_status(PostStatus.PUBLISHED)
    assert len(published) == 1
    assert published[0].linkedin_post_urn is None
    # Footer especial avisa que publicou mas sem link.
    last_edit = b.telegram.edited_messages[-1]
    text = last_edit["text"].lower()
    assert "publicado" in text
    assert "não devolveu o link" in text


# ============================================================ token expirado


@pytest.mark.asyncio
async def test_token_expirado_marca_rejected_com_cause_dedicada() -> None:
    b = _make_service(linkedin_dry_run=False)
    b.linkedin.set_token_expired()

    post = await _gen_and_get_awaiting(b)
    await b.service.handle_approval(CHAT_ID, post.id, "cb1")
    await b.service.wait_pending()

    rejected = await b.posts.list_by_status(PostStatus.REJECTED)
    assert len(rejected) == 1
    assert rejected[0].rejection_cause == RejectionCause.TOKEN_EXPIRED
    # Mensagem específica de token expirado, não a genérica de "Falha ao publicar".
    msgs_texts = [m["text"] for m in b.telegram.sent_messages]
    assert any("Token do LinkedIn expirou" in t for t in msgs_texts)
    assert any("60 dias" in t for t in msgs_texts)


# ============================================================ falha genérica


@pytest.mark.asyncio
async def test_publication_failed_marca_rejected_com_cause_generica() -> None:
    b = _make_service(linkedin_dry_run=False)
    b.linkedin.set_error(PublicationFailedError("createPost status 400: malformed"))

    post = await _gen_and_get_awaiting(b)
    await b.service.handle_approval(CHAT_ID, post.id, "cb1")
    await b.service.wait_pending()

    rejected = await b.posts.list_by_status(PostStatus.REJECTED)
    assert len(rejected) == 1
    assert rejected[0].rejection_cause == RejectionCause.PUBLICATION_FAILURE
    assert "400" in (rejected[0].rejection_detail or "")
    msgs_texts = [m["text"] for m in b.telegram.sent_messages]
    assert any("Falha ao publicar" in t for t in msgs_texts)


# ============================================================ idempotência


@pytest.mark.asyncio
async def test_duplo_clique_em_aprovar_publica_apenas_uma_vez() -> None:
    """Telegram pode reentregar callbacks, e o user pode clicar 2× rápido.

    Idempotência: a 2ª chamada vê status != AWAITING_APPROVAL e ignora.
    """
    b = _make_service(linkedin_dry_run=False)
    post = await _gen_and_get_awaiting(b)

    # Dois cliques em sequência ANTES do background terminar.
    await b.service.handle_approval(CHAT_ID, post.id, "cb1")
    await b.service.handle_approval(CHAT_ID, post.id, "cb2")
    await b.service.wait_pending()

    # Apenas 1 chamada ao LinkedIn — segunda foi ignorada.
    assert len(b.linkedin.publish_calls) == 1
    # Ambos os callbacks foram respondidos (UX: feedback visual no botão).
    assert any(c["id"] == "cb1" for c in b.telegram.answered_callbacks)
    assert any(c["id"] == "cb2" for c in b.telegram.answered_callbacks)
    # 2º callback recebeu "Já processado.".
    cb2_answer = next(c for c in b.telegram.answered_callbacks if c["id"] == "cb2")
    assert cb2_answer["text"] == "Já processado."

    published = await b.posts.list_by_status(PostStatus.PUBLISHED)
    assert len(published) == 1


@pytest.mark.asyncio
async def test_aprovar_post_em_estado_terminal_e_ignorado() -> None:
    """Cobre o caso de callback re-entregue depois que o post já foi published."""
    b = _make_service(linkedin_dry_run=False)
    post = await _gen_and_get_awaiting(b)
    await b.service.handle_approval(CHAT_ID, post.id, "cb1")
    await b.service.wait_pending()

    # Re-clica em Aprovar AGORA com status==PUBLISHED. Não deve disparar novo publish.
    calls_antes = len(b.linkedin.publish_calls)
    await b.service.handle_approval(CHAT_ID, post.id, "cb-late")

    assert len(b.linkedin.publish_calls) == calls_antes  # nada novo
    cb_late = next(c for c in b.telegram.answered_callbacks if c["id"] == "cb-late")
    assert cb_late["text"] == "Já processado."
