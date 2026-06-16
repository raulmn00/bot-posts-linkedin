"""Firestore PostStore — persistência real do Post na coleção configurada.

Substitui o stub `NotImplementedError` da Fase A. Implementação usa o cliente
async oficial (`google.cloud.firestore.AsyncClient`) e converte Post ↔ dict via
pydantic — datetime vira Timestamp nativo do Firestore, StrEnum.value vira str.

Pré-requisitos:
  - Firestore (Native mode) habilitado no projeto (já criado no Passo 2)
  - SA tem `roles/datastore.user` (configurado pela G.1 em gcp_create_service_account.sh)
  - Composite index pro `find_active_for_chat` aplicado via firestore.indexes.json
    (script scripts/gcp_firestore_indexes.sh) — sem ele, a query levanta
    FailedPrecondition pedindo pra criar o index.
"""

from typing import Any

from google.cloud import firestore

from bot_posts_linkedin.domain.post import Post
from bot_posts_linkedin.domain.states import PostStatus

# Estados não-terminais usados em find_active_for_chat — não inclui PUBLISHED,
# SIMULATED, REJECTED (terminais; já saíram do ciclo de aprovação).
_NON_TERMINAL_STATUSES: list[str] = [
    PostStatus.DRAFT.value,
    PostStatus.RESEARCHING.value,
    PostStatus.GENERATING.value,
    PostStatus.AWAITING_APPROVAL.value,
    PostStatus.REVISING.value,
    PostStatus.APPROVED.value,
]


class FirestorePostStore:
    """Implementação Firestore do PostStore Protocol.

    Documentos em /<collection>/<post_id>. Cliente async pra integrar com o
    restante do flow sem `to_thread`.
    """

    def __init__(
        self,
        *,
        project_id: str,
        collection: str = "posts",
        client: firestore.AsyncClient | None = None,
    ) -> None:
        self._collection = collection
        # Cliente injetável pra testes futuros com emulator.
        self._client = client or firestore.AsyncClient(project=project_id)

    def _doc_ref(self, post_id: str):
        return self._client.collection(self._collection).document(post_id)

    async def save(self, post: Post) -> None:
        await self._doc_ref(post.id).set(_to_doc(post))

    async def get(self, post_id: str) -> Post | None:
        snap = await self._doc_ref(post_id).get()
        if not snap.exists:
            return None
        return _from_doc(snap.to_dict() or {})

    async def list_by_status(self, status: PostStatus) -> list[Post]:
        query = self._client.collection(self._collection).where(
            filter=firestore.FieldFilter("status", "==", status.value)
        )
        return [_from_doc(doc.to_dict() or {}) async for doc in query.stream()]

    async def find_active_for_chat(self, chat_id: str) -> Post | None:
        # Composite index obrigatório: (chat_id ASC, status ASC, created_at DESC).
        # Sem ele, Firestore levanta FailedPrecondition no primeiro stream.
        query = (
            self._client.collection(self._collection)
            .where(filter=firestore.FieldFilter("chat_id", "==", chat_id))
            .where(filter=firestore.FieldFilter("status", "in", _NON_TERMINAL_STATUSES))
            .order_by("created_at", direction=firestore.Query.DESCENDING)
            .limit(1)
        )
        async for doc in query.stream():
            return _from_doc(doc.to_dict() or {})
        return None


def _to_doc(post: Post) -> dict[str, Any]:
    """Converte Post → dict pronto pro Firestore.

    Pydantic model_dump() mantém datetime nativo (vira Timestamp) e converte
    StrEnum pra str (porque StrEnum.value já é str). Listas/dicts viajam como
    estão. None continua None.
    """
    data = post.model_dump(mode="python")
    # Garantia: enums string-based viram strings puras (não Enum instances)
    if isinstance(data.get("status"), PostStatus):
        data["status"] = data["status"].value
    if data.get("rejection_cause") is not None and not isinstance(data["rejection_cause"], str):
        data["rejection_cause"] = data["rejection_cause"].value
    return data


def _from_doc(data: dict[str, Any]) -> Post:
    """Converte dict do Firestore → Post via validação pydantic.

    Firestore Timestamps vêm como datetime aware (UTC). Pydantic re-valida e
    aplica defaults pra qualquer campo opcional que esteja ausente.
    """
    return Post.model_validate(data)
