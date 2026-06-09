from enum import StrEnum


class PostStatus(StrEnum):
    """Estados do post entre o comando do Telegram e a publicação no LinkedIn."""

    DRAFT = "draft"
    RESEARCHING = "researching"
    GENERATING = "generating"
    AWAITING_APPROVAL = "awaiting_approval"
    REVISING = "revising"
    APPROVED = "approved"
    PUBLISHED = "published"
    # Dry-run terminal (Fase F): payload foi montado e mostrado, mas NÃO posted real.
    SIMULATED = "simulated"
    REJECTED = "rejected"


# Transições válidas. Chave = estado atual, valor = conjunto de estados-alvo permitidos.
# REVISING → GENERATING fecha o loop de reprovação: motivo informado → regenera → nova aprovação.
_VALID_TRANSITIONS: dict[PostStatus, set[PostStatus]] = {
    PostStatus.DRAFT: {PostStatus.RESEARCHING, PostStatus.REJECTED},
    PostStatus.RESEARCHING: {PostStatus.GENERATING, PostStatus.REJECTED},
    PostStatus.GENERATING: {PostStatus.AWAITING_APPROVAL, PostStatus.REJECTED},
    PostStatus.AWAITING_APPROVAL: {
        PostStatus.APPROVED,
        PostStatus.REVISING,
        PostStatus.REJECTED,
    },
    PostStatus.REVISING: {PostStatus.GENERATING, PostStatus.REJECTED},
    PostStatus.APPROVED: {PostStatus.PUBLISHED, PostStatus.SIMULATED, PostStatus.REJECTED},
    PostStatus.PUBLISHED: set(),
    PostStatus.SIMULATED: set(),
    PostStatus.REJECTED: set(),
}


class InvalidStateTransition(ValueError):
    """Levantada quando se tenta transição não permitida pela máquina de estados."""


def assert_transition(current: PostStatus, target: PostStatus) -> None:
    """Valida a transição. Não retorna nada em sucesso; levanta em falha."""
    if target not in _VALID_TRANSITIONS[current]:
        raise InvalidStateTransition(
            f"Transição inválida: {current.value} → {target.value}"
        )


def is_terminal(status: PostStatus) -> bool:
    """True se PUBLISHED ou REJECTED — estados sem saídas válidas."""
    return not _VALID_TRANSITIONS[status]
