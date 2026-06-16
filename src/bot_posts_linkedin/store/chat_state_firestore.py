"""Firestore ChatStateStore — persistência real do ChatState.

TTL aplicado no `get()` da mesma forma que o InMemory: se expirado, retorna None
e remove proativamente do storage. Mantém semântica idêntica entre os dois impls.

Quando subir Firestore TTL nativo (Fase G.4), basta configurar `expires_at`
como TTL field na console — o get() continua funcionando porque o doc nem chega
mais ao cliente.
"""

from typing import Any

from google.cloud import firestore

from bot_posts_linkedin.domain.chat_state import ChatState


class FirestoreChatStateStore:
    def __init__(
        self,
        *,
        project_id: str,
        collection: str = "chat_states",
        client: firestore.AsyncClient | None = None,
    ) -> None:
        self._collection = collection
        self._client = client or firestore.AsyncClient(project=project_id)

    def _doc_ref(self, chat_id: str):
        return self._client.collection(self._collection).document(chat_id)

    async def save(self, state: ChatState) -> None:
        await self._doc_ref(state.chat_id).set(_to_doc(state))

    async def get(self, chat_id: str) -> ChatState | None:
        snap = await self._doc_ref(chat_id).get()
        if not snap.exists:
            return None
        state = _from_doc(snap.to_dict() or {})
        if state.is_expired():
            # Limpa proativamente (mesma estratégia do InMemory).
            await self._doc_ref(chat_id).delete()
            return None
        return state

    async def delete(self, chat_id: str) -> None:
        # Firestore delete é idempotente — não falha se o doc não existe.
        await self._doc_ref(chat_id).delete()


def _to_doc(state: ChatState) -> dict[str, Any]:
    return state.model_dump(mode="python")


def _from_doc(data: dict[str, Any]) -> ChatState:
    return ChatState.model_validate(data)
