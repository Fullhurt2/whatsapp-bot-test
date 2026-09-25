# Тесты BirdWhatsAppClient через httpx.MockTransport: наружу ничего не уходит.
# Проверяются формат запроса, чанкинг, ретраи 5xx/429 и маппинг ошибок.
# Запуск: python tests/test_bird_client.py

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from whatsapp.bird_client import BirdError, BirdWhatsAppClient

API_URL = "https://eu1.platform.bird.com"
SENDER = "+77000000000"
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
    return BirdWhatsAppClient("test-key", API_URL, SENDER, transport=httpx.MockTransport(handler))


async def main():
    print("[1] формат запроса к Bird API")
    posts = []

    def handler_ok(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(202, json={"id": "wam_1", "status": "accepted"})

    client = make_client(handler_ok)
    await client.send_text("+7 777 (123) 45-67", "привет")
    check("один POST", len(posts) == 1)
    request = posts[0]
    check("путь /v1/whatsapp/messages", request.url.path == "/v1/whatsapp/messages")
    check("Bearer-авторизация", request.headers["Authorization"] == "Bearer test-key")
    check("Idempotency-Key задан", bool(request.headers.get("Idempotency-Key")))
    body = json.loads(request.content)
    check("from — бизнес-номер", body["from"] == SENDER)
    check("to нормализован", body["to"] == "+77771234567")
    check("текст на месте", body["text"]["body"] == "привет")

    print("[2] чанкинг: 9000 символов -> 3 запроса по <= 4096")
    parts = []

    def handler_chunk(request: httpx.Request) -> httpx.Response:
        parts.append(json.loads(request.content)["text"]["body"])
        return httpx.Response(202)

    await make_client(handler_chunk).send_text(TO, "x" * 9000)
    check("3 запроса", len(parts) == 3)
    check("каждая часть <= 4096", all(len(p) <= 4096 for p in parts))
    check("конкатенация без потерь", "".join(parts) == "x" * 9000)

    print("[3] 500 -> один повтор, успех")
    attempts = []

    def handler_retry(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(500) if len(attempts) == 1 else httpx.Response(202)

    await make_client(handler_retry).send_text(TO, "hi")
    check("ровно 2 попытки", len(attempts) == 2)

    print("[4] 429 c Retry-After -> повтор")
    tries429 = []

    def handler_429(request: httpx.Request) -> httpx.Response:
        tries429.append(1)
        if len(tries429) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"error": "rate"})
        return httpx.Response(202)

    await make_client(handler_429).send_text(TO, "hi")
    check("после 429 повтор успешен", len(tries429) == 2)

    print("[5] постоянная ошибка -> BirdError")
    raised = False

    def handler_422(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "WhatsAppSenderRequired"})

    try:
        await make_client(handler_422).send_text(TO, "текст")
    except BirdError:
        raised = True
    check("BirdError выброшен", raised)

    print("[6] пустой текст не отправляется")
    posts_count = []

    def handler_count(request: httpx.Request) -> httpx.Response:
        posts_count.append(1)
        return httpx.Response(202)

    await make_client(handler_count).send_text(TO, "")
    check("запросов не было", len(posts_count) == 0)

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


# Общий сценарий: тестовый клиент Bird с заглушкой транспорта.
API_URL = "https://eu1.platform.bird.com"
SENDER = "+77000000000"
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


if __name__ == "__main__":
    asyncio.run(main())
