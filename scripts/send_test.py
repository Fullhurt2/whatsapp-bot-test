"""Проверка исходящей отправки через активный провайдер.

Запуск из корня проекта:
    python scripts/send_test.py --to +77770000000 --text "Тест бота"

Провайдер берётся из .env (MESSAGING_PROVIDER): zernio — Zernio API, meta —
WhatsApp Cloud API (Graph API), telegram — Bot API (тогда --to это chat_id).
Успех: Zernio — диалог открыт шаблоном/сообщение принято, Meta/Telegram — 200.

Внимание: Zernio не умеет слать свободный текст «в никуда» — вне 24-часового
окна нужен шаблон. Для smoke-проверки задайте owner_template_name в конфиге
(уйдёт через send_template) или --conversation-id уже открытого диалога.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings
from whatsapp.errors import MessagingError
from whatsapp.meta_client import MetaError, MetaWhatsAppClient
from whatsapp.telegram_client import TelegramClient
from whatsapp.zernio_client import ZernioError, ZernioWhatsAppClient


def make_sender(settings):
    """Клиент активного провайдера — та же логика, что в main.build_sender."""
    if settings.messaging_provider == "telegram":
        return TelegramClient(settings.telegram_bot_token)
    if settings.messaging_provider == "zernio":
        return ZernioWhatsAppClient(
            settings.zernio_api_key,
            settings.zernio_account_id,
            base_url=settings.zernio_base_url,
        )
    return MetaWhatsAppClient(
        settings.whatsapp_access_token,
        settings.whatsapp_phone_number_id,
        graph_version=settings.meta_graph_version,
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Тестовая отправка сообщения")
    parser.add_argument("--to", required=True,
                        help="получатель: номер в E.164 (+77770000000) или chat_id для Telegram")
    parser.add_argument("--text", default="Проверка связи от бота 🤖", help="текст сообщения")
    parser.add_argument("--conversation-id", default="",
                        help="Zernio: id открытого диалога (иначе нужен шаблон)")
    args = parser.parse_args()

    settings = get_settings()
    print(f"Бизнес: {settings.business_name}")
    print(f"Провайдер: {settings.messaging_provider}")
    if settings.messaging_provider == "meta":
        print(f"Graph API: v{settings.meta_graph_version} | phone_number_id: {settings.whatsapp_phone_number_id}")
    elif settings.messaging_provider == "telegram":
        print("Telegram Bot API (получатель --to — это chat_id)")
    else:
        print(f"Zernio API: {settings.zernio_base_url} | account_id: {settings.zernio_account_id}")
    print(f"Получатель: {args.to}")
    print(f"Текст: {args.text!r}\n")

    sender = make_sender(settings)
    try:
        is_zernio = settings.messaging_provider == "zernio"
        if is_zernio and not args.conversation_id and settings.owner_template_name:
            # Первый контакт: открываем диалог approved-шаблоном.
            print(f"Шаблон: {settings.owner_template_name} "
                  f"({settings.owner_template_language or 'ru'})")
            await sender.send_template(
                to=args.to,
                template_name=settings.owner_template_name,
                language=settings.owner_template_language or "ru",
                params=[args.text, args.text],
            )
        else:
            await sender.send_text(
                args.to, args.text, conversation_id=args.conversation_id,
            )
        print("OK: провайдер принял сообщение. Проверьте получателя.")
    except MessagingError as exc:
        print(f"ОШИБКА: {exc}")
        print("\nПодсказки:")
        print("- 401: неверный токен (ZERNIO_API_KEY / WHATSAPP_ACCESS_TOKEN / TELEGRAM_BOT_TOKEN);")
        print("- Meta 190: токен истёк или не хватает прав System User;")
        print("- Meta (131030) получатель не в списке: в dev-режиме получатель должен")
        print("  быть в списке разрешённых номеров тестового окружения;")
        print("- Zernio TEMPLATE_REQUIRED: свободный текст вне 24ч-окна — нужен шаблон")
        print("  (--conversation-id открытого диалога или owner_template_name в конфиге);")
        print("- Zernio PLATFORM_LIMITATION / 131056: слишком частые сообщения одному получателю;")
        print("- Telegram «chat not found»: получатель не начинал чат с ботом или chat_id неверен.")
        raise SystemExit(1)
    finally:
        await sender.close()


if __name__ == "__main__":
    asyncio.run(main())
