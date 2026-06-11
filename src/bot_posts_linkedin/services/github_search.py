"""Busca em repos públicos de raulmn00 quando a flag [GITHUB] está presente.

Estratégia:
  1) GET /users/{username}/repos — lista todos (max 100, mais recentes primeiro).
  2) Filtra forks (não representam trabalho próprio do usuário).
  3) LLM (Claude) ranqueia os top-3 mais relevantes ao assunto via JSON estruturado.
  4) Pra cada um dos 3: tenta README; se < 200 chars ou 404, fallback pra
     description + topics.
  5) Consolida em texto plano alimentando `github_findings` do Post.

Anti-hallucination: nomes devolvidos pelo Claude são validados contra a lista real
antes de fetch. Se o LLM inventar um nome, o repo é descartado silenciosamente.
"""

import base64
import json
import re
from typing import Any, Protocol, runtime_checkable

import httpx

from bot_posts_linkedin.domain.insights import GithubFinding, GithubFindings
from bot_posts_linkedin.services.anthropic_client import AnthropicClient

# TODO Fase D: aplicar teto de tamanho no `summary` consolidado.
# Em assuntos amplos os 3 trechos podem somar 5kB+ e inflar o prompt do Claude
# na geração do body. Sugestão: truncar com indicador "[...]" ao passar de 3000 chars.
_GITHUB_FINDINGS_MAX_CHARS = 3000

_README_FALLBACK_THRESHOLD = 200
_README_MAX_CHARS = 1500
_TOP_N = 3


@runtime_checkable
class GithubSearchService(Protocol):
    async def search(self, topic: str) -> GithubFindings: ...


class GithubApiSearch:
    _BASE_URL = "https://api.github.com"

    def __init__(
        self,
        token: str,
        username: str,
        anthropic_client: AnthropicClient,
        *,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._username = username
        self._anthropic = anthropic_client
        self._timeout = timeout_seconds
        # transport=None usa o default (rede real). Testes passam httpx.MockTransport.
        self._transport = transport

    async def search(self, topic: str) -> GithubFindings:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        async with httpx.AsyncClient(
            base_url=self._BASE_URL,
            timeout=self._timeout,
            headers=headers,
            transport=self._transport,
        ) as http:
            repos = await self._list_repos(http)
            repos = [r for r in repos if not r.get("fork")]
            if not repos:
                return GithubFindings(
                    summary="(nenhum repo público não-fork encontrado)",
                    repos=[],
                )

            top_names = await self._rank_top_relevant(topic, repos)
            findings: list[GithubFinding] = []
            for name in top_names[:_TOP_N]:
                repo = next((r for r in repos if r.get("name") == name), None)
                if repo is None:
                    continue  # anti-hallucination
                excerpt = await self._fetch_relevance(http, repo)
                findings.append(
                    GithubFinding(
                        name=f"{self._username}/{repo['name']}",
                        url=repo.get("html_url", ""),
                        description=repo.get("description"),
                        topics=list(repo.get("topics") or []),
                        relevance_excerpt=excerpt,
                    )
                )

            summary = self._consolidate(topic, findings)
            return GithubFindings(summary=summary, repos=findings)

    async def _list_repos(self, http: httpx.AsyncClient) -> list[dict[str, Any]]:
        r = await http.get(
            f"/users/{self._username}/repos",
            params={"type": "owner", "per_page": 100, "sort": "updated"},
        )
        r.raise_for_status()
        return r.json()

    async def _rank_top_relevant(
        self, topic: str, repos: list[dict[str, Any]]
    ) -> list[str]:
        compact = [
            {
                "name": r["name"],
                "description": r.get("description") or "",
                "topics": r.get("topics") or [],
            }
            for r in repos
        ]
        valid_names = {r["name"] for r in repos}

        prompt = (
            f"Dada a lista de repos públicos abaixo e o assunto, retorne SOMENTE "
            f"os {_TOP_N} nomes mais relevantes ao assunto, do mais relevante ao "
            f"menos. Use exatamente os nomes da lista. Devolva apenas JSON nesta "
            f'forma: {{"top": ["nome1", "nome2", "nome3"]}}.\n\n'
            f"Assunto: {topic}\n\n"
            f"Repos:\n{json.dumps(compact, ensure_ascii=False, indent=2)}"
        )

        try:
            raw = await self._anthropic.chat(prompt)
        except Exception:
            # Falha no LLM: fallback determinístico pelos mais recentes.
            return [r["name"] for r in repos[:_TOP_N]]

        # O Claude às vezes embrulha em ```json ... ```; pesco o objeto bruto.
        match = re.search(r'\{[^{}]*"top"[^{}]*\}', raw, re.DOTALL)
        if not match:
            return [r["name"] for r in repos[:_TOP_N]]
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return [r["name"] for r in repos[:_TOP_N]]

        names = parsed.get("top") or []
        return [n for n in names if isinstance(n, str) and n in valid_names]

    async def _fetch_relevance(
        self, http: httpx.AsyncClient, repo: dict[str, Any]
    ) -> str:
        """README (truncado) se houver e tiver corpo; senão fallback description+topics."""
        name = repo["name"]
        try:
            r = await http.get(f"/repos/{self._username}/{name}/readme")
            if r.status_code == 200:
                content_b64 = r.json().get("content", "")
                try:
                    decoded = base64.b64decode(content_b64).decode("utf-8", errors="ignore")
                except (ValueError, UnicodeDecodeError):
                    decoded = ""
                if len(decoded.strip()) >= _README_FALLBACK_THRESHOLD:
                    return decoded[:_README_MAX_CHARS]
        except httpx.HTTPError:
            pass

        # Fallback combinado description+topics (diretiva sua no PASSO Fase C).
        desc = repo.get("description") or "(sem description)"
        topics = repo.get("topics") or []
        topics_line = ", ".join(topics) if topics else "(sem topics)"
        return f"{desc}\n\nTopics: {topics_line}"

    def _consolidate(self, topic: str, findings: list[GithubFinding]) -> str:
        if not findings:
            return "(busca no GitHub não encontrou repos relevantes)"

        lines: list[str] = [
            f"Achados em repos públicos de {self._username} relacionados a '{topic}':",
            "",
        ]
        for f in findings:
            lines.append(f"• {f.name} — {f.description or '(sem description)'}")
            if f.topics:
                lines.append(f"  topics: {', '.join(f.topics)}")
            excerpt = f.relevance_excerpt.strip().replace("\n", " ")
            if len(excerpt) > 400:
                excerpt = excerpt[:400] + "..."
            lines.append(f"  trecho: {excerpt}")
            lines.append("")

        result = "\n".join(lines).strip()
        # Hoje sem teto duro — só o TODO acima. Truncamento futuro entra aqui.
        if len(result) > _GITHUB_FINDINGS_MAX_CHARS:
            result = result[:_GITHUB_FINDINGS_MAX_CHARS] + "\n\n[...]"
        return result
