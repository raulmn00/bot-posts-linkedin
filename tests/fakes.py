"""Fakes para isolar testes de IO real.

Cada Fake satisfaz o Protocol correspondente e registra todas as chamadas em listas
que os testes inspecionam — `isinstance(fake, ProtocolDoNosso)` retorna True.
"""

from typing import Any

from bot_posts_linkedin.domain.insights import (
    GithubFinding,
    GithubFindings,
    WebResearchResult,
)
from bot_posts_linkedin.services.linkedin_publisher import (
    PublicationResult,
    TokenExpiredError,
)


class FakeTelegramClient:
    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []
        self.sent_photos: list[dict[str, Any]] = []
        self.edited_messages: list[dict[str, Any]] = []
        self.answered_callbacks: list[dict[str, Any]] = []
        self.set_webhook_calls: list[dict[str, Any]] = []
        self.deleted_webhook_count = 0
        self.closed = False
        self._next_message_id = 100

    async def send_message(
        self,
        *,
        chat_id: str | int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        message_id = self._next_message_id
        self._next_message_id += 1
        self.sent_messages.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
                "message_id": message_id,
            }
        )
        return {"ok": True, "result": {"message_id": message_id, "text": text}}

    async def send_photo(
        self,
        *,
        chat_id: str | int,
        photo: str,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        message_id = self._next_message_id
        self._next_message_id += 1
        self.sent_photos.append(
            {
                "chat_id": chat_id,
                "photo": photo,
                "caption": caption,
                "parse_mode": parse_mode,
                "message_id": message_id,
            }
        )
        return {"ok": True, "result": {"message_id": message_id}}

    async def edit_message_text(
        self,
        *,
        chat_id: str | int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        self.edited_messages.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
            }
        )
        return {"ok": True, "result": {"message_id": message_id, "text": text}}

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> dict[str, Any]:
        self.answered_callbacks.append({"id": callback_query_id, "text": text})
        return {"ok": True, "result": True}

    async def set_webhook(self, url: str, secret_token: str) -> dict[str, Any]:
        self.set_webhook_calls.append({"url": url, "secret_token": secret_token})
        return {"ok": True, "result": True}

    async def delete_webhook(self) -> dict[str, Any]:
        self.deleted_webhook_count += 1
        return {"ok": True, "result": True}

    async def close(self) -> None:
        self.closed = True


class FakeAnthropicClient:
    """Devolve respostas fixas; testes podem sobrescrever via set_*."""

    def __init__(self) -> None:
        self.research_calls: list[dict[str, Any]] = []
        self.chat_calls: list[dict[str, Any]] = []
        self.closed = False
        self._research_response = WebResearchResult(
            summary="[fake research] resumo da pesquisa web sobre o assunto",
            sources=["https://exemplo.com/fonte1", "https://exemplo.com/fonte2"],
        )
        self._chat_response = '{"top": ["repo-a", "repo-b", "repo-c"]}'
        self._raise_research: Exception | None = None
        self._raise_chat: Exception | None = None

    def set_research_response(self, response: WebResearchResult) -> None:
        self._research_response = response

    def set_chat_response(self, text: str) -> None:
        self._chat_response = text

    def set_research_error(self, exc: Exception) -> None:
        self._raise_research = exc

    def set_chat_error(self, exc: Exception) -> None:
        self._raise_chat = exc

    async def research_with_web_search(
        self, topic: str, *, author_context: str | None = None
    ) -> WebResearchResult:
        self.research_calls.append({"topic": topic, "author_context": author_context})
        if self._raise_research is not None:
            raise self._raise_research
        return self._research_response

    async def chat(self, prompt: str, system: str | None = None) -> str:
        self.chat_calls.append({"prompt": prompt, "system": system})
        if self._raise_chat is not None:
            raise self._raise_chat
        return self._chat_response

    async def close(self) -> None:
        self.closed = True


class FakePostGenerator:
    def __init__(self) -> None:
        self.body_calls: list[dict[str, Any]] = []
        self.image_prompt_calls: list[dict[str, Any]] = []
        self._body_response: tuple[str, str] = (
            "Post PT gerado pelo fake — texto plausível com conteúdo placeholder.",
            "Generated EN post — placeholder content from the fake.",
        )
        self._image_prompt_response = "Ilustração visual moderna sobre o tema."
        self._raise_body: Exception | None = None
        self._raise_image_prompt: Exception | None = None

    def set_body_response(self, body_pt: str, body_en: str) -> None:
        self._body_response = (body_pt, body_en)

    def set_image_prompt_response(self, prompt: str) -> None:
        self._image_prompt_response = prompt

    def set_body_error(self, exc: Exception) -> None:
        self._raise_body = exc

    def set_image_prompt_error(self, exc: Exception) -> None:
        self._raise_image_prompt = exc

    async def generate_body(self, post: Any) -> tuple[str, str]:
        self.body_calls.append({"post_id": post.id, "prompt": post.user_prompt})
        if self._raise_body is not None:
            raise self._raise_body
        return self._body_response

    async def generate_image_prompt(self, post: Any) -> str:
        self.image_prompt_calls.append({"post_id": post.id, "prompt": post.user_prompt})
        if self._raise_image_prompt is not None:
            raise self._raise_image_prompt
        return self._image_prompt_response


class FakeReplicateImageService:
    def __init__(self) -> None:
        self.generate_calls: list[dict[str, Any]] = []
        self.closed = False
        self._response = "https://replicate.example/image.png"
        self._raise: Exception | None = None
        self.credentials_validated = False

    def set_response(self, url: str) -> None:
        self._response = url

    def set_error(self, exc: Exception) -> None:
        self._raise = exc

    async def generate(self, prompt: str) -> str:
        self.generate_calls.append({"prompt": prompt})
        if self._raise is not None:
            raise self._raise
        return self._response

    async def validate_credentials(self) -> None:
        self.credentials_validated = True

    async def close(self) -> None:
        self.closed = True


class FakeGcsImageStorage:
    def __init__(self, bucket_name: str = "fake-bucket") -> None:
        self.store_calls: list[dict[str, Any]] = []
        self._bucket = bucket_name
        self._raise: Exception | None = None
        self.bucket_validated = False

    def set_error(self, exc: Exception) -> None:
        self._raise = exc

    async def store(self, image_url: str, post_id: str) -> tuple[str, str]:
        self.store_calls.append({"image_url": image_url, "post_id": post_id})
        if self._raise is not None:
            raise self._raise
        signed_url = f"https://storage.googleapis.com/{self._bucket}/posts/{post_id}.png?sig=fake"
        gs_path = f"gs://{self._bucket}/posts/{post_id}.png"
        return signed_url, gs_path

    async def validate_bucket(self) -> None:
        self.bucket_validated = True


class FakeLinkedInPublisher:
    """Devolve resultado configurável; testes podem forçar dry_run, erros, etc."""

    def __init__(self, *, dry_run: bool = False) -> None:
        self.publish_calls: list[dict[str, Any]] = []
        self.validate_count = 0
        self.closed = False
        self._dry_run = dry_run
        # Default real-mode response — URN preenchido.
        self._real_response = PublicationResult(
            dry_run=False,
            payload_sent={"mock": "real_payload"},
            post_urn="urn:li:share:7000000000000000000",
            image_urn="urn:li:image:fake_real",
        )
        self._dry_run_response = PublicationResult(
            dry_run=True,
            payload_sent={
                "author": "urn:li:person:fake",
                "commentary": "PT\n\n━━━━━━━━━━━━━━━\n\nEN",
                "visibility": "PUBLIC",
            },
            post_urn=None,
            image_urn="urn:li:image:DRY_RUN_PLACEHOLDER_NAO_UPLOADED",
        )
        self._raise: Exception | None = None

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def set_dry_run(self, value: bool) -> None:
        self._dry_run = value

    def set_real_response(self, result: PublicationResult) -> None:
        self._real_response = result

    def set_dry_run_response(self, result: PublicationResult) -> None:
        self._dry_run_response = result

    def set_error(self, exc: Exception) -> None:
        self._raise = exc

    def set_token_expired(self) -> None:
        self._raise = TokenExpiredError("token expirado (fake)")

    async def publish(self, post: Any) -> PublicationResult:
        self.publish_calls.append({"post_id": post.id, "dry_run": self._dry_run})
        if self._raise is not None:
            raise self._raise
        return self._dry_run_response if self._dry_run else self._real_response

    async def validate_credentials(self) -> None:
        self.validate_count += 1

    async def close(self) -> None:
        self.closed = True


class FakeTaskQueueClient:
    """Em testes, execução é síncrona — mesma semântica do antigo asyncio.create_task.

    Pra isso funcionar, os tests precisam chamar `set_dispatch_target(service)`
    DEPOIS de construir o PostFlowService — assim o enqueue chama dispatch_task
    direto na mesma corrotina (sem rede).
    """

    def __init__(self) -> None:
        self.enqueue_calls: list[dict[str, Any]] = []
        self.closed = False
        self._dispatch_target = None  # type: ignore[var-annotated]
        self._raise: Exception | None = None

    def set_dispatch_target(self, service: Any) -> None:
        self._dispatch_target = service

    def set_error(self, exc: Exception) -> None:
        self._raise = exc

    async def enqueue(self, action: str, payload: dict[str, Any]) -> None:
        self.enqueue_calls.append({"action": action, "payload": payload})
        if self._raise is not None:
            raise self._raise
        if self._dispatch_target is not None:
            await self._dispatch_target.dispatch_task(action, payload)

    async def close(self) -> None:
        self.closed = True


class FakeUpdateDedupStore:
    """In-memory dedup store pra tests — sem TTL real."""

    def __init__(self) -> None:
        self._seen: set[int] = set()
        self.mark_calls: list[int] = []

    async def already_processed(self, update_id: int) -> bool:
        return update_id in self._seen

    async def mark_processed(self, update_id: int) -> None:
        self._seen.add(update_id)
        self.mark_calls.append(update_id)


class FakeGithubSearchService:
    def __init__(self) -> None:
        self.search_calls: list[dict[str, Any]] = []
        self._response = GithubFindings(
            summary="[fake github] achados nos repos públicos do raulmn00",
            repos=[
                GithubFinding(
                    name="raulmn00/agent-router",
                    url="https://github.com/raulmn00/agent-router",
                    description="DistilBERT-based agent router",
                    topics=["llm", "nlp"],
                    relevance_excerpt="trecho relevante do README",
                ),
                GithubFinding(
                    name="raulmn00/rag-hibrido",
                    url="https://github.com/raulmn00/rag-hibrido",
                    description="RAG híbrido com re-ranking",
                    topics=["rag"],
                    relevance_excerpt="trecho rag",
                ),
            ],
        )
        self._raise: Exception | None = None

    def set_response(self, response: GithubFindings) -> None:
        self._response = response

    def set_error(self, exc: Exception) -> None:
        self._raise = exc

    async def search(self, topic: str) -> GithubFindings:
        self.search_calls.append({"topic": topic})
        if self._raise is not None:
            raise self._raise
        return self._response
