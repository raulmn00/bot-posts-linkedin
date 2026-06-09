from datetime import UTC, datetime
from uuid import uuid4

from pydantic import BaseModel, Field

from bot_posts_linkedin.domain.rejection_cause import RejectionCause
from bot_posts_linkedin.domain.states import PostStatus, assert_transition


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Post(BaseModel):
    """Estado completo de um post no fluxo gerar → aprovar → publicar."""

    id: str = Field(default_factory=lambda: uuid4().hex)

    # Quem disparou — usado pra dedup de comandos concorrentes (find_active_for_chat).
    chat_id: str

    # Entrada do usuário ----------------------------------------------------
    user_prompt: str  # texto após [GERAR-POST]
    use_github: bool = False  # True quando a flag [GITHUB] está presente

    # Máquina de estados ---------------------------------------------------
    status: PostStatus = PostStatus.DRAFT
    revision_count: int = 0  # incrementado a cada loop de reprovação

    # Insumos coletados (Fase C) -------------------------------------------
    research_summary: str | None = None
    github_findings: str | None = None

    # Conteúdo gerado (Fase D) ---------------------------------------------
    body_pt: str | None = None
    body_en: str | None = None
    image_prompt: str | None = None  # sempre em PT
    image_url: str | None = None  # URL temporária do Replicate
    image_gcs_path: str | None = None  # gs://bucket/path persistido

    # Feedback do usuário a cada reprovação (Fase E) -----------------------
    # SOMENTE motivos humanos que o user digitou no Telegram. Sinais de controle
    # (cancelamento, descarte, falha) vão em rejection_cause/rejection_detail.
    revision_feedback: list[str] = Field(default_factory=list)

    # Motivo estrutural quando o post termina em REJECTED — None se ainda ativo
    # ou se foi rejeitado por motivo não categorizado (não deveria acontecer).
    rejection_cause: RejectionCause | None = None
    rejection_detail: str | None = None  # texto livre auxiliar (erro, prompt do novo cmd, etc.)

    # Telegram tracking — pra editar a mensagem certa nos callbacks (Fase B)
    telegram_approval_message_id: int | None = None

    # LinkedIn (Fase F) ----------------------------------------------------
    linkedin_post_urn: str | None = None

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def transition_to(self, target: PostStatus) -> None:
        """Aplica uma transição validada e atualiza updated_at.

        Mantém o Post como único ponto de truth sobre transições — quem chama
        não precisa lembrar de validar separado nem mexer em updated_at.
        """
        assert_transition(self.status, target)
        self.status = target
        self.updated_at = _utcnow()
