"""Router HTTP do webhook do Telegram.

Faz só roteamento e validação de segurança. A lógica de negócio fica no
PostFlowService, que é injetado via FastAPI dependency e pode ser substituído
nos testes com `app.dependency_overrides`.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status

from bot_posts_linkedin.config import Settings, get_settings
from bot_posts_linkedin.services.post_flow import PostFlowService
from bot_posts_linkedin.telegram.parser import EmptySubjectError, parse_command

router = APIRouter(prefix="/telegram")


def get_post_flow(request: Request) -> PostFlowService:
    """Injetado pela app via app.state — testes podem fazer override."""
    service = getattr(request.app.state, "post_flow", None)
    if service is None:
        raise RuntimeError("PostFlowService não inicializado — chame create_app primeiro")
    return service


def _verify_webhook_secret(
    settings: Annotated[Settings, Depends(get_settings)],
    x_telegram_bot_api_secret_token: Annotated[str | None, Header()] = None,
) -> None:
    if x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        # 401 quando o secret está errado/ausente — só o Telegram com o secret correto passa.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid secret")


def _extract_chat_id(update: dict[str, Any]) -> str | None:
    """Telegram coloca chat.id em locais diferentes pra message vs callback_query."""
    if "message" in update:
        return str(update["message"].get("chat", {}).get("id"))
    if "callback_query" in update:
        msg = update["callback_query"].get("message") or {}
        return str(msg.get("chat", {}).get("id"))
    return None


@router.post("/webhook", dependencies=[Depends(_verify_webhook_secret)])
async def telegram_webhook(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    flow: Annotated[PostFlowService, Depends(get_post_flow)],
) -> Response:
    update: dict[str, Any] = await request.json()
    # TODO Fase G: dedup por update_id. Telegram pode reenviar o mesmo update se
    # nosso webhook demorar pra responder 200 — sem dedup, podemos rodar o flow 2×.
    # Estratégia: guardar update_id num set/cache (Firestore TTL 10min) e ignorar se já visto.
    chat_id = _extract_chat_id(update)

    # Chat de terceiro: 200 silent. Não revelar que o bot existe pra ID não autorizado.
    if chat_id != settings.telegram_chat_id:
        return Response(status_code=status.HTTP_200_OK)

    if "callback_query" in update:
        await _dispatch_callback(flow, chat_id, update["callback_query"])
    elif "message" in update and "text" in update["message"]:
        await _dispatch_message(flow, chat_id, update["message"]["text"])

    return Response(status_code=status.HTTP_200_OK)


async def _dispatch_message(flow: PostFlowService, chat_id: str, text: str) -> None:
    try:
        parsed = parse_command(text)
    except EmptySubjectError:
        await flow.send_help(chat_id)
        return

    if parsed is None:
        # Não é comando: pode ser motivo de revisão (Q3=c) ou ruído.
        await flow.handle_free_text(chat_id, text)
        return

    await flow.handle_command(chat_id, parsed.user_prompt, parsed.use_github)


async def _dispatch_callback(
    flow: PostFlowService, chat_id: str, callback: dict[str, Any]
) -> None:
    cb_id = callback["id"]
    data: str = callback.get("data", "")
    message = callback.get("message") or {}
    message_id = message.get("message_id")

    # callback_data sempre é "<acao>:<param>" — ver telegram/keyboards.py.
    action, _, param = data.partition(":")

    if action == "approve":
        await flow.handle_approval(chat_id, param, cb_id)
    elif action == "reject":
        await flow.handle_rejection(chat_id, param, cb_id)
    elif action == "cancel":
        await flow.handle_cancel(chat_id, param, cb_id)
    elif action == "discard_yes":
        await flow.handle_discard_decision(
            chat_id=chat_id, accept=True, callback_query_id=cb_id, original_message_id=message_id
        )
    elif action == "discard_no":
        await flow.handle_discard_decision(
            chat_id=chat_id, accept=False, callback_query_id=cb_id, original_message_id=message_id
        )
