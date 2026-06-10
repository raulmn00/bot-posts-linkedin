"""Inline keyboards do Telegram.

Cada callback_data segue o formato `<acao>:<param>` para que o handler do
webhook saiba rotear sem precisar consultar o storage primeiro. O limite
do Telegram é 64 bytes — `approve:<uuid hex 32>` = 40 bytes, folga ampla.
"""


def approval_keyboard(post_id: str) -> dict:
    """Botões de Aprovar/Reprovar exibidos com o post mockado/gerado."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Aprovar", "callback_data": f"approve:{post_id}"},
                {"text": "❌ Reprovar", "callback_data": f"reject:{post_id}"},
            ]
        ]
    }


def discard_keyboard(chat_id: str) -> dict:
    """Botões para confirmar/cancelar descarte de post pendente.

    chat_id no callback_data permite roteamento direto — o handler busca o
    chat_state correspondente sem precisar de parsing adicional.
    """
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Sim, descartar", "callback_data": f"discard_yes:{chat_id}"},
                {"text": "❌ Não, manter", "callback_data": f"discard_no:{chat_id}"},
            ]
        ]
    }


def limit_reached_keyboard(post_id: str) -> dict:
    """Keyboard exibido quando revision_count atinge max — força decisão final.

    Reusa `approve:{id}` (idempotente; o handle_approval existente trata).
    Novo `cancel:{id}` dispara handle_cancel → REJECTED.
    """
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Aprovar essa última", "callback_data": f"approve:{post_id}"},
                {"text": "🚫 Cancelar tudo", "callback_data": f"cancel:{post_id}"},
            ]
        ]
    }
