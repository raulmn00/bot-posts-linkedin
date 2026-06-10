#!/usr/bin/env python3
"""Registra (ou deleta) o webhook do Telegram.

Uso:
    uv run python scripts/register_telegram_webhook.py https://abc.ngrok-free.app
    uv run python scripts/register_telegram_webhook.py --delete

ngrok grátis muda a URL a cada restart — rodar este script de novo é o caminho.
"""

import argparse
import asyncio
import sys

from bot_posts_linkedin.config import get_settings
from bot_posts_linkedin.telegram.client import HttpxTelegramClient


async def _run(url: str | None, delete: bool) -> int:
    settings = get_settings()
    client = HttpxTelegramClient(settings.telegram_bot_token)
    try:
        if delete:
            r = await client.delete_webhook()
            print(f"✅ Webhook deletado: {r}")
            return 0

        if not url:
            print("❌ URL obrigatória (ou use --delete).", file=sys.stderr)
            return 1

        full_url = url.rstrip("/") + "/telegram/webhook"
        r = await client.set_webhook(full_url, settings.telegram_webhook_secret)
        print(f"✅ Webhook registrado em {full_url}")
        print(f"   resposta: {r}")
        return 0
    finally:
        await client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Registra ou deleta o webhook do Telegram.")
    parser.add_argument("url", nargs="?", help="URL pública HTTPS (ex: https://abc.ngrok-free.app)")
    parser.add_argument("--delete", action="store_true", help="Deleta o webhook atual.")
    args = parser.parse_args()
    return asyncio.run(_run(args.url, args.delete))


if __name__ == "__main__":
    sys.exit(main())
