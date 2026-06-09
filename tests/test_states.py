import pytest

from bot_posts_linkedin.domain.states import (
    InvalidStateTransition,
    PostStatus,
    assert_transition,
    is_terminal,
)


def test_transicao_valida_draft_para_researching() -> None:
    assert_transition(PostStatus.DRAFT, PostStatus.RESEARCHING)


def test_transicao_invalida_draft_para_published_levanta() -> None:
    with pytest.raises(InvalidStateTransition):
        assert_transition(PostStatus.DRAFT, PostStatus.PUBLISHED)


def test_loop_de_revisao_completo() -> None:
    # Caminho real: usuário reprova → bot pede motivo → regenera → volta para aprovação.
    assert_transition(PostStatus.AWAITING_APPROVAL, PostStatus.REVISING)
    assert_transition(PostStatus.REVISING, PostStatus.GENERATING)
    assert_transition(PostStatus.GENERATING, PostStatus.AWAITING_APPROVAL)


def test_qualquer_estado_nao_terminal_pode_ir_para_rejected() -> None:
    for status in PostStatus:
        if not is_terminal(status):
            assert_transition(status, PostStatus.REJECTED)


def test_published_e_terminal() -> None:
    assert is_terminal(PostStatus.PUBLISHED)
    with pytest.raises(InvalidStateTransition):
        assert_transition(PostStatus.PUBLISHED, PostStatus.DRAFT)


def test_rejected_e_terminal() -> None:
    assert is_terminal(PostStatus.REJECTED)


def test_draft_nao_e_terminal() -> None:
    assert not is_terminal(PostStatus.DRAFT)
