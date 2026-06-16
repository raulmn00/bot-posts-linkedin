"""Testes do PostGenerator — parse bilíngue tolerante + insumos passados ao Claude."""

import pytest

from bot_posts_linkedin.domain.post import Post
from bot_posts_linkedin.services.post_generator import (
    BilingualParseError,
    ClaudePostGenerator,
    parse_bilingual,
)
from tests.fakes import FakeAnthropicClient

# ============================================================ parse_bilingual


def test_parse_basico() -> None:
    pt, en = parse_bilingual("Texto PT aqui\n\n===EN===\n\nEnglish text here")
    assert pt == "Texto PT aqui"
    assert en == "English text here"


def test_parse_tolerante_aceita_separadores_variados() -> None:
    casos = [
        "PT\n====EN====\nEN",
        "PT\n=== EN ===\nEN",
        "PT\n======== EN ========\nEN",
        "PT\n===en===\nEN",  # lowercase
        "PT\n=== En ===\nEN",
    ]
    for raw in casos:
        pt, en = parse_bilingual(raw)
        assert pt == "PT" and en == "EN", f"falhou pra {raw!r}"


def test_parse_separador_ausente_levanta() -> None:
    with pytest.raises(BilingualParseError, match="separador"):
        parse_bilingual("só texto sem separador algum")


def test_parse_bloco_pt_vazio_levanta() -> None:
    with pytest.raises(BilingualParseError, match="vazio"):
        parse_bilingual("===EN===\nEN content")


def test_parse_bloco_en_vazio_levanta() -> None:
    with pytest.raises(BilingualParseError, match="vazio"):
        parse_bilingual("PT content\n===EN===\n   ")


def test_parse_normaliza_whitespace() -> None:
    pt, en = parse_bilingual("\n\n  PT longo  \n\n===EN===\n\n  EN longo  \n\n")
    assert pt == "PT longo"
    assert en == "EN longo"


# ============================================================ ClaudePostGenerator


@pytest.fixture
def system_prompt_file(tmp_path):
    f = tmp_path / "system.txt"
    f.write_text("System prompt para Raul.", encoding="utf-8")
    return str(f)


@pytest.mark.asyncio
async def test_generate_body_passa_insumos_no_prompt(system_prompt_file) -> None:
    anthropic = FakeAnthropicClient()
    anthropic.set_chat_response("PT body\n===EN===\nEN body")
    gen = ClaudePostGenerator(anthropic, system_prompt_file)

    post = Post(
        chat_id="x",
        user_prompt="rag híbrido",
        research_summary="resumo da pesquisa",
        github_findings="achados do github",
        revision_feedback=["primeiro feedback", "segundo"],
    )
    body_pt, body_en = await gen.generate_body(post)

    assert body_pt == "PT body"
    assert body_en == "EN body"
    # Verifica que o prompt repassou todos os insumos.
    call = anthropic.chat_calls[0]
    assert "rag híbrido" in call["prompt"]
    assert "resumo da pesquisa" in call["prompt"]
    assert "achados do github" in call["prompt"]
    assert "primeiro feedback" in call["prompt"]
    assert "segundo" in call["prompt"]
    # Regra de tamanho dura (sem hard cap o Claude estourou 3000 chars em prod).
    assert "1200 caracteres" in call["prompt"]
    assert "700 a 1000" in call["prompt"]
    assert "ANTES DE FECHAR" in call["prompt"]
    # System prompt veio do arquivo.
    assert "Raul" in call["system"]


@pytest.mark.asyncio
async def test_generate_body_sem_github_omite_secao(system_prompt_file) -> None:
    anthropic = FakeAnthropicClient()
    anthropic.set_chat_response("PT\n===EN===\nEN")
    gen = ClaudePostGenerator(anthropic, system_prompt_file)
    post = Post(chat_id="x", user_prompt="tema", research_summary="x", use_github=False)

    await gen.generate_body(post)

    prompt = anthropic.chat_calls[0]["prompt"]
    assert "GitHub" not in prompt  # sem flag → seção omitida


@pytest.mark.asyncio
async def test_generate_image_prompt_usa_research_summary(system_prompt_file) -> None:
    anthropic = FakeAnthropicClient()
    anthropic.set_chat_response("Ilustração minimalista de servidores em camadas.")
    gen = ClaudePostGenerator(anthropic, system_prompt_file)
    post = Post(
        chat_id="x",
        user_prompt="arquitetura distribuída",
        research_summary="resumo sobre microserviços",
    )

    out = await gen.generate_image_prompt(post)

    assert "Ilustração" in out
    call = anthropic.chat_calls[0]
    assert "arquitetura distribuída" in call["prompt"]
    assert "microserviços" in call["prompt"]
    assert "SEM texto" in call["prompt"]


@pytest.mark.asyncio
async def test_parse_falho_propaga_exception(system_prompt_file) -> None:
    anthropic = FakeAnthropicClient()
    anthropic.set_chat_response("Claude não respeitou o formato, sem separador.")
    gen = ClaudePostGenerator(anthropic, system_prompt_file)
    post = Post(chat_id="x", user_prompt="x", research_summary="x")

    with pytest.raises(BilingualParseError):
        await gen.generate_body(post)


def test_construtor_falha_se_path_invalido() -> None:
    with pytest.raises(FileNotFoundError):
        ClaudePostGenerator(FakeAnthropicClient(), "/path/que/nao/existe.txt")
