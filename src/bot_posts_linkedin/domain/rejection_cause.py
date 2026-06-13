"""Categoria do motivo de rejeição/encerramento do post.

Separa o motivo ESTRUTURAL (por que o post terminou em REJECTED) do feedback
HUMANO do user (revision_feedback). Cada categoria pode carregar um detail
opcional em texto livre (ex: prompt do novo comando que descartou; mensagem
de erro da IA que falhou).

Adicionar categoria nova: incluir aqui + handler correspondente em post_flow.
"""

from enum import StrEnum


class RejectionCause(StrEnum):
    # User clicou em "Cancelar tudo" no flow do limite (Fase E).
    USER_CANCELLED_AT_LIMIT = "user_cancelled_at_limit"
    # User disparou novo [GERAR-POST] e confirmou descarte do pendente.
    # rejection_detail = prompt do novo comando, pra rastreabilidade.
    DISCARDED_BY_NEW_COMMAND = "discarded_by_new_command"
    # Pipeline de geração falhou irrecuperavelmente (LLM, GitHub, parse, etc.).
    # rejection_detail = "TypeError: ..." resumido.
    GENERATION_FAILURE = "generation_failure"
    # API do LinkedIn retornou 4xx/5xx ao publicar (Fase F).
    # rejection_detail = "status N: body resumido".
    PUBLICATION_FAILURE = "publication_failure"
    # LinkedIn retornou 401 — access_token expirou (vida útil 60 dias).
    # rejection_detail = None (mensagem específica é montada em messages.py).
    TOKEN_EXPIRED = "token_expired"
