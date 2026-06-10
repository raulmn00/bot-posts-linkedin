import re
from dataclasses import dataclass

# [GERAR-POST] precisa ser a primeira tag (após whitespace opcional).
# Case-insensitive aceita [gerar-post], [Gerar-Post], etc.
_COMMAND_RE = re.compile(r"^\s*\[gerar-post\]", re.IGNORECASE)

# [GITHUB] pode aparecer em qualquer posição, qualquer caixa, qualquer número de vezes.
_GITHUB_FLAG_RE = re.compile(r"\[github\]", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedCommand:
    user_prompt: str
    use_github: bool


class EmptySubjectError(ValueError):
    """Comando [GERAR-POST] sem assunto — caller decide se manda mini-help ou ignora."""


def parse_command(text: str) -> ParsedCommand | None:
    """Parseia uma mensagem do Telegram em um ParsedCommand.

    Retorna None quando não é um comando (não começa com [GERAR-POST]).
    Levanta EmptySubjectError quando o comando é válido mas o assunto fica vazio
    após remover as tags — sinal pro caller responder com mini-help.
    """
    if not _COMMAND_RE.match(text):
        return None

    # Remove o [GERAR-POST] do início (só a primeira ocorrência).
    remaining = _COMMAND_RE.sub("", text, count=1)

    # [GITHUB] em qualquer posição.
    use_github = bool(_GITHUB_FLAG_RE.search(remaining))
    if use_github:
        remaining = _GITHUB_FLAG_RE.sub("", remaining)

    # Colapsa whitespace múltiplo, tira pontas.
    subject = " ".join(remaining.split())
    if not subject:
        raise EmptySubjectError("Comando [GERAR-POST] sem assunto.")

    return ParsedCommand(user_prompt=subject, use_github=use_github)
