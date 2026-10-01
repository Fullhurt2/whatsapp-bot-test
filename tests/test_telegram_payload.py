# Тесты разбора входящих событий вебхука Telegram Bot API.
# Запуск: python tests/test_telegram_payload.py

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whatsapp.telegram_payload import parse_telegram_update

BOT_ID = "123456789"

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


def update(update_id, **message):
    """Update формата Telegram: update_id + message."""
    base = {
        "message_id": update_id,
        "from": {"id": 777, "is_bot": False, "first_name": "Аня", "last_name": "Қ"},
        "chat": {"id": 777, "type": "private", "first_name": "Аня"},
        "date": 1700000000,
    }
    base.update(message)
    return {"update_id": update_id, "message": base}


def main():
    print("[1] текстовое сообщение")
    events = parse_telegram_update(update(1, text="Привет"), BOT_ID)
    check("одно сообщение", len(events) == 1)
    inbound = events[0]
    check("chat_id как ключ диалога (строка)", inbound.phone == "777")
    check("имя склеено из first/last", inbound.display_name == "Аня Қ")
    check("текст извлечён", inbound.text == "Привет")
    check("content_kind=text", inbound.content_kind == "text")
    check("message_id = update_id", inbound.message_id == "1")
    check("phone_number_id = id бота", inbound.phone_number_id == BOT_ID)

    print("[2] команда /start сохраняется как текст")
    events = parse_telegram_update(update(2, text="/start"), BOT_ID)
    check("текст /start", events[0].text == "/start")

    print("[3] групповой чат: отрицательный chat_id и имя")
    group = {"message_id": 3, "from": {"id": 42},
             "chat": {"id": -1001234567890, "type": "group", "title": "Клиенты"},
             "date": 1, "text": "вопрос"}
    events = parse_telegram_update({"update_id": 3, "message": group}, BOT_ID)
    check("отрицательный chat_id", events[0].phone == "-1001234567890")
    check("имя из title, если у отправителя его нет", events[0].display_name == "Клиенты")

    named = {"message_id": 4, "from": {"id": 42, "first_name": "Иван"},
             "chat": {"id": -1001234567890, "type": "group", "title": "Клиенты"},
             "date": 1, "text": "вопрос"}
    events = parse_telegram_update({"update_id": 4, "message": named}, BOT_ID)
    check("имя отправителя важнее title группы", events[0].display_name == "Иван")

    print("[4] медиа без подписи -> нетекстовый контент")
    events = parse_telegram_update(update(4, photo=[{"file_id": "x"}]), BOT_ID)
    check("content_kind=photo", events[0].content_kind == "photo")
    check("текст пустой", events[0].text == "")

    print("[5] медиа с подписью -> текст из caption")
    events = parse_telegram_update(
        update(5, voice={"file_id": "v"}, caption="Сколько стоит?"), BOT_ID)
    check("caption как текст", events[0].text == "Сколько стоит?")
    check("content_kind=text при caption", events[0].content_kind == "text")

    print("[6] не-наши события игнорируются")
    check("edited_message -> []",
          parse_telegram_update({"update_id": 6, "edited_message": {"text": "x"}}, BOT_ID) == [])
    check("callback_query -> []",
          parse_telegram_update({"update_id": 7, "callback_query": {"id": "1"}}, BOT_ID) == [])
    check("не словарь -> []", parse_telegram_update("мусор", BOT_ID) == [])

    print("[7] сообщение без chat/from id пропускается")
    check("нет id -> []",
          parse_telegram_update({"update_id": 8, "message": {"text": "x"}}, BOT_ID) == [])

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
