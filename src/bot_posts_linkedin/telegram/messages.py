"""Templates de texto enviados pelo bot.

Centralizar aqui mantém o tom consistente, simplifica i18n futuro
e isola o que muda entre Fase B (mocks) e Fases D/F (conteúdo real).
"""

from bot_posts_linkedin.domain.insights import GithubFindings, WebResearchResult

HELP_MESSAGE = (
    "🤖 Como usar:\n\n"
    "Comando obrigatório no início:\n"
    "[GERAR-POST] <assunto>\n\n"
    "Flag opcional (em qualquer posição):\n"
    "[GITHUB] — busca nos seus repos públicos como insumo\n\n"
    "Exemplos:\n"
    "• [GERAR-POST] minha experiência com RAG híbrido\n"
    "• [GERAR-POST] [GITHUB] como estruturei meu agent-router\n"
    "• [GERAR-POST] reflexões sobre LLMs [GITHUB]"
)

ASKING_REASON_MESSAGE = (
    "❌ Reprovado. Me diga o motivo na próxima mensagem (texto livre)."
)


def format_bilingual_post(body_pt: str, body_en: str) -> str:
    """Formato final do post: bloco PT no topo, separador, bloco EN.

    Esse separador foi confirmado com o user no PASSO 1. Não mexer sem combinar.
    """
    return f"🇧🇷\n{body_pt}\n\n━━━━━━━━━━━━━━━\n\n🇺🇸\n{body_en}"


def format_discard_question(pending_prompt_preview: str, pending_status: str) -> str:
    return (
        f"⚠️ Você tem um post pendente:\n\n"
        f"   \"{pending_prompt_preview}\" (em {pending_status})\n\n"
        f"Descartar esse e gerar o novo?"
    )


APPROVED_FOOTER = "\n\n✅ Aprovado — publicação no LinkedIn será feita na Fase F."

# Footer mostrado IMEDIATAMENTE ao clicar Aprovar, enquanto a publicação roda em background.
PUBLISHING_FOOTER = "\n\n⏳ Aprovado — publicando no LinkedIn..."

# Footer aplicado quando o post foi publicado com sucesso E temos o URN.
def format_published_footer(post_urn: str) -> str:
    # URN vem como `urn:li:share:1234` ou `urn:li:ugcPost:1234` — em ambos os formatos
    # o feed update funciona via /feed/update/{urn} (LinkedIn aceita ambos).
    url = f"https://www.linkedin.com/feed/update/{post_urn}/"
    return f"\n\n✅ Publicado: {url}"


# Footer quando o LinkedIn retornou 2xx mas SEM x-restli-id (raro mas possível).
PUBLISHED_WITHOUT_URN_FOOTER = (
    "\n\n✅ Publicado no LinkedIn, mas a API não devolveu o link do post. "
    "Confira diretamente no seu perfil em https://www.linkedin.com/in/"
)


def format_publication_failure_message(error_summary: str) -> str:
    return (
        "❌ Falha ao publicar no LinkedIn:\n\n"
        f"{error_summary}\n\n"
        "O post foi marcado como REJECTED. Tente gerar de novo com [GERAR-POST] ..."
    )


def format_token_expired_message() -> str:
    """Mensagem específica pra 401 — token expirou."""
    return (
        "❌ Token do LinkedIn expirou (vida útil 60 dias).\n\n"
        "Renove em https://www.linkedin.com/developers/apps → seu app → "
        "Auth → OAuth 2.0 tools → Token generator.\n\n"
        "Atualize LINKEDIN_ACCESS_TOKEN no .env (dev) ou no Secret Manager (prod) "
        "e reinicie o bot."
    )


def format_dry_run_chunks(payload_json: str) -> list[str]:
    """Quebra o payload do dry-run em mensagens que cabem no Telegram (4096 chars).

    Retorna lista de strings prontas pra enviar em sequência. Cada bloco vem em
    <pre> pra preservar formatação JSON no Telegram (parse_mode=HTML).
    """
    header = (
        "🧪 DRY RUN — NÃO publicado de verdade.\n"
        "Payload que SERIA enviado pro POST /rest/posts:\n"
    )
    # Reserva ~200 chars pra header + tags <pre>; chunks de payload de ~3800 cada.
    max_payload_chars = 3800
    chunks: list[str] = []
    remaining = payload_json
    first = True
    while remaining:
        slice_ = remaining[:max_payload_chars]
        remaining = remaining[max_payload_chars:]
        prefix = header if first else "🧪 DRY RUN — continuação:\n"
        first = False
        chunks.append(f"{prefix}<pre>{_html_escape(slice_)}</pre>")
    return chunks


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


SIMULATED_FOOTER = (
    "\n\n🧪 SIMULADO (LINKEDIN_DRY_RUN=true) — payload mostrado acima. "
    "Mude LINKEDIN_DRY_RUN=false no .env pra publicar de verdade."
)

DISCARDED_KEEP_PENDING = (
    "❌ Comando novo cancelado. Continue mandando o motivo do post pendente."
)

# Texto enviado quando o user manda mensagem livre enquanto há discard pendente —
# ponto que ficou em aberto da Fase B. Força decisão explícita nos botões.
DISCARD_PENDING_TEXT_MESSAGE = (
    "⚠️ Você tem uma pergunta pendente acima (botões \"✅ Sim, descartar\" / "
    "\"❌ Não, manter\"). Clique em um deles, ou mande [GERAR-POST] novo se "
    "mudou de ideia."
)


def format_revision_header(revision_count: int, max_iterations: int, reason: str) -> str:
    """Header da mensagem de revisão — mostra consumo do limite (Fase E)."""
    return f'📝 Revisão #{revision_count} de {max_iterations} (motivo: "{reason}")\n\n'


# Footer aplicado quando o user atinge MAX_REVISION_ITERATIONS e clica em Reprovar.
LIMIT_REACHED_FOOTER = (
    "\n\n⚠️ Você atingiu o limite de revisões. Aprovar essa versão ou cancelar?"
)

# Footer aplicado quando o user clica em Cancelar no keyboard do limite.
CANCELLED_AT_LIMIT_FOOTER = "\n\n🚫 Cancelado após atingir o limite de revisões."


def format_failure_message(error_summary: str) -> str:
    """Mensagem quando a geração falha — salvaguarda (a) da Fase C.

    Mantemos a classe + mensagem do erro pra você diagnosticar sem ter que
    olhar logs. Em prod (Cloud Run) os logs também vão pro Cloud Logging.
    """
    return (
        f"❌ Falha ao gerar:\n\n"
        f"{error_summary}\n\n"
        f"Tente novamente com [GERAR-POST] ..."
    )


def format_bilingual_parse_failure_message(error_summary: str) -> str:
    """Específico pra quando o Claude não respeitou o formato bilíngue.

    Diferente de format_failure_message porque a causa é o LLM, não infra —
    a sugestão útil é reformular o assunto, não tentar de novo cego.
    """
    return (
        "❌ O Claude não respeitou o formato bilíngue (PT/EN) na resposta.\n\n"
        f"Detalhe: {error_summary}\n\n"
        "Tente reformular o assunto com [GERAR-POST] ..., "
        "talvez algo mais específico ajude."
    )


def format_image_failure_notice(error_summary: str) -> str:
    """Aviso de imagem indisponível — post vai sem foto (decisão do user).

    Aparece como mensagem ANTES do texto bilíngue + botões, pra você saber
    o que aconteceu e poder reprovar se quiser tentar de novo.
    """
    return (
        "⚠️ Não foi possível gerar a imagem desta vez — o post vai sem foto.\n\n"
        f"Motivo: {error_summary}"
    )


# Limite oficial do Telegram pra caption de photo.
TELEGRAM_CAPTION_MAX_CHARS = 1024


def format_caption_short(body_pt: str) -> str:
    """Trunca o body PT pra caber no caption do sendPhoto (limite 1024 chars)."""
    if len(body_pt) <= TELEGRAM_CAPTION_MAX_CHARS:
        return body_pt
    # Reserva 4 chars pro "..." e quebra em uma fronteira de palavra quando possível.
    cut = body_pt[: TELEGRAM_CAPTION_MAX_CHARS - 4]
    last_space = cut.rfind(" ")
    if last_space > TELEGRAM_CAPTION_MAX_CHARS - 100:  # corte com folga
        cut = cut[:last_space]
    return cut + "..."


def format_insights_message(
    topic: str,
    web: WebResearchResult,
    gh: GithubFindings | None,
) -> str:
    """Mostra os insumos coletados antes da geração — pra você inspecionar.

    Trunca cada bloco pra caber bem no Telegram (limite por mensagem é 4096 chars,
    mas legibilidade pede menos). O conteúdo completo fica persistido no Post.
    """
    web_block = web.summary
    if len(web_block) > 600:
        web_block = web_block[:600] + "..."

    parts: list[str] = [
        f"🔎 Insumos coletados sobre: {topic}",
        "",
        "🌐 Pesquisa web:",
        web_block,
    ]
    if web.sources:
        # Lista compacta das 5 primeiras fontes — referência rápida.
        sources_preview = "\n".join(f"   • {url}" for url in web.sources[:5])
        parts.extend(["", f"   Fontes ({len(web.sources)} no total):", sources_preview])

    if gh is not None and gh.repos:
        parts.extend(["", "📚 GitHub (raulmn00):"])
        for repo in gh.repos:
            short_desc = repo.description or "(sem description)"
            if len(short_desc) > 120:
                short_desc = short_desc[:120] + "..."
            parts.append(f"• {repo.name} — {short_desc}")

    parts.extend(["", "Gerando post..."])
    return "\n".join(parts)
