"""Testes do GithubApiSearch usando httpx.MockTransport.

Cobre:
  - filtro de forks
  - ranking via LLM (com nomes válidos)
  - anti-hallucination (LLM inventa nome → ignora)
  - fallback determinístico quando LLM falha
  - README presente e grande → usado
  - README < 200 chars → fallback pra description+topics
  - README 404 → fallback
  - consolidação textual
"""

import base64
from typing import Any

import httpx
import pytest

from bot_posts_linkedin.services.github_search import GithubApiSearch
from tests.fakes import FakeAnthropicClient

USERNAME = "raulmn00"


def _repo(
    name: str,
    description: str | None = None,
    topics: list[str] | None = None,
    fork: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "html_url": f"https://github.com/{USERNAME}/{name}",
        "description": description,
        "topics": topics or [],
        "fork": fork,
    }


def _readme_response(content: str) -> dict[str, Any]:
    return {
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "encoding": "base64",
    }


def _make_transport(
    repos: list[dict[str, Any]],
    readmes: dict[str, str | None],
) -> httpx.MockTransport:
    """Mock do GitHub API.

    readmes[name] = conteúdo (None pra simular 404). Repos não listados caem em 404.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/users/{USERNAME}/repos":
            return httpx.Response(200, json=repos)
        for name, content in readmes.items():
            if path == f"/repos/{USERNAME}/{name}/readme":
                if content is None:
                    return httpx.Response(404, json={"message": "Not Found"})
                return httpx.Response(200, json=_readme_response(content))
        return httpx.Response(404, json={"message": "Not Found"})

    return httpx.MockTransport(handler)


def _make_service(
    repos: list[dict[str, Any]],
    readmes: dict[str, str | None],
    *,
    llm_response: str = '{"top": ["repo-a", "repo-b", "repo-c"]}',
    llm_error: Exception | None = None,
) -> tuple[GithubApiSearch, FakeAnthropicClient]:
    anthropic = FakeAnthropicClient()
    anthropic.set_chat_response(llm_response)
    if llm_error is not None:
        anthropic.set_chat_error(llm_error)
    transport = _make_transport(repos, readmes)
    service = GithubApiSearch(
        token="t",
        username=USERNAME,
        anthropic_client=anthropic,
        transport=transport,
    )
    return service, anthropic


@pytest.mark.asyncio
async def test_filtra_forks() -> None:
    repos = [
        _repo("repo-a", description="A"),
        _repo("fork-x", description="fork de outro", fork=True),
        _repo("repo-b", description="B"),
    ]
    readmes = {"repo-a": "x" * 300, "repo-b": "y" * 300}
    service, anthropic = _make_service(
        repos, readmes, llm_response='{"top": ["repo-a", "repo-b"]}'
    )

    result = await service.search("qualquer")

    # Fork não deve aparecer no prompt do LLM nem nos repos retornados.
    nomes = {r.name for r in result.repos}
    assert "raulmn00/fork-x" not in nomes
    assert nomes == {"raulmn00/repo-a", "raulmn00/repo-b"}
    # E o LLM não viu o fork no prompt
    assert "fork-x" not in anthropic.chat_calls[0]["prompt"]


@pytest.mark.asyncio
async def test_anti_hallucination_ignora_nome_inventado() -> None:
    repos = [_repo("real-a"), _repo("real-b"), _repo("real-c")]
    readmes = {"real-a": "x" * 300, "real-b": "y" * 300, "real-c": "z" * 300}
    # LLM inventa "fantasma" — deve ser descartado sem crash.
    service, _ = _make_service(
        repos, readmes, llm_response='{"top": ["real-a", "fantasma", "real-c"]}'
    )

    result = await service.search("tema")

    nomes = {r.name for r in result.repos}
    assert nomes == {"raulmn00/real-a", "raulmn00/real-c"}


@pytest.mark.asyncio
async def test_fallback_quando_readme_curto() -> None:
    repos = [_repo("curto", description="repo com README minúsculo", topics=["llm", "rag"])]
    readmes = {"curto": "ABC"}  # < 200 chars
    service, _ = _make_service(repos, readmes, llm_response='{"top": ["curto"]}')

    result = await service.search("tema")
    repo = result.repos[0]

    # Fallback usa description + topics, não o README de 3 chars.
    assert "repo com README minúsculo" in repo.relevance_excerpt
    assert "llm" in repo.relevance_excerpt
    assert "rag" in repo.relevance_excerpt
    assert "ABC" not in repo.relevance_excerpt


@pytest.mark.asyncio
async def test_fallback_quando_readme_404() -> None:
    repos = [_repo("sem-readme", description="só description", topics=["x"])]
    readmes = {"sem-readme": None}  # 404
    service, _ = _make_service(repos, readmes, llm_response='{"top": ["sem-readme"]}')

    result = await service.search("tema")
    repo = result.repos[0]
    assert "só description" in repo.relevance_excerpt
    assert "x" in repo.relevance_excerpt


@pytest.mark.asyncio
async def test_readme_grande_usado_truncado() -> None:
    readme_grande = "linha relevante\n" * 200  # ~3200 chars
    repos = [_repo("grande", description="d")]
    readmes = {"grande": readme_grande}
    service, _ = _make_service(repos, readmes, llm_response='{"top": ["grande"]}')

    result = await service.search("tema")
    excerpt = result.repos[0].relevance_excerpt
    assert "linha relevante" in excerpt
    assert len(excerpt) <= 1500  # _README_MAX_CHARS


@pytest.mark.asyncio
async def test_llm_erro_fallback_para_repos_mais_recentes() -> None:
    repos = [_repo(f"r{i}", description=f"desc {i}") for i in range(5)]
    readmes = {r["name"]: r["description"] * 100 for r in repos}  # >200 chars
    service, _ = _make_service(repos, readmes, llm_error=RuntimeError("LLM down"))

    result = await service.search("tema")

    # Fallback determinístico: pega os 3 primeiros (já vêm sorted=updated da API).
    assert {r.name for r in result.repos} == {
        "raulmn00/r0",
        "raulmn00/r1",
        "raulmn00/r2",
    }


@pytest.mark.asyncio
async def test_llm_json_invalido_fallback() -> None:
    repos = [_repo(f"r{i}") for i in range(3)]
    readmes = {r["name"]: "x" * 300 for r in repos}
    service, _ = _make_service(
        repos, readmes, llm_response="resposta sem json aqui"
    )

    result = await service.search("tema")
    assert len(result.repos) == 3  # caiu no fallback determinístico


@pytest.mark.asyncio
async def test_consolidacao_inclui_repos_e_topic() -> None:
    repos = [_repo("a", description="da", topics=["t1"])]
    readmes = {"a": "readme da " * 50}
    service, _ = _make_service(repos, readmes, llm_response='{"top": ["a"]}')

    result = await service.search("meu tema")
    assert "meu tema" in result.summary
    assert "raulmn00/a" in result.summary
    assert "t1" in result.summary


@pytest.mark.asyncio
async def test_nenhum_repo_publico() -> None:
    service, _ = _make_service(repos=[], readmes={})
    result = await service.search("tema")
    assert result.repos == []
    assert "nenhum repo" in result.summary.lower()
