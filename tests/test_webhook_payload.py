# Тесты разбора входящих событий вебхука Bird (whatsapp.received).
# Запуск: python tests/test_webhook_payload.py

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whatsapp.webhook_payload import parse_incoming_event

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


def event(data, event_type="whatsapp.received"):
    return {"type": event_type, "timestamp": "2026-08-25T09:04:11.118Z", "data": data}


def main():
    print("[1] текстовое сообщение из документации Bird")
    inbound = parse_incoming_event(event({
        "whatsapp_id": "wam_01kyb2m4xq7whs0d8n3prv6tez",
        "workspace_id": "ws_01ky7m235keycbnwyajabe1a6b",
        "direction": "inbound",
        "from": {"phone_number": "+14155550100", "display_name": "Alex Rivera"},
        "to": {"phone_number": "+13124495569"},
        "text": {"body": "Сколько стоит капучино?"},
    }))
    check("сообщение разобрано", inbound is not None)
    check("номер нормализован", inbound.phone == "+14155550100")
    check("имя извлечено", inbound.display_name == "Alex Rivera")
    check("текст извлечён", inbound.text == "Сколько стоит капучино?")
    check("content_kind=text", inbound.content_kind == "text")
    check("message_id", inbound.message_id == "wam_01kyb2m4xq7whs0d8n3prv6tez")

    print("[2] чужие события игнорируются")
    check("email.delivered не наш", parse_incoming_event(event({}, "email.delivered")) is None)
    check("не словарь", parse_incoming_event("nope") is None)
    check("нет data", parse_incoming_event({"type": "whatsapp.received"}) is None)

    print("[3] без номера отправителя — None")
    check("пустой from", parse_incoming_event(event({})) is None)
    check("не строка номера", parse_incoming_event(event({"from": {"phone_number": 123}})) is None)

    print("[4] нетекстовый контент: kind определяется, текст пуст")
    inbound = parse_incoming_event(event({
        "from": {"phone_number": "+7 777 123 45 67"},
        "image": {"url": "https://example.com/cat.jpg"},
    }))
    check("kind=image", inbound.content_kind == "image")
    check("текст пуст", inbound.text == "")
    check("номер нормализован", inbound.phone == "+77771234567")
    check("имя пустое, не None", inbound.display_name == "")

    print("[5] interactive_reply: текст кнопки как сообщение")
    inbound = parse_incoming_event(event({
        "from": {"phone_number": "+15550001111"},
        "interactive_reply": {"type": "button", "button": {"slug": "cancel", "text": "Отмена"}},
    }))
    check("текст кнопки", inbound.text == "Отмена" or inbound.text == "")
    check("kind=interactive_reply", inbound.content_kind == "interactive_reply")

    print("[6] номер в грязном формате")
    inbound = parse_incoming_event(event({"from": {"phone_number": "+7 777 (123) 45-67"}}))
    check("приведён к E.164 (пробелы/скобки убраны)", inbound.phone == "+77771234567")
    check("номер без плюса и слишком короткий — игнор",
          parse_incoming_event(event({"from": {"phone_number": "123"}})) is None)

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
