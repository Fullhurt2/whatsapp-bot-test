"""Проверка исходящей отправки через активный WhatsApp-провайдер.

Запуск из корня проекта:
    python scripts/send_test.py --to +77770000000 --text "Тест бота"

Провайдер берётся из .env (MESSAGING_PROVIDER): bird — Bird API, meta —
WhatsApp Cloud API (Graph API). Номер получателя — в E.164 (с «+»).
Успех: Bird — 202 Accepted, Meta — 200 (сообщение принято в очередь).
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


def make_sender(settings):
    """Клиент активного провайдера — та же логика, что в main.WebhookState."""
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
    parser = argparse.ArgumentParser(description="Тестовая отправка WhatsApp")
    parser.add_argument("--to", required=True, help="номер получателя в E.164, например +77770000000")
    parser.add_argument("--text", default="Проверка связи от бота 🤖", help="текст сообщения")
    args = parser.parse_args()

    settings = get_settings()
    print(f"Бизнес: {settings.business_name}")
    print(f"Провайдер: {settings.messaging_provider}")
    if settings.messaging_provider == "meta":
        print(f"Graph API: v{settings.meta_graph_version} | phone_number_id: {settings.whatsapp_phone_number_id}")
    else:
        print(f"API: {settings.bird_api_url} | from: {settings.whatsapp_sender_number}")
    print(f"Получатель: {args.to}")
    print(f"Текст: {args.text!r}\n")

    sender = make_sender(settings)
    try:
        await sender.send_text(args.to, args.text)
        print("OK: провайдер принял сообщение. Проверьте WhatsApp получателя.")
    except MessagingError as exc:
        print(f"ОШИБКА: {exc}")
        print("\nПодсказки:")
        print("- 401: неверный токен (BIRD_API_KEY / WHATSAPP_ACCESS_TOKEN);")
        print("- Meta 190: токен истёк или не хватает прав System User;")
        print("- Meta (131030) получатель не в списке: в dev-режиме получатель должен")
        print("  быть в списке разрешённых номеров тестового окружения;")
        print("- 421 Misdirected Request (Bird): регион ключа не совпадает с BIRD_API_URL.")
        raise SystemExit(1)
    finally:
        await sender.close()


if __name__ == "__main__":
    asyncio.run(main())
