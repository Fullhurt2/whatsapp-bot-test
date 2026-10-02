# Тесты ZernioWhatsAppClient через httpx.MockTransport: наружу ничего не уходит.
# Проверяются формат запросов (reply/шаблон), чанкинг, ретраи и маппинг ошибок.
# Запуск: python tests/test_zernio_client.py

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from whatsapp.errors import MessagingError
from whatsapp.zernio_client import ZernioError, ZernioWhatsAppClient

ACCOUNT_ID = "66b2e19d8c3f5a7e9d0b1c2d"
CONVERSATION_ID = "66c3d2ae7b4f6c8d0e1f2a3b"
API_KEY = "sk_test_key"
BASE = "https://zernio.com/api/v1"
TO = "+77771234567"

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
    return ZernioWhatsAppClient(
        API_KEY, ACCOUNT_ID, base_url=BASE, transport=httpx.MockTransport(handler),
    )


async def main():
    print("[1] reply в диалог: путь, авторизация, тело")
    posts = []

    def handler_ok(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(200, json={"success": True,
                                         "data": {"messageId": "wamid.1"}})

    await make_client(handler_ok).send_text(
        TO, "привет", conversation_id=CONVERSATION_ID)
    check("один POST", len(posts) == 1)
    request = posts[0]
    check("путь reply", request.url.path == f"/api/v1/inbox/conversations/{CONVERSATION_ID}/messages")
    check("Bearer-авторизация", request.headers["Authorization"] == f"Bearer {API_KEY}")
    check("есть Idempotency-Key", bool(request.headers.get("Idempotency-Key")))
    body = json.loads(request.content)
    check("accountId в теле", body["accountId"] == ACCOUNT_ID)
    check("текст в поле message", body["message"] == "привет")

    print("[2] без conversation_id свободный текст невозможен")
    raised = False
    try:
        await make_client(handler_ok).send_text(TO, "привет")
    except MessagingError:
        raised = True
    check("ZernioError выброшен", raised)

    print("[3] чанкинг: 9000 символов -> 3 запроса по <= 4096")
    parts = []

    def handler_chunk(request: httpx.Request) -> httpx.Response:
        parts.append(json.loads(request.content)["message"])
        return httpx.Response(200)

    await make_client(handler_chunk).send_text(
        TO, "x" * 9000, conversation_id=CONVERSATION_ID)
    check("3 запроса", len(parts) == 3)
    check("каждая часть <= 4096", all(len(p) <= 4096 for p in parts))
    check("конкатенация без потерь", "".join(parts) == "x" * 9000)

    print("[4] шаблон открывает диалог")
    tpl_posts = []

    def handler_tpl(request: httpx.Request) -> httpx.Response:
        tpl_posts.append(request)
        return httpx.Response(201, json={"success": True,
                                         "data": {"conversationId": "new-conv"}})

    await make_client(handler_tpl).send_template(
        "8 777 123 45 67", "owner_alert", "ru", ["Аня", "нужен человек"])
    check("один POST", len(tpl_posts) == 1)
    tpl = tpl_posts[0]
    check("путь открытия диалога", tpl.url.path == "/api/v1/inbox/conversations")
    tpl_body = json.loads(tpl.content)
    check("accountId", tpl_body["accountId"] == ACCOUNT_ID)
    check("participantId без + и пробелов", tpl_body["participantId"] == "87771234567")
    check("templateName", tpl_body["templateName"] == "owner_alert")
    check("templateLanguage", tpl_body["templateLanguage"] == "ru")
    check("templateParams по порядку", tpl_body["templateParams"] == ["Аня", "нужен человек"])

    print("[5] 500 -> один повтор, успех")
    attempts = []

    def handler_retry(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500) if len(attempts) == 1 else httpx.Response(200)

    await make_client(handler_retry).send_text(
        TO, "hi", conversation_id=CONVERSATION_ID)
    check("ровно 2 попытки", len(attempts) == 2)

    print("[6] 429 c Retry-After -> повтор")
    tries429 = []

    def handler_429(request: httpx.Request) -> httpx.Response:
        tries429.append(1)
        if len(tries429) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"},
                                  json={"error": "Rate limit exceeded"})
        return httpx.Response(200)

    await make_client(handler_429).send_text(TO, "hi", conversation_id=CONVERSATION_ID)
    check("после 429 повтор успешен", len(tries429) == 2)

    print("[7] ошибка платформы -> MessagingError с кодом")
    text_holder = {}

    def handler_400(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": "Template required", "type": "invalid_request_error",
            "code": "TEMPLATE_REQUIRED", "platform": "whatsapp",
        })

    try:
        await make_client(handler_400).send_text(TO, "текст", conversation_id=CONVERSATION_ID)
    except ZernioError as exc:
        text_holder["text"] = str(exc)
    check("MessagingError выброшен", "text" in text_holder)
    check("код в тексте ошибки", "TEMPLATE_REQUIRED" in text_holder.get("text", ""))
    check("платформа в тексте ошибки", "whatsapp" in text_holder.get("text", ""))

    print("[8] пустой текст не отправляется")
    count = []

    def handler_count(request: httpx.Request) -> httpx.Response:
        count.append(1)
        return httpx.Response(200)

    await make_client(handler_count).send_text(TO, "", conversation_id=CONVERSATION_ID)
    check("запросов не было", len(count) == 0)

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
