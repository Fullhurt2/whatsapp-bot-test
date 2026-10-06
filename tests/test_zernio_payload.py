# Разбор конверта вебхука Zernio (message.received) -> InboundMessage.
# Запуск: python tests/test_zernio_payload.py

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whatsapp.zernio_payload import parse_zernio_events

ACCOUNT_ID = "66b2e19d8c3f5a7e9d0b1c2d"
CONVERSATION_ID = "66c3d2ae7b4f6c8d0e1f2a3b"

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


def envelope(*, event="message.received", platform="whatsapp", direction="incoming",
             text="Привет", attachments=None, sender=None, metadata=None,
             account_id=ACCOUNT_ID, conversation_id=CONVERSATION_ID,
             platform_message_id="wamid.ABC", message_id="msg-1"):
    message = {
        "id": message_id,
        "conversationId": conversation_id,
        "platform": platform,
        "platformMessageId": platform_message_id,
        "direction": direction,
        "text": text,
        "attachments": attachments or [],
        "sender": sender if sender is not None else {
            "id": "77770000001",
            "phoneNumber": "+77770000001",
            "name": "Аня",
        },
        "sentAt": "2027-01-04T14:00:03Z",
        "isRead": False,
        "sentVia": None,
    }
    payload = {
        "id": "3f0c1c2e-6c4a-4d3e-9b1f-2a7d8e9f0a1b",
        "event": event,
        "message": message,
        "conversation": {"id": conversation_id},
        "account": {"id": account_id, "accountId": account_id, "profileId": "p1",
                    "platform": "whatsapp"},
        "timestamp": "2027-01-04T14:00:04Z",
    }
    if metadata is not None:
        payload["metadata"] = metadata
    return payload


def main():
    print("[1] обычное текстовое сообщение")
    result = parse_zernio_events(envelope(text="Сколько стоит маникюр?"))
    check("одно сообщение", len(result) == 1)
    msg = result[0]
    check("номер E.164", msg.phone == "+77770000001")
    check("имя отправителя", msg.display_name == "Аня")
    check("текст", msg.text == "Сколько стоит маникюр?")
    check("content_kind=text", msg.content_kind == "text")
    check("message_id из platformMessageId", msg.message_id == "wamid.ABC")
    check("account_id", msg.account_id == ACCOUNT_ID)
    check("ключ маршрутизации = accountId", msg.phone_number_id == ACCOUNT_ID)
    check("conversation_id", msg.conversation_id == CONVERSATION_ID)

    print("[2] нетекстовое вложение")
    img = parse_zernio_events(envelope(text=None, attachments=[
        {"type": "image", "url": "https://zernio.com/api/v1/whatsapp/media/1"}]))
    check("content_kind=image", img[0].content_kind == "image")
    check("текст пустой", img[0].text == "")

    print("[3] не наше событие")
    check("иное событие -> []",
          parse_zernio_events(envelope(event="user.typing")) == [])
    check("исходящее -> []",
          parse_zernio_events(envelope(direction="outgoing")) == [])
    check("другая платформа -> []",
          parse_zernio_events(envelope(platform="instagram")) == [])
    check("пустой payload -> []", parse_zernio_events({}) == [])
    check("не словарь -> []", parse_zernio_events("нет") == [])

    print("[4] standby (ответ ведёт Meta Business Agent) пропускается")
    check("standby -> []",
          parse_zernio_events(envelope(metadata={"standby": True})) == [])
    check("standby=false обрабатывается",
          len(parse_zernio_events(envelope(metadata={"standby": False}))) == 1)

    print("[5] отправитель без номера (BSUID)")
    bsuid = envelope(sender={"id": "US.123", "businessScopedUserId": "US.123",
                             "name": "Bob"})
    result = parse_zernio_events(bsuid)
    check("BSUID как ключ диалога", result[0].phone == "US.123")
    check("номер не выдуман", result[0].phone != "+US.123")

    print("[6] отправитель без идентификатора — сообщение пропускается")
    check("нет sender -> []",
          parse_zernio_events(envelope(sender={})) == [])

    print("[7] бары конверта: нет account, conversation в корне")
    payload = envelope(account_id="")
    payload["account"] = {}
    payload["message"]["conversationId"] = None
    result = parse_zernio_events(payload)
    check("account_id пустой, но сообщение собрано", len(result) == 1)
    check("conversation_id из conversation.id",
          result[0].conversation_id == CONVERSATION_ID)

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
