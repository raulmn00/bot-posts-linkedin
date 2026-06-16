"""Dedup por update_id do Telegram (Fase G.3).

Telegram pode reentregar callbacks/updates se o webhook demorar pra responder
200 (timeout ~60s) — sem dedup, isso pode causar publicação duplicada.

Estratégia:
  - Toda vez que recebemos um update, marcamos `update_id` no Firestore com
    `expires_at = now + TTL` (default 10min).
  - Antes de processar, checamos se já existe doc pro update_id; se sim, ignora.
  - TTL nativo do Firestore (config via console/script) garante limpeza
    automática sem precisar cron.

Por que 10min é suficiente: Telegram desiste de retry após ~5min. Margem 2×.
"""

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class UpdateDedupStore(Protocol):
    async def already_processed(self, update_id: int) -> bool:
        """True se o update_id já foi marcado dentro do TTL."""
        ...

    async def mark_processed(self, update_id: int) -> None:
        """Marca update_id como processado com expires_at = now + TTL."""
        ...


class FirestoreUpdateDedupStore:
    """Persiste update_ids processados no Firestore com TTL.

    Doc ID = str(update_id). Doc carrega `processed_at` (audit) + `expires_at`
    (consumido pelo TTL nativo do Firestore configurado na collection).
    """

    def __init__(
        self,
        *,
        project_id: str,
        collection: str = "processed_updates",
        ttl_minutes: int = 10,
        client: Any = None,
    ) -> None:
        from google.cloud import firestore  # noqa: PLC0415

        self._collection = collection
        self._ttl = timedelta(minutes=ttl_minutes)
        self._client = client or firestore.AsyncClient(project=project_id)

    def _doc_ref(self, update_id: int):
        return self._client.collection(self._collection).document(str(update_id))

    async def already_processed(self, update_id: int) -> bool:
        snap = await self._doc_ref(update_id).get()
        if not snap.exists:
            return False
        # Em caso de race (TTL ainda não limpou doc velho), conferir expires_at.
        data = snap.to_dict() or {}
        expires_at = data.get("expires_at")
        if expires_at is None:
            return True
        # Se o TTL passou mas o doc ainda não foi limpo, trata como não processado.
        return datetime.now(UTC) < expires_at

    async def mark_processed(self, update_id: int) -> None:
        now = datetime.now(UTC)
        await self._doc_ref(update_id).set(
            {"processed_at": now, "expires_at": now + self._ttl}
        )
