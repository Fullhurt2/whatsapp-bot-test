"""Проверка связки с Bird API: отправка одного WhatsApp-сообщения.

Запуск из корня проекта:
    python scripts/send_test.py --to +77770000000 --text "Тест бота"

Номер получателя — в формате E.164 (обязательно с «+»). Сообщение придёт
от бизнес-номера из .env (WHATSAPP_SENDER_NUMBER). Успех — 202 Accepted:
в логе появится id сообщения Bird (wam_...), доставка асинхронная.
"""

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings
from whatsapp.bird_client import BirdError, BirdWhatsAppClient


async def main() -> None:
    parser = argparse.ArgumentParser(description="Тестовая отправка WhatsApp через Bird API")
    parser.add_argument("--to", required=True, help="номер получателя в E.164, например +77770000000")
    parser.add_argument("--text", default="Проверка связи от бота 🤖", help="текст сообщения")
    args = parser.parse_args()

    settings = get_settings()
    print(f"Бизнес: {settings.business_name}")
    print(f"Отправитель (from): {settings.whatsapp_sender_number}")
    print(f"Получатель (to): {args.to}")
    print(f"API: {settings.bird_api_url}")
    print(f"Текст: {args.text!r}\n")

    bird = BirdWhatsAppClient(settings.bird_api_key, settings.bird_api_url,
                              settings.whatsapp_sender_number)
    try:
        await bird.send_text(args.to, args.text)
        print("OK: Bird принял сообщение (202). Проверьте WhatsApp получателя.")
    except BirdError as exc:
        print(f"ОШИБКА: {exc}")
        print("\nПодсказки:")
        print("- статус 401: неверный BIRD_API_KEY;")
        print("- 422 WhatsAppSenderRequired/NotFound: проверьте WHATSAPP_SENDER_NUMBER —")
        print("  это должен быть номер отправителя, подключённый к workspace Bird;")
        print("- 421 Misdirected Request: регион ключа не совпадает с BIRD_API_URL.")
        raise SystemExit(1)
    finally:
        await bird.close()


if __name__ == "__main__":
    asyncio.run(main())
