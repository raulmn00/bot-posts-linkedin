import asyncio

from bot_posts_linkedin.domain.chat_state import ChatState


class InMemoryChatStateStore:
    """ChatStateStore em memória — dev/test. Expirados são tratados como inexistentes."""

    def __init__(self) -> None:
        self._data: dict[str, ChatState] = {}
        self._lock = asyncio.Lock()

    async def save(self, state: ChatState) -> None:
        async with self._lock:
            self._data[state.chat_id] = state.model_copy(deep=True)

    async def get(self, chat_id: str) -> ChatState | None:
        async with self._lock:
            stored = self._data.get(chat_id)
            if stored is None or stored.is_expired():
                # Expirado: limpa proativamente — economiza memória e mantém store enxuto.
                if stored is not None:
                    self._data.pop(chat_id, None)
                return None
            return stored.model_copy(deep=True)

    async def delete(self, chat_id: str) -> None:
        async with self._lock:
            self._data.pop(chat_id, None)
