"""Models dos insumos coletados antes da geração do post.

Frozen dataclasses pra deixar explícito que esses são snapshots imutáveis devolvidos
pelos serviços de pesquisa — o PostFlowService apenas copia o `summary` consolidado
para os campos do Post e segue.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WebResearchResult:
    """Resultado da pesquisa web nativa do Claude (tool web_search_20250305)."""

    summary: str  # texto consolidado para alimentar research_summary no Post
    sources: list[str] = field(default_factory=list)  # URLs citadas pelo Claude


@dataclass(frozen=True)
class GithubFinding:
    """Um repo do GitHub que o LLM considerou relevante para o assunto."""

    name: str  # ex: "raulmn00/agent-router"
    url: str
    description: str | None
    topics: list[str]
    relevance_excerpt: str  # README ou fallback (description + topics) quando README curto


@dataclass(frozen=True)
class GithubFindings:
    """Resultado consolidado da busca em repos públicos de raulmn00."""

    summary: str  # texto que vai pro campo github_findings do Post
    repos: list[GithubFinding]
