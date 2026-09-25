# Тесты разбора входящих событий вебхука Meta.
# Запуск: python tests/test_meta_payload.py

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whatsapp.meta_payload import parse_meta_events

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


def message(msg_type="text", **arms):
    """Сообщение формата Meta: from/id/type + ветка контента."""
    m = {"from": "77770000001", "id": "wamid.TEST1", "timestamp": "1609685060",
         "type": msg_type}
    m.update(arms)
    return m


def payload(*messages):
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": "106540352242922"},
                    "contacts": [{"profile": {"name": "Аня"}, "wa_id": "77770000001"}],
                    "messages": list(messages),
                },
            }],
        }],
    }


def main():
    print("[1] текстовое сообщение")
    events = parse_meta_events(payload(message(text={"body": "Привет"})))
    check("одно сообщение разобрано", len(events) == 1)
    inbound = events[0]
    check("номер с плюсом", inbound.phone == "+77770000001")
    check("имя из contacts", inbound.display_name == "Аня")
    check("текст извлечён", inbound.text == "Привет")
    check("content_kind=text", inbound.content_kind == "text")
    check("message_id", inbound.message_id.startswith("wamid."))

    print("[2] чужие/служебные события игнорируются")
    check("не наш объект", parse_meta_events({"object": "page", "entry": []}) == [])
    statuses_only = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages",
                                "value": {"statuses": [{"id": "wamid_x", "status": "delivered"}]}}]}],
    }
    check("statuses-only -> []", parse_meta_events(statuses_only) == [])
    check("не словарь -> []", parse_meta_events("nope") == [])
    check("пустой payload -> []", parse_meta_events({}) == [])

    print("[3] несколько entry/changes/messages в одном POST")
    multi = parse_meta_events({
        "object": "whatsapp_business_account",
        "entry": [
            {"changes": [
                {"field": "messages", "value": {"messages": [message(text={"body": "1"})]}},
                {"field": "other", "value": {}},
            ]},
            {"changes": [{"field": "messages",
                          "value": {"messages": [
                              message("image", image={"id": "m"}),
                              message(text={"body": "пока"}),
                          ]}}]},
        ],
    })
    check("3 сообщения из разных entry", len(multi) == 3)
    check("порядок сохранён", [e.text for e in multi] == ["1", "", "пока"])
    check("kind у нетекстового", multi[1].content_kind == "image")

    print("[4] кнопки и интерактив: текст извлекается")
    btn = parse_meta_events(payload(message("button", button={"text": "Отмена записи"})))[0]
    check("текст кнопки", btn.text == "Отмена записи")
    intr = parse_meta_events(payload(message(
        "interactive", interactive={"button_reply": {"id": "b1", "title": "Отменить"}})))[0]
    check("текст button_reply", intr.text == "Отменить")
    lst = parse_meta_events(payload(message(
        "interactive", interactive={"list_reply": {"id": "l1", "title": "Меню"}})))[0]
    check("текст list_reply", lst.text == "Меню")

    print("[5] битые данные")
    check("None -> []", parse_meta_events(None) == [])
    no_phone = parse_meta_events(payload({"id": "wamid_y", "type": "text", "text": {"body": "x"}}))
    check("сообщение без from пропускается", len(no_phone) == 0)

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
