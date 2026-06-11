"""Cliente do Claude — pesquisa web e chat simples.

Dois métodos usados em todo o projeto:

  - `research_with_web_search(topic)` na Fase C (research_summary do post).
  - `chat(prompt, system)` aqui pelo GithubSearchService (ranqueia repos relevantes)
    e nas Fases D/E pra geração e revisão do body bilíngue.

Protocol + impl concreta: testes plugam um Fake sem custo de API.
"""

from typing import Any, Protocol, runtime_checkable

from anthropic import AsyncAnthropic

from bot_posts_linkedin.domain.insights import WebResearchResult

_RESEARCH_PROMPT_TEMPLATE = (
    "Pesquise na web sobre o assunto abaixo e devolva um resumo objetivo em "
    "português (3-5 parágrafos), focando em informações relevantes, atuais e que "
    "ajudem a embasar um post profissional. Cite fatos, números e datas quando "
    "disponíveis nas fontes.\n"
    "{author_context}"
    "\nAssunto: {topic}"
)


@runtime_checkable
class AnthropicClient(Protocol):
    async def research_with_web_search(
        self, topic: str, *, author_context: str | None = None
    ) -> WebResearchResult: ...

    async def chat(self, prompt: str, system: str | None = None) -> str: ...

    async def close(self) -> None: ...


class HttpxAnthropicClient:
    """Implementação concreta usando o SDK oficial `anthropic` (que usa httpx por baixo)."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        web_search_max_uses: int = 3,
        max_tokens: int = 2048,
    ) -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._web_search_max_uses = web_search_max_uses
        self._max_tokens = max_tokens

    async def research_with_web_search(
        self, topic: str, *, author_context: str | None = None
    ) -> WebResearchResult:
        # author_context evita o Claude tentar "achar na web" um repo pessoal do
        # autor — ele já sabe que o GithubApiSearch vai cuidar disso em outra etapa.
        ctx_block = f"\n{author_context.strip()}\n" if author_context else ""
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            tools=[
                {
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": self._web_search_max_uses,
                }
            ],
            messages=[
                {
                    "role": "user",
                    "content": _RESEARCH_PROMPT_TEMPLATE.format(
                        topic=topic, author_context=ctx_block
                    ),
                }
            ],
        )
        return _parse_research_response(response)

    async def chat(self, prompt: str, system: str | None = None) -> str:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system is not None:
            kwargs["system"] = system
        response = await self._client.messages.create(**kwargs)
        return _extract_text(response)

    async def close(self) -> None:
        await self._client.close()


def _extract_text(response: Any) -> str:
    """Concatena todos os TextBlocks. Ignora tool_use / tool_result."""
    chunks: list[str] = []
    for block in response.content:
        data = block.model_dump() if hasattr(block, "model_dump") else block
        if isinstance(data, dict) and data.get("type") == "text":
            text = data.get("text") or ""
            if text:
                chunks.append(text)
    return "\n\n".join(chunks).strip()


def _parse_research_response(response: Any) -> WebResearchResult:
    """Extrai summary (textos do Claude) + sources (URLs vistas/citadas).

    Usa model_dump pra não acoplar a nomes específicos de classes do SDK —
    se a SDK trocar TextBlock por TextContentBlock, este parser continua.
    """
    text_chunks: list[str] = []
    sources: list[str] = []
    seen: set[str] = set()

    def _add(url: str | None) -> None:
        if not url or url in seen:
            return
        seen.add(url)
        sources.append(url)

    for block in response.content:
        data = block.model_dump() if hasattr(block, "model_dump") else block
        if not isinstance(data, dict):
            continue
        btype = data.get("type")
        if btype == "text":
            text = data.get("text") or ""
            if text:
                text_chunks.append(text)
            for cit in data.get("citations") or []:
                if isinstance(cit, dict):
                    _add(cit.get("url"))
        elif btype == "web_search_tool_result":
            for item in data.get("content") or []:
                if isinstance(item, dict):
                    _add(item.get("url"))

    summary = "\n\n".join(text_chunks).strip()
    if not summary:
        summary = "(pesquisa web não retornou resumo)"
    return WebResearchResult(summary=summary, sources=sources)
