"""Проверка исходящей отправки через активный провайдер.

Запуск из корня проекта:
    python scripts/send_test.py --to +77770000000 --text "Тест бота"

Провайдер берётся из .env (MESSAGING_PROVIDER): bird — Bird API, meta —
WhatsApp Cloud API (Graph API), telegram — Bot API (тогда --to это chat_id).
Успех: Bird — 202 Accepted, Meta/Telegram — 200 (сообщение принято).
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings
from whatsapp.bird_client import BirdError, BirdWhatsAppClient
from whatsapp.errors import MessagingError
from whatsapp.meta_client import MetaError, MetaWhatsAppClient
from whatsapp.telegram_client import TelegramClient


def make_sender(settings):
    """Клиент активного провайдера — та же логика, что в main.build_sender."""
    if settings.messaging_provider == "telegram":
        return TelegramClient(settings.telegram_bot_token)
    if settings.messaging_provider == "meta":
        return MetaWhatsAppClient(
            settings.whatsapp_access_token,
            settings.whatsapp_phone_number_id,
            graph_version=settings.meta_graph_version,
        )
    return BirdWhatsAppClient(
        settings.bird_api_key, settings.bird_api_url, settings.whatsapp_sender_number
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Тестовая отправка сообщения")
    parser.add_argument("--to", required=True,
                        help="получатель: номер в E.164 (+77770000000) или chat_id для Telegram")
    parser.add_argument("--text", default="Проверка связи от бота 🤖", help="текст сообщения")
    args = parser.parse_args()

    settings = get_settings()
    print(f"Бизнес: {settings.business_name}")
    print(f"Провайдер: {settings.messaging_provider}")
    if settings.messaging_provider == "meta":
        print(f"Graph API: v{settings.meta_graph_version} | phone_number_id: {settings.whatsapp_phone_number_id}")
    elif settings.messaging_provider == "telegram":
        print("Telegram Bot API (получатель --to — это chat_id)")
    else:
        print(f"API: {settings.bird_api_url} | from: {settings.whatsapp_sender_number}")
    print(f"Получатель: {args.to}")
    print(f"Текст: {args.text!r}\n")

    sender = make_sender(settings)
    try:
        await sender.send_text(args.to, args.text)
        print("OK: провайдер принял сообщение. Проверьте получателя.")
    except MessagingError as exc:
        print(f"ОШИБКА: {exc}")
        print("\nПодсказки:")
        print("- 401: неверный токен (BIRD_API_KEY / WHATSAPP_ACCESS_TOKEN / TELEGRAM_BOT_TOKEN);")
        print("- Meta 190: токен истёк или не хватает прав System User;")
        print("- Meta (131030) получатель не в списке: в dev-режиме получатель должен")
        print("  быть в списке разрешённых номеров тестового окружения;")
        print("- Telegram «chat not found»: получатель не начинал чат с ботом или chat_id неверен;")
        print("- 421 Misdirected Request (Bird): регион ключа не совпадает с BIRD_API_URL.")
        raise SystemExit(1)
    finally:
        await sender.close()


if __name__ == "__main__":
    asyncio.run(main())
