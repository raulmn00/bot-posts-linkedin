import pytest
from pydantic import ValidationError

from bot_posts_linkedin.config import Settings, get_settings


def test_settings_carregam_do_env() -> None:
    s = get_settings()
    assert s.env in {"dev", "prod"}
    assert s.anthropic_model == "claude-sonnet-4-6"
    assert s.max_revision_iterations == 5
    assert s.linkedin_person_urn.startswith("urn:li:person:")
    assert s.github_username == "raulmn00"


def test_urn_em_formato_invalido_e_rejeitado() -> None:
    # Passa o URN inválido como kwarg (sobrepõe o valor do .env)
    # e força a validação a falhar — exatamente como falharia no boot
    # caso alguém colocasse um URN errado no Secret Manager.
    with pytest.raises(ValidationError) as exc:
        Settings(linkedin_person_urn="formato-errado")  # type: ignore[call-arg]
    assert "urn:li:person:" in str(exc.value)
