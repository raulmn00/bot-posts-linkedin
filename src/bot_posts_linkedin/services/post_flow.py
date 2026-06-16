"""Orquestração do fluxo completo de um post.

Desacoplada do FastAPI/Telegram: recebe stores + clients + services via
construtor, testável com fakes, reutilizável pelo worker do Cloud Tasks na Fase G.

Salvaguardas acumuladas:
  (a) try/except global no flow em background → REJECTED + aviso no Telegram
  (b) `find_active_for_chat` detecta conflito em qualquer estado transitório
  (c) handle_free_text bloqueia motivo quando há discard pendente
  (d) gather body+imagem RACE-FREE: tasks só RETORNAM valores, mutação do Post
      acontece sequencialmente após o gather, dono único
  (e) imagem falha sozinha → post sem foto + aviso dedicado (NÃO REJECTED)
  (f) parse bilíngue falho → mensagem específica (não EN vazio silencioso)

Decisão de produto MVP (Fase D):
  Qualquer reprovação regenera body + imagem juntos. Detectar "feedback é só
  sobre imagem" via LLM é frágil e complica a state machine — adia pra pós-MVP.
  Custo extra ~$0.05/revisão é aceitável.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from bot_posts_linkedin.config import Settings
from bot_posts_linkedin.domain.chat_state import ChatState, PendingNewCommand
from bot_posts_linkedin.domain.post import Post
from bot_posts_linkedin.domain.rejection_cause import RejectionCause
from bot_posts_linkedin.domain.states import PostStatus, is_terminal
from bot_posts_linkedin.services.anthropic_client import AnthropicClient
from bot_posts_linkedin.services.github_search import GithubSearchService
from bot_posts_linkedin.services.image_generator import (
    GcsImageStorage,
    ReplicateImageService,
)
from bot_posts_linkedin.services.linkedin_publisher import (
    LinkedInPublisher,
    TokenExpiredError,
)
from bot_posts_linkedin.services.post_generator import (
    BilingualParseError,
    PostGeneratorService,
)
from bot_posts_linkedin.services.task_queue import TaskQueueClient
from bot_posts_linkedin.store.base import PostStore
from bot_posts_linkedin.store.chat_state_base import ChatStateStore
from bot_posts_linkedin.telegram.client import TelegramClient
from bot_posts_linkedin.telegram.keyboards import (
    approval_keyboard,
    discard_keyboard,
    limit_reached_keyboard,
)
from bot_posts_linkedin.telegram.messages import (
    ASKING_REASON_MESSAGE,
    CANCELLED_AT_LIMIT_FOOTER,
    DISCARD_PENDING_TEXT_MESSAGE,
    DISCARDED_KEEP_PENDING,
    HELP_MESSAGE,
    LIMIT_REACHED_FOOTER,
    PUBLISHED_WITHOUT_URN_FOOTER,
    PUBLISHING_FOOTER,
    SIMULATED_FOOTER,
    format_bilingual_parse_failure_message,
    format_bilingual_post,
    format_caption_short,
    format_discard_question,
    format_dry_run_chunks,
    format_failure_message,
    format_image_failure_notice,
    format_insights_message,
    format_publication_failure_message,
    format_published_footer,
    format_revision_header,
    format_token_expired_message,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _short(err: BaseException, max_len: int = 300) -> str:
    s = f"{type(err).__name__}: {err}"
    return s if len(s) <= max_len else s[: max_len - 3] + "..."


def _pretty_json(payload: dict) -> str:
    """Pretty-print pro dry-run mostrar o payload no Telegram de forma legível."""
    return json.dumps(payload, ensure_ascii=False, indent=2)


class PostFlowService:
    def __init__(
        self,
        post_store: PostStore,
        chat_state_store: ChatStateStore,
        telegram_client: TelegramClient,
        anthropic_client: AnthropicClient,
        github_search: GithubSearchService,
        post_generator: PostGeneratorService,
        replicate_image: ReplicateImageService,
        gcs_image: GcsImageStorage,
        linkedin_publisher: LinkedInPublisher,
        task_queue: TaskQueueClient,
        settings: Settings,
    ) -> None:
        self._posts = post_store
        self._chats = chat_state_store
        self._telegram = telegram_client
        self._anthropic = anthropic_client
        self._github = github_search
        self._post_generator = post_generator
        self._replicate = replicate_image
        self._gcs = gcs_image
        self._linkedin = linkedin_publisher
        self._task_queue = task_queue
        self._settings = settings
        # Mantido vazio pra compat de wait_pending() em tests existentes —
        # com Cloud Tasks o spawn é via enqueue (durável) e não asyncio.create_task.
        self._background_tasks: set[asyncio.Task] = set()

    # ====================================================================== infra

    def _ttl(self) -> datetime:
        return _utcnow() + timedelta(hours=self._settings.revision_pending_ttl_hours)

    async def wait_pending(self) -> None:
        """Compat no-op pós G.3 — tasks vão pro Cloud Tasks (durável).

        Em tests, FakeTaskQueueClient executa síncrono dentro de enqueue;
        em prod, Cloud Tasks invoca o worker em outro processo. Não há
        background asyncio.create_task pra esperar.
        """
        return

    async def dispatch_task(self, action: str, payload: dict) -> None:
        """Roteador chamado pelo worker (Cloud Tasks → /internal/process-task).

        Cada action mapeia pra um dos _run_*_safely. Adicionar action nova:
        criar caso aqui + caller (handle_*) deve usar self._task_queue.enqueue.
        """
        if action == "run_generation":
            await self._run_generation_safely(payload["chat_id"], payload["post_id"])
        elif action == "run_revision":
            await self._run_revision_safely(
                payload["chat_id"], payload["post_id"], payload["reason"]
            )
        elif action == "run_publish":
            await self._run_publish_safely(payload["chat_id"], payload["post_id"])
        else:
            raise ValueError(f"unknown task action: {action!r}")

    def _build_author_context(self, use_github: bool) -> str | None:
        """Texto injetado no prompt da pesquisa web quando flag [GITHUB] está presente.

        Evita o Claude tentar "achar na web" repos pessoais do autor — esses
        vão ser consultados em separado via GithubApiSearch, que olha direto
        nos repos públicos de raulmn00. A pesquisa web deve focar em CONCEITOS,
        não no repo específico.
        """
        if not use_github:
            return None
        username = self._settings.github_username
        return (
            f"Contexto do autor: este post é escrito por Raul (@{username} no "
            "GitHub). Se o assunto mencionar 'meu projeto', 'meu agent', 'meu "
            "RAG' ou similar, NÃO tente identificar o repositório específico via "
            "web — esses repos serão consultados em outra etapa, direto no GitHub. "
            "Foque a pesquisa nos CONCEITOS técnicos e no estado da arte do tema, "
            "não em projetos homônimos de outras pessoas."
        )

    # ====================================================================== help

    async def send_help(self, chat_id: str) -> None:
        await self._telegram.send_message(chat_id=chat_id, text=HELP_MESSAGE)

    # ====================================================================== command

    async def handle_command(self, chat_id: str, prompt: str, use_github: bool) -> None:
        active = await self._posts.find_active_for_chat(chat_id)
        if active is not None:
            await self._ask_discard(chat_id, active, prompt, use_github)
            return

        post = Post(chat_id=chat_id, user_prompt=prompt, use_github=use_github)
        await self._posts.save(post)
        await self._task_queue.enqueue(
            "run_generation", {"chat_id": chat_id, "post_id": post.id}
        )

    async def _ask_discard(
        self, chat_id: str, active: Post, new_prompt: str, new_use_github: bool
    ) -> None:
        state = await self._chats.get(chat_id) or ChatState(
            chat_id=chat_id, expires_at=self._ttl()
        )
        state.pending_new_command = PendingNewCommand(
            user_prompt=new_prompt, use_github=new_use_github
        )
        state.updated_at = _utcnow()
        await self._chats.save(state)

        preview = active.user_prompt
        if len(preview) > 80:
            preview = preview[:80] + "..."
        await self._telegram.send_message(
            chat_id=chat_id,
            text=format_discard_question(preview, active.status.value),
            reply_markup=discard_keyboard(chat_id),
        )

    # ====================================================================== generation (Fase D)

    async def _run_generation_safely(self, chat_id: str, post_id: str) -> None:
        try:
            await self._run_generation(chat_id, post_id)
        except Exception as exc:
            await self._handle_generation_failure(chat_id, post_id, exc)

    async def _run_generation(self, chat_id: str, post_id: str) -> None:
        post = await self._posts.get(post_id)
        if post is None:
            return

        # 1. Pesquisa web (sempre).
        post.transition_to(PostStatus.RESEARCHING)
        await self._posts.save(post)
        author_context = self._build_author_context(post.use_github)
        web = await self._anthropic.research_with_web_search(
            post.user_prompt, author_context=author_context
        )
        post.research_summary = web.summary

        # 2. GitHub (opcional).
        gh = None
        if post.use_github:
            gh = await self._github.search(post.user_prompt)
            post.github_findings = gh.summary
        await self._posts.save(post)

        # 3. Mensagem de inspeção de insumos.
        await self._telegram.send_message(
            chat_id=chat_id,
            text=format_insights_message(post.user_prompt, web, gh),
        )

        # 4. GENERATING — body + imagem em paralelo, race-free.
        post.transition_to(PostStatus.GENERATING)
        await self._posts.save(post)

        body_pt, body_en, image_payload = await self._generate_body_and_image(post)

        # 5. Mutação sequencial — dono único do post depois do gather.
        post.body_pt = body_pt
        post.body_en = body_en

        if image_payload is None:
            # Imagem falhou: o handler interno já enviou o aviso. Post vai sem foto.
            post.image_prompt = None
            post.image_url = None
            post.image_gcs_path = None
            display_image_url: str | None = None
        else:
            post.image_prompt = image_payload[0]
            post.image_url = image_payload[1]
            post.image_gcs_path = image_payload[2]
            display_image_url = image_payload[1]

        post.transition_to(PostStatus.AWAITING_APPROVAL)
        await self._send_for_approval(chat_id, post, image_url=display_image_url)

    async def _generate_body_and_image(
        self, post: Post
    ) -> tuple[str, str, tuple[str, str, str] | None]:
        """Roda body e pipeline de imagem em paralelo (gather race-free).

        Body falha → re-raise (vira REJECTED no _run_generation_safely).
        Imagem falha → manda aviso AGORA e retorna image_payload=None pro caller seguir sem foto.

        Tasks são read-only sobre o `post` (passado por referência) — nenhuma mutação
        rola até este método retornar e o caller aplicar os valores sequencialmente.
        """
        body_result, image_result = await asyncio.gather(
            self._build_body_only(post),
            self._build_image_pipeline(post),
            return_exceptions=True,
        )

        if isinstance(body_result, BaseException):
            raise body_result  # → REJECTED via _run_generation_safely

        body_pt, body_en = body_result

        if isinstance(image_result, BaseException):
            await self._telegram.send_message(
                chat_id=post.chat_id,
                text=format_image_failure_notice(_short(image_result)),
            )
            return body_pt, body_en, None

        image_prompt, signed_url, gs_path = image_result
        return body_pt, body_en, (image_prompt, signed_url, gs_path)

    async def _build_body_only(self, post: Post) -> tuple[str, str]:
        """Task pura: lê post, retorna (pt, en). Não muta nada."""
        return await self._post_generator.generate_body(post)

    async def _build_image_pipeline(self, post: Post) -> tuple[str, str, str]:
        """Task pura: prompt → image URL → upload GCS → (prompt, signed_url, gs_path)."""
        image_prompt = await self._post_generator.generate_image_prompt(post)
        raw_url = await self._replicate.generate(image_prompt)
        signed_url, gs_path = await self._gcs.store(raw_url, post.id)
        return image_prompt, signed_url, gs_path

    async def _handle_generation_failure(
        self, chat_id: str, post_id: str, error: Exception
    ) -> None:
        summary = _short(error)

        post = await self._posts.get(post_id)
        if post is not None:
            post.rejection_cause = RejectionCause.GENERATION_FAILURE
            post.rejection_detail = summary
            if not is_terminal(post.status):
                post.transition_to(PostStatus.REJECTED)
            await self._posts.save(post)

        # Mensagem dedicada pra parse bilíngue falho — root cause é LLM, não infra.
        if isinstance(error, BilingualParseError):
            notice = format_bilingual_parse_failure_message(summary)
        else:
            notice = format_failure_message(summary)
        await self._telegram.send_message(chat_id=chat_id, text=notice)

    async def _send_for_approval(
        self,
        chat_id: str,
        post: Post,
        *,
        image_url: str | None = None,
        header: str = "",
    ) -> None:
        # Q5(a): foto primeiro (com caption truncado), texto bilíngue + botões depois.
        if image_url:
            await self._telegram.send_photo(
                chat_id=chat_id,
                photo=image_url,
                caption=format_caption_short(post.body_pt or ""),
            )

        text = header + format_bilingual_post(post.body_pt or "", post.body_en or "")
        sent = await self._telegram.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=approval_keyboard(post.id),
        )
        post.telegram_approval_message_id = sent["result"]["message_id"]
        await self._posts.save(post)

    # ====================================================================== buttons

    async def handle_approval(
        self, chat_id: str, post_id: str, callback_query_id: str
    ) -> None:
        """Aprova e dispara publicação em background.

        Idempotência (Fase F): se o post já estiver em qualquer estado != AWAITING_APPROVAL
        (APPROVED, PUBLISHED, SIMULATED, REJECTED), o callback é ignorado. Cobre:
          - Duplo clique no Telegram (callback reentregue OU user clicou rápido)
          - Race teórica entre 2 webhooks chegando "ao mesmo tempo"
        A transição AWAITING_APPROVAL → APPROVED ANTES de spawnar o publish task
        fecha a janela: 2º callback vê status==APPROVED e early-returna.
        """
        post = await self._posts.get(post_id)
        if not post or post.status != PostStatus.AWAITING_APPROVAL:
            await self._telegram.answer_callback_query(
                callback_query_id, text="Já processado."
            )
            return

        # Transição imediata pra fechar a janela de idempotência.
        post.transition_to(PostStatus.APPROVED)
        await self._posts.save(post)

        await self._telegram.answer_callback_query(
            callback_query_id, text="✅ Aprovado, publicando..."
        )
        if post.telegram_approval_message_id is not None:
            body = format_bilingual_post(post.body_pt or "", post.body_en or "")
            await self._telegram.edit_message_text(
                chat_id=chat_id,
                message_id=post.telegram_approval_message_id,
                text=body + PUBLISHING_FOOTER,
            )

        # Publicação enfileirada no Cloud Tasks — webhook responde 200 imediato.
        await self._task_queue.enqueue(
            "run_publish", {"chat_id": chat_id, "post_id": post.id}
        )

    async def _run_publish_safely(self, chat_id: str, post_id: str) -> None:
        try:
            await self._run_publish(chat_id, post_id)
        except TokenExpiredError as exc:
            await self._handle_token_expired(chat_id, post_id, exc)
        except Exception as exc:
            await self._handle_publication_failure(chat_id, post_id, exc)

    async def _run_publish(self, chat_id: str, post_id: str) -> None:
        post = await self._posts.get(post_id)
        # Guard adicional: se 2 tasks foram spawned (não deve, mas defesa em profundidade),
        # apenas a primeira encontra APPROVED.
        if post is None or post.status != PostStatus.APPROVED:
            return

        result = await self._linkedin.publish(post)

        if result.dry_run:
            await self._finalize_simulated(chat_id, post, result)
            return

        # Real: marca PUBLISHED. URN pode vir None (2xx sem x-restli-id) — tratado.
        post.linkedin_post_urn = result.post_urn
        post.transition_to(PostStatus.PUBLISHED)
        await self._posts.save(post)
        await self._notify_published(chat_id, post)

    async def _finalize_simulated(
        self, chat_id: str, post: Post, result
    ) -> None:
        """Dry-run: marca SIMULATED, envia payload em chunks + atualiza msg do post."""
        post.transition_to(PostStatus.SIMULATED)
        await self._posts.save(post)

        # 1. Envia o payload (potencialmente em múltiplas mensagens — HTML pra <pre>).
        payload_json = _pretty_json(result.payload_sent)
        for chunk in format_dry_run_chunks(payload_json):
            await self._telegram.send_message(
                chat_id=chat_id, text=chunk, parse_mode="HTML"
            )

        # 2. Edita a mensagem do post pra deixar claro que foi simulado.
        if post.telegram_approval_message_id is not None:
            body = format_bilingual_post(post.body_pt or "", post.body_en or "")
            await self._telegram.edit_message_text(
                chat_id=chat_id,
                message_id=post.telegram_approval_message_id,
                text=body + SIMULATED_FOOTER,
            )

    async def _notify_published(self, chat_id: str, post: Post) -> None:
        body = format_bilingual_post(post.body_pt or "", post.body_en or "")
        footer = (
            format_published_footer(post.linkedin_post_urn)
            if post.linkedin_post_urn
            else PUBLISHED_WITHOUT_URN_FOOTER
        )
        if post.telegram_approval_message_id is not None:
            await self._telegram.edit_message_text(
                chat_id=chat_id,
                message_id=post.telegram_approval_message_id,
                text=body + footer,
            )

    async def _handle_token_expired(
        self, chat_id: str, post_id: str, error: Exception
    ) -> None:
        post = await self._posts.get(post_id)
        if post is not None:
            post.rejection_cause = RejectionCause.TOKEN_EXPIRED
            post.rejection_detail = _short(error)
            if not is_terminal(post.status):
                post.transition_to(PostStatus.REJECTED)
            await self._posts.save(post)
        await self._telegram.send_message(
            chat_id=chat_id, text=format_token_expired_message()
        )

    async def _handle_publication_failure(
        self, chat_id: str, post_id: str, error: Exception
    ) -> None:
        summary = _short(error)
        post = await self._posts.get(post_id)
        if post is not None:
            post.rejection_cause = RejectionCause.PUBLICATION_FAILURE
            post.rejection_detail = summary
            if not is_terminal(post.status):
                post.transition_to(PostStatus.REJECTED)
            await self._posts.save(post)
        await self._telegram.send_message(
            chat_id=chat_id, text=format_publication_failure_message(summary)
        )

    async def handle_rejection(
        self, chat_id: str, post_id: str, callback_query_id: str
    ) -> None:
        post = await self._posts.get(post_id)
        if not post or post.status != PostStatus.AWAITING_APPROVAL:
            await self._telegram.answer_callback_query(
                callback_query_id, text="Já processado."
            )
            return

        # Fase E: limite atingido — substitui keyboard (não transiciona, deixa o user
        # escolher entre aprovar a última versão ou cancelar).
        #
        # Semântica: revision_count é o número de revisões JÁ APLICADAS. Com MAX=5,
        # o user faz revisões #1..#5 (count vai a 1, 2, 3, 4, 5) e ao tentar reprovar
        # uma 6ª vez (count==5), entra no limit flow. Com MAX=1, faz 1 revisão e o
        # limit flow aparece na 2ª tentativa de reprovação.
        if post.revision_count >= self._settings.max_revision_iterations:
            await self._telegram.answer_callback_query(
                callback_query_id, text="⚠️ Limite de revisões atingido"
            )
            if post.telegram_approval_message_id is not None:
                body = format_bilingual_post(post.body_pt or "", post.body_en or "")
                await self._telegram.edit_message_text(
                    chat_id=chat_id,
                    message_id=post.telegram_approval_message_id,
                    text=body + LIMIT_REACHED_FOOTER,
                    reply_markup=limit_reached_keyboard(post.id),
                )
            return

        await self._telegram.answer_callback_query(
            callback_query_id, text="❌ Me diga o motivo"
        )
        post.transition_to(PostStatus.REVISING)
        await self._posts.save(post)
        await self._chats.save(
            ChatState(
                chat_id=chat_id,
                awaiting_revision_for_post_id=post_id,
                expires_at=self._ttl(),
            )
        )
        if post.telegram_approval_message_id is not None:
            await self._telegram.edit_message_text(
                chat_id=chat_id,
                message_id=post.telegram_approval_message_id,
                text=format_bilingual_post(post.body_pt or "", post.body_en or "")
                + "\n\n" + ASKING_REASON_MESSAGE,
            )

    async def handle_cancel(
        self, chat_id: str, post_id: str, callback_query_id: str
    ) -> None:
        """Cancela um post no flow do limite (Fase E) — marca REJECTED."""
        await self._telegram.answer_callback_query(callback_query_id, text="🚫 Cancelado")
        post = await self._posts.get(post_id)
        if not post or is_terminal(post.status):
            return  # idempotente — clique duplo OK
        post.rejection_cause = RejectionCause.USER_CANCELLED_AT_LIMIT
        post.transition_to(PostStatus.REJECTED)
        await self._posts.save(post)
        if post.telegram_approval_message_id is not None:
            body = format_bilingual_post(post.body_pt or "", post.body_en or "")
            await self._telegram.edit_message_text(
                chat_id=chat_id,
                message_id=post.telegram_approval_message_id,
                text=body + CANCELLED_AT_LIMIT_FOOTER,
            )

    async def handle_discard_decision(
        self,
        chat_id: str,
        accept: bool,
        callback_query_id: str,
        original_message_id: int,
    ) -> None:
        state = await self._chats.get(chat_id)
        if not state or not state.pending_new_command:
            await self._telegram.answer_callback_query(
                callback_query_id, text="Já processado."
            )
            return
        pending = state.pending_new_command

        if accept:
            active = await self._posts.find_active_for_chat(chat_id)
            if active is not None:
                active.rejection_cause = RejectionCause.DISCARDED_BY_NEW_COMMAND
                active.rejection_detail = pending.user_prompt
                if not is_terminal(active.status):
                    active.transition_to(PostStatus.REJECTED)
                await self._posts.save(active)
            await self._chats.delete(chat_id)
            await self._telegram.answer_callback_query(
                callback_query_id, text="Pendente descartado."
            )
            await self._telegram.edit_message_text(
                chat_id=chat_id,
                message_id=original_message_id,
                text=f'✅ Pendente descartado. Processando novo: "{pending.user_prompt}"',
            )
            novo = Post(
                chat_id=chat_id,
                user_prompt=pending.user_prompt,
                use_github=pending.use_github,
            )
            await self._posts.save(novo)
            await self._task_queue.enqueue(
                "run_generation", {"chat_id": chat_id, "post_id": novo.id}
            )
        else:
            state.pending_new_command = None
            state.updated_at = _utcnow()
            await self._chats.save(state)
            await self._telegram.answer_callback_query(
                callback_query_id, text="Mantido."
            )
            await self._telegram.edit_message_text(
                chat_id=chat_id,
                message_id=original_message_id,
                text=DISCARDED_KEEP_PENDING,
            )

    # ====================================================================== free text

    async def handle_free_text(self, chat_id: str, text: str) -> None:
        state = await self._chats.get(chat_id)
        if not state:
            return

        if state.pending_new_command is not None:
            await self._telegram.send_message(
                chat_id=chat_id, text=DISCARD_PENDING_TEXT_MESSAGE
            )
            return

        if not state.awaiting_revision_for_post_id:
            return

        post = await self._posts.get(state.awaiting_revision_for_post_id)
        if not post or post.status != PostStatus.REVISING:
            await self._chats.delete(chat_id)
            return

        await self._chats.delete(chat_id)
        post.revision_feedback.append(text)
        post.revision_count += 1
        await self._posts.save(post)

        await self._task_queue.enqueue(
            "run_revision",
            {"chat_id": chat_id, "post_id": post.id, "reason": text},
        )

    async def _run_revision_safely(
        self, chat_id: str, post_id: str, reason: str
    ) -> None:
        try:
            await self._run_revision(chat_id, post_id, reason)
        except Exception as exc:
            await self._handle_generation_failure(chat_id, post_id, exc)

    async def _run_revision(self, chat_id: str, post_id: str, reason: str) -> None:
        """Regenera body + imagem reaproveitando research_summary/github_findings.

        Decisão MVP: qualquer feedback dispara regeneração de AMBOS body e imagem
        (não detectamos "feedback só sobre imagem/texto"). Custo ~$0.05/revisão.
        """
        post = await self._posts.get(post_id)
        if post is None or post.status != PostStatus.REVISING:
            return
        post.transition_to(PostStatus.GENERATING)
        await self._posts.save(post)

        body_pt, body_en, image_payload = await self._generate_body_and_image(post)

        post.body_pt = body_pt
        post.body_en = body_en

        if image_payload is None:
            post.image_prompt = None
            post.image_url = None
            post.image_gcs_path = None
            display_image_url: str | None = None
        else:
            post.image_prompt = image_payload[0]
            post.image_url = image_payload[1]
            post.image_gcs_path = image_payload[2]
            display_image_url = image_payload[1]

        post.transition_to(PostStatus.AWAITING_APPROVAL)
        await self._send_for_approval(
            chat_id,
            post,
            image_url=display_image_url,
            header=format_revision_header(
                post.revision_count,
                self._settings.max_revision_iterations,
                reason,
            ),
        )
