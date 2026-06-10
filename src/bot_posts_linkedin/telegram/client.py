from typing import Any, Protocol, runtime_checkable

import httpx


@runtime_checkable
class TelegramClient(Protocol):
    """Interface mínima que o bot precisa do Bot API do Telegram.

    Implementações: HttpxTelegramClient (prod) e FakeTelegramClient (testes).
    Métodos retornam o JSON do Telegram (dict) pra preservar metadados como
    message_id, que precisamos pra editar a mensagem depois.
    """

    async def send_message(
        self,
        *,
        chat_id: str | int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]: ...

    async def send_photo(
        self,
        *,
        chat_id: str | int,
        photo: str,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]: ...

    async def edit_message_text(
        self,
        *,
        chat_id: str | int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]: ...

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
    ) -> dict[str, Any]: ...

    async def set_webhook(self, url: str, secret_token: str) -> dict[str, Any]: ...

    async def delete_webhook(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class TelegramAPIError(RuntimeError):
    """Resposta do Telegram com ok=false ou HTTP error."""


class HttpxTelegramClient:
    """Implementação concreta do TelegramClient usando httpx.AsyncClient.

    Reutiliza a mesma conexão entre chamadas (pool HTTP/2) — importante porque o bot
    faz várias chamadas em sequência (send + edit + answer_callback) por update.
    """

    def __init__(self, bot_token: str, *, timeout_seconds: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{bot_token}",
            timeout=timeout_seconds,
        )

    async def _post(self, method: str, payload: dict) -> dict[str, Any]:
        # Remove keys com valor None — o Telegram rejeita reply_markup=null em alguns métodos.
        payload = {k: v for k, v in payload.items() if v is not None}
        response = await self._client.post(f"/{method}", json=payload)
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise TelegramAPIError(
                f"Telegram API retornou ok=false em {method}: {data!r}"
            )
        return data

    async def send_message(
        self,
        *,
        chat_id: str | int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        return await self._post(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
            },
        )

    async def send_photo(
        self,
        *,
        chat_id: str | int,
        photo: str,
        caption: str | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        return await self._post(
            "sendPhoto",
            {
                "chat_id": chat_id,
                "photo": photo,
                "caption": caption,
                "parse_mode": parse_mode,
            },
        )

    async def edit_message_text(
        self,
        *,
        chat_id: str | int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        return await self._post(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
            },
        )

    async def answer_callback_query(
        self, callback_query_id: str, text: str | None = None
    ) -> dict[str, Any]:
        return await self._post(
            "answerCallbackQuery",
            {"callback_query_id": callback_query_id, "text": text},
        )

    async def set_webhook(self, url: str, secret_token: str) -> dict[str, Any]:
        # allowed_updates limita o que o Telegram nos envia — economiza tráfego e clarifica intent.
        return await self._post(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "callback_query"],
            },
        )

    async def delete_webhook(self) -> dict[str, Any]:
        return await self._post("deleteWebhook", {})

    async def close(self) -> None:
        await self._client.aclose()
