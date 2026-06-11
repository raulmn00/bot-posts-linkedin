import pytest

from bot_posts_linkedin.telegram.parser import (
    EmptySubjectError,
    ParsedCommand,
    parse_command,
)


def test_comando_basico_sem_github() -> None:
    r = parse_command("[GERAR-POST] sobre RAG híbrido")
    assert r == ParsedCommand(user_prompt="sobre RAG híbrido", use_github=False)


def test_comando_case_insensitive() -> None:
    assert parse_command("[gerar-post] xpto") == ParsedCommand("xpto", False)
    assert parse_command("[Gerar-Post] xpto") == ParsedCommand("xpto", False)
    assert parse_command("[GERAR-post] xpto") == ParsedCommand("xpto", False)


def test_github_no_inicio() -> None:
    r = parse_command("[GERAR-POST] [GITHUB] meu projeto")
    assert r == ParsedCommand("meu projeto", True)


def test_github_no_meio() -> None:
    r = parse_command("[GERAR-POST] sobre [GITHUB] meu projeto")
    assert r == ParsedCommand("sobre meu projeto", True)


def test_github_no_fim() -> None:
    r = parse_command("[GERAR-POST] meu projeto [GITHUB]")
    assert r == ParsedCommand("meu projeto", True)


def test_github_case_insensitive() -> None:
    assert parse_command("[GERAR-POST] [github] tema").use_github is True
    assert parse_command("[GERAR-POST] [GitHub] tema").use_github is True


def test_whitespace_inicio_aceito() -> None:
    assert parse_command("   [GERAR-POST] tema") == ParsedCommand("tema", False)


def test_nao_comando_retorna_none() -> None:
    assert parse_command("oi, tudo bem?") is None
    assert parse_command("texto [GERAR-POST] no meio") is None
    assert parse_command("") is None


def test_assunto_vazio_levanta() -> None:
    with pytest.raises(EmptySubjectError):
        parse_command("[GERAR-POST]")
    with pytest.raises(EmptySubjectError):
        parse_command("[GERAR-POST]   ")
    with pytest.raises(EmptySubjectError):
        parse_command("[GERAR-POST] [GITHUB]")  # flag presente mas assunto vazio


def test_whitespace_normalizado() -> None:
    # Múltiplos espaços e quebras de linha devem virar 1 espaço.
    r = parse_command("[GERAR-POST]   sobre   o    tema\n\nlegal")
    assert r == ParsedCommand("sobre o tema legal", False)
