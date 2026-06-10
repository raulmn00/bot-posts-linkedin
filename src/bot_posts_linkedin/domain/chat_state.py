from datetime import UTC, datetime

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PendingNewCommand(BaseModel):
    """Comando novo enviado pelo user enquanto havia um post pendente em REVISING.

    Fica "pausado" dentro do ChatState até o user confirmar via botão se quer
    descartar o pendente (vira novo) ou manter (descarta este pending_new_command).
    """

    user_prompt: str
    use_github: bool


class ChatState(BaseModel):
    """Estado da conversa entre o bot e um chat_id.

    Resolve dois problemas que não cabem no Post:
      1) Saber qual post está aguardando motivo de reprovação (chave: chat_id).
      2) Memorizar um comando novo até o user decidir o que fazer com o pendente.
    """

    chat_id: str
    awaiting_revision_for_post_id: str | None = None
    pending_new_command: PendingNewCommand | None = None
    expires_at: datetime  # passado: o get() do store devolve None
    updated_at: datetime = Field(default_factory=_utcnow)

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or _utcnow()) >= self.expires_at
