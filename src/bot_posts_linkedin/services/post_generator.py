"""Geração do body bilíngue do post + prompt da imagem.

Estrutura: PostGeneratorService como Protocol; ClaudePostGenerator concretiza
usando AnthropicClient + system prompt lido de arquivo no boot (configurável).

Parse PT/EN com regex tolerante (aceita variações como `===EN===`, `==== EN ====`,
`===en===`). Quando o split falhar, BilingualParseError sobe — o post_flow
trata isso com uma mensagem dedicada no Telegram (não EN vazio silencioso).
"""

import re
from pathlib import Path
from typing import Protocol, runtime_checkable

from bot_posts_linkedin.domain.post import Post
from bot_posts_linkedin.services.anthropic_client import AnthropicClient

# Aceita ===EN===, ==== EN ====, == en ==, etc. — tolerante mas inequívoco.
_SEPARATOR_RE = re.compile(r"={3,}\s*EN\s*={3,}", re.IGNORECASE)


class BilingualParseError(ValueError):
    """Saída do Claude não respeitou o separador bilíngue ou um dos blocos veio vazio."""


@runtime_checkable
class PostGeneratorService(Protocol):
    async def generate_body(self, post: Post) -> tuple[str, str]:
        """Retorna (body_pt, body_en). Levanta BilingualParseError em formato inválido."""
        ...

    async def generate_image_prompt(self, post: Post) -> str:
        """Retorna o prompt em PT a ser passado pro gerador de imagem."""
        ...


class ClaudePostGenerator:
    """Implementação concreta usando o AnthropicClient + system prompt do arquivo."""

    def __init__(
        self,
        anthropic_client: AnthropicClient,
        system_prompt_path: str,
    ) -> None:
        self._anthropic = anthropic_client
        # Falha cedo se o arquivo não existir — boot é melhor lugar pra esse erro.
        self._system_prompt = Path(system_prompt_path).read_text(encoding="utf-8").strip()

    async def generate_body(self, post: Post) -> tuple[str, str]:
        user_prompt = _build_body_prompt(post)
        raw = await self._anthropic.chat(prompt=user_prompt, system=self._system_prompt)
        return parse_bilingual(raw)

    async def generate_image_prompt(self, post: Post) -> str:
        ctx = (post.research_summary or "")[:500]
        prompt = (
            "Gere um prompt de imagem em português para uma ilustração "
            "profissional de post de LinkedIn sobre:\n\n"
            f"Assunto: {post.user_prompt}\n\n"
            f"Contexto: {ctx}\n\n"
            "Regras:\n"
            "- 1-2 frases em português\n"
            "- Foco em estilo visual, paleta, atmosfera\n"
            "- Estilo profissional e moderno\n"
            "- Evitar clichês visuais de tech (engrenagens, redes neurais "
            "visíveis, robôs humanoides, fios de luz)\n"
            "- SEM texto na imagem\n\n"
            "Responda APENAS o prompt, sem explicações."
        )
        raw = await self._anthropic.chat(prompt)
        return raw.strip()


def parse_bilingual(raw: str) -> tuple[str, str]:
    """Split tolerante PT/EN.

    Levanta BilingualParseError se separador estiver ausente ou se um dos
    blocos resultar vazio (ex: ===EN=== no início, ou ===EN=== no fim).
    """
    match = _SEPARATOR_RE.search(raw)
    if match is None:
        raise BilingualParseError(
            "separador '===EN===' não encontrado na resposta do Claude"
        )
    pt = raw[: match.start()].strip()
    en = raw[match.end() :].strip()
    if not pt or not en:
        raise BilingualParseError(
            f"bloco vazio após split — PT={len(pt)} chars, EN={len(en)} chars"
        )
    return pt, en


def _build_body_prompt(post: Post) -> str:
    """User prompt da geração — usa insumos já coletados na Fase C."""
    lines: list[str] = [f"Assunto: {post.user_prompt}", ""]

    lines.append("Insumos da pesquisa web:")
    lines.append(post.research_summary or "(sem insumos de web)")
    lines.append("")

    if post.github_findings:
        lines.append("Insumos do GitHub (repos próprios relevantes):")
        lines.append(post.github_findings)
        lines.append("")

    if post.revision_feedback:
        lines.append("Feedback de revisões anteriores (responder e ajustar):")
        for i, fb in enumerate(post.revision_feedback, 1):
            lines.append(f"  {i}. {fb}")
        lines.append("")

    lines.append(
        "ALVO DE TAMANHO: cada versão (PT e EN) deve ter entre 600 e 1200 "
        "caracteres. Não conte caracteres explicitamente — apenas mire no alcance."
    )
    lines.append("")
    lines.append("FORMATO DE SAÍDA (siga exatamente):")
    lines.append("- Bloco PT primeiro")
    lines.append("- Linha separadora literal: ===EN===")
    lines.append("- Bloco EN depois")
    lines.append("- Nada mais (sem markdown de título, sem comentários antes/depois)")

    return "\n".join(lines)
