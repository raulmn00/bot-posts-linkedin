from typing import Protocol, runtime_checkable

from bot_posts_linkedin.domain.chat_state import ChatState


@runtime_checkable
class ChatStateStore(Protocol):
    """Persistência do ChatState. Em prod = Firestore (Fase G); em dev/test = in-memory."""

    async def save(self, state: ChatState) -> None:
        """Upsert pelo chat_id."""
        ...

    async def get(self, chat_id: str) -> ChatState | None:
        """Retorna None se não existir OU se estiver expirado.

        Expirado conta como inexistente — o caller não precisa saber a diferença.
        """
        ...

    async def delete(self, chat_id: str) -> None:
        """Idempotente — deletar inexistente não erra."""
        ...
