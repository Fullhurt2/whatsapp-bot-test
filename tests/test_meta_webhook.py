# Интеграционные тесты вебхука Meta (/webhooks/meta): GET-верификация подписки,
# X-Hub-Signature-256, ack, дедуп, фоновая обработка. LLM и Meta подменены
# стабами — наружу ничего не уходит.
# Запуск: python tests/test_meta_webhook.py

import hashlib
import hmac
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging
logging.basicConfig(level=logging.CRITICAL)

from fastapi.testclient import TestClient

from config.settings import LLMParams, Settings
from handlers.message_handler import MessageProcessor

# --- тестовые константы ---
VERIFY_TOKEN = "мой-verify-token"
APP_SECRET = "test-app-secret"
CLIENT_PHONE = "+77770000001"
EXPECTED_CHALLENGE = "1158201444"

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


def sign(app_secret: str, body: bytes) -> str:
    """Подпись так же, как её делает Meta: sha256=<hex(hmac-sha256)>."""
    digest = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def build_settings() -> Settings:
    return Settings(
        messaging_provider="meta",
        whatsapp_access_token="test-token",
        whatsapp_phone_number_id="106540352242922",
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
        meta_graph_version="v21.0",
        bird_api_key="",
        bird_webhook_secret="",
        bird_api_url="",
        whatsapp_sender_number="",
        app_host="127.0.0.1",
        app_port=8000,
        llm_api_url="https://llm.test/v1",
        llm_api_key="test",
        business_name="Тестовый Бизнес",
        tone="вежливый",
        language="auto",
        knowledge_base="Часы работы: 10:00-20:00.",
        owner_phone="+70000000000",
        fallback_triggers=["жалоба"],
        llm=LLMParams(model="m", temperature=0.6, max_tokens=100,
                      timeout_seconds=15, reasoning_effort=None),
    )


class FakeBird:
    def __init__(self):
        self.sent = []

    async def send_text(self, to, text):
        self.sent.append((to, text))

    async def close(self):
        pass


class StubLLM:
    async def chat(self, system_prompt, user_message, history=None):
        return "ответ от LLM"


class FakeSender:
    """Стаб Bird/Meta-клиента: собирает отправки, наружу не ходит."""

    def __init__(self):
        self.sent = []

    async def send_text(self, to, text):
        self.sent.append((to, text))

    async def close(self):
        pass


def meta_body(phone: str, text: str, message_id: str) -> bytes:
    """Payload вебхука Meta по формату документации."""
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": "106540352242922"},
                    "contacts": [{"profile": {"name": "Аня"}, "wa_id": phone.lstrip("+")}],
                    "messages": [{"from": phone.lstrip("+"), "id": message_id,
                                  "timestamp": "1609685060", "type": "text",
                                  "text": {"body": text}}],
                },
            }],
        }],
    }).encode()


def main():
    from main import create_app

    settings = build_settings()
    app = create_app(settings)

    def meta_headers(body: bytes) -> dict:
        return {"X-Hub-Signature-256": sign(APP_SECRET, body)}

    with TestClient(app) as client:
        state = app.state.state
        fake_sender = FakeSender()
        state.sender = fake_sender
        state.processor = MessageProcessor(settings, StubLLM(), fake_sender)

        body = meta_body("+77770000001", "привет", "wamid_1")
        headers = {"X-Hub-Signature-256": sign(APP_SECRET, body)}

        print("[1] GET-верификация подписки (первичная привязка вебхука)")
        r = client.get("/webhooks/meta", params={
            "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": EXPECTED_CHALLENGE})
        check("challenge возвращён как текст",
              r.status_code == 200 and r.text == "1158201444")
        r = client.get("/webhooks/meta", params={
            "hub.mode": "subscribe", "hub.verify_token": "чужой",
            "hub.challenge": "1"})
        check("неверный verify_token -> 403", r.status_code == 403)

        print("[2] валидная подпись -> 200 и обработка в фоне")
        r = client.post("/webhooks/meta", content=body, headers=headers)
        check("статус 200", r.status_code == 200)
        check("ack", r.json() == {"ok": True})
        replies = [t for to, t in fake_sender.sent if to == CLIENT_PHONE]
        check("клиенту отправлен ответ LLM", replies == ["ответ от LLM"])

        print("[3] повтор доставки того же wamid -> дедупликация")
        r = client.post("/webhooks/meta", content=body, headers=headers)
        check("200", r.status_code == 200)
        replies = [t for to, t in fake_sender.sent if to == CLIENT_PHONE]
        check("второй раз ответ не отправлялся", len(replies) == 1)

        print("[4] чужая подпись -> 401")
        r = client.post("/webhooks/meta", content=body,
                        headers={"X-Hub-Signature-256": "sha256=deadbeef"})
        check("401", r.status_code == 401)

        print("[5] без подписи -> 401")
        r = client.post("/webhooks/meta", content=body)
        check("401 без заголовков", r.status_code == 401)

        print("[6] statuses-only -> 200 без обработки")
        statuses_body = json.dumps({
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"field": "messages",
                                    "value": {"statuses": [{"id": "wamid_s", "status": "delivered"}]}}]}],
        }).encode()
        r = client.post("/webhooks/meta", content=statuses_body,
                        headers={"X-Hub-Signature-256": sign(APP_SECRET, statuses_body)})
        check("200 на статусы", r.status_code == 200 and r.json() == {"ok": True})

        print("[8] healthz")
        r = client.get("/healthz")
        check("healthz ok", r.status_code == 200 and r.json()["status"] == "ok")

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
