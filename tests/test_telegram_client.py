# Тесты TelegramClient через httpx.MockTransport: наружу ничего не уходит.
# Проверяются формат запросов к Bot API, чанкинг, проверка ok:false, ретраи,
# маппинг ошибок, а также setWebhook/getMe.
# Запуск: python tests/test_telegram_client.py

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from whatsapp.errors import MessagingError
from whatsapp.telegram_client import TelegramClient

TOKEN = "123456789:AAHhqwertyuiopasdfghjklzxcvbnm"
CHAT_ID = "777"

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


def make_client(handler):
    return TelegramClient(TOKEN, transport=httpx.MockTransport(handler))


def ok_response(**result):
    return httpx.Response(200, json={"ok": True, "result": result})


async def main():
    print("[1] формат sendMessage")
    posts = []

    def handler_ok(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return ok_response(message_id=1)

    await make_client(handler_ok).send_text(CHAT_ID, "привет")
    check("один POST", len(posts) == 1)
    request = posts[0]
    check("путь содержит токен и sendMessage",
          request.url.path == f"/bot{TOKEN}/sendMessage")
    body = json.loads(request.content)
    check("chat_id на месте", body["chat_id"] == CHAT_ID)
    check("текст на месте", body["text"] == "привет")

    print("[2] чанкинг: 9000 символов -> 3 запроса по <= 4096")
    parts = []

    def handler_chunk(request: httpx.Request) -> httpx.Response:
        parts.append(json.loads(request.content)["text"])
        return ok_response()

    await make_client(handler_chunk).send_text(CHAT_ID, "x" * 9000)
    check("3 запроса", len(parts) == 3)
    check("каждая часть <= 4096", all(len(p) <= 4096 for p in parts))
    check("конкатенация без потерь", "".join(parts) == "x" * 9000)

    print("[3] HTTP 200 с ok:false -> MessagingError")
    raised = False

    def handler_ok_false(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error_code": 403,
                                         "description": "bot was blocked by the user"})

    try:
        await make_client(handler_ok_false).send_text(CHAT_ID, "текст")
    except MessagingError as exc:
        raised = "blocked" in str(exc)
    check("TelegramError с текстом описания", raised)

    print("[4] 429 -> один повтор, успех")
    attempts = []

    def handler_429(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, json={"ok": False, "error_code": 429,
                                             "parameters": {"retry_after": 0}})
        return ok_response()

    await make_client(handler_429).send_text(CHAT_ID, "hi")
    check("ровно 2 попытки", len(attempts) == 2)

    print("[5] постоянная ошибка 400 -> MessagingError")
    failed_400 = False

    def handler_400(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "error_code": 400,
                                         "description": "chat not found"})

    try:
        await make_client(handler_400).send_text(CHAT_ID, "текст")
    except MessagingError:
        failed_400 = True
    check("MessagingError выброшен", failed_400)

    print("[6] пустой текст не отправляется")
    calls = []

    def handler_count(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return ok_response()

    await make_client(handler_count).send_text(CHAT_ID, "")
    check("запросов не было", len(calls) == 0)

    print("[7] setWebhook: url, secret_token, allowed_updates")
    webhook_posts = []

    def handler_webhook(request: httpx.Request) -> httpx.Response:
        webhook_posts.append(request)
        return ok_response()

    client = make_client(handler_webhook)
    await client.set_webhook("https://example.com/webhooks/telegram/123", "sec-1")
    check("один POST", len(webhook_posts) == 1)
    check("путь setWebhook", webhook_posts[0].url.path == f"/bot{TOKEN}/setWebhook")
    body = json.loads(webhook_posts[0].content)
    check("url передан", body["url"] == "https://example.com/webhooks/telegram/123")
    check("secret_token передан", body["secret_token"] == "sec-1")
    check("allowed_updates=[message]", body["allowed_updates"] == ["message"])

    print("[8] getMe возвращает result")
    def handler_get_me(request: httpx.Request) -> httpx.Response:
        return ok_response(id=123456789, username="jauap_bot")

    me = await make_client(handler_get_me).get_me()
    check("id бота получен", str(me.get("id")) == "123456789")

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
