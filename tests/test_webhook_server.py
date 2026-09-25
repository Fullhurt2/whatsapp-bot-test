# Интеграционные тесты вебхука FastAPI: подпись, ack, дедуп, фоновая обработка.
# LLM и Bird подменены стабами — наружу ничего не уходит.
# Запуск: python tests/test_webhook_server.py

import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging
logging.basicConfig(level=logging.CRITICAL)

from fastapi.testclient import TestClient

from config.settings import LLMParams, Settings
from handlers.message_handler import MessageProcessor

# --- тестовые константы ---
SECRET = "whsec_" + base64.b64encode(b"test-webhook-secret-key").decode()

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


def sign(secret: str, webhook_id: str, timestamp: str, body: bytes) -> str:
    """Подпись так же, как её делает Bird."""
    key = base64.b64decode(secret.removeprefix("whsec_"))
    payload = f"{webhook_id}.{timestamp}.".encode() + body
    return "v1," + base64.b64encode(hmac.new(key, payload, hashlib.sha256).digest()).decode()


def build_settings() -> Settings:
    return Settings(
        bird_api_key="test-key",
        bird_webhook_secret=SECRET,
        bird_api_url="https://eu1.platform.bird.com",
        whatsapp_sender_number="+77000000000",
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
    """Собирает отправки; наружу не ходит."""

    def __init__(self):
        self.sent = []

    async def send_text(self, to, text):
        self.sent.append((to, text))

    async def close(self):
        pass  # реальный клиент закрывает httpx-сессию; стабу нечего закрывать


class StubLLM:
    async def chat(self, system_prompt, user_message, history=None):
        return "ответ от LLM"


def event_body(phone: str, text: str) -> bytes:
    return json.dumps({
        "type": "whatsapp.received",
        "data": {
            "whatsapp_id": "wam_test",
            "from": {"phone_number": phone, "display_name": "Аня"},
            "text": {"body": text},
        },
    }).encode()


def main():
    from main import create_app

    settings = build_settings()
    app = create_app(settings)

    def webhooks_headers(webhook_id: str, body: bytes) -> dict:
        ts = str(int(time.time()))
        return {"webhook-id": webhook_id, "webhook-timestamp": ts,
                "webhook-signature": sign(SECRET, webhook_id, ts, body)}

    client = TestClient(app)

    print("[1] валидная подпись -> 200, обработка в фоне")
    with client:
        state = app.state.state
        fake_bird = FakeBird()
        state.bird = fake_bird
        state.processor = MessageProcessor(settings, StubLLM(), fake_bird)

        body = json.dumps({
            "type": "whatsapp.received",
            "data": {
                "whatsapp_id": "wam_test_1",
                "from": {"phone_number": "+77770000001", "display_name": "Аня"},
                "text": {"body": "привет"},
            },
        }).encode()
        ts = str(int(time.time()))
        headers = {"webhook-id": "msg_test_1", "webhook-timestamp": ts,
                   "webhook-signature": sign(SECRET, "msg_test_1", ts, body)}

        r = client.post("/webhooks/bird", content=body, headers=headers)
        check("статус 200", r.status_code == 200)
        check("тело ack", r.json() == {"ok": True})
        # BackgroundTasks выполняются внутри того же request-response цикла
        # тестового клиента, поэтому к этому моменту ответ уже ушёл клиенту.
        replies = [t for to, t in fake_bird.sent if to == "+77770000001"]
        check("клиенту отправлен ответ LLM", replies == ["ответ от LLM"])

        print("[2] повтор той же доставки (тот же webhook-id) -> deduplicated")
        r = client.post("/webhooks/bird", content=body, headers=headers)
        check("deduplicated в ответе", r.json().get("deduplicated") is True)
        replies = [t for to, t in fake_bird.sent if to == "+77770000001"]
        check("второй раз ответ не отправлялся", len(replies) == 1)

        print("[3] чужая подпись -> 401")
        r = client.post("/webhooks/bird", content=body,
                        headers={"webhook-id": "msg_x", "webhook-timestamp": ts,
                                 "webhook-signature": "v1,%%%bad%%%"})
        check("401", r.status_code == 401)

        print("[4] без подписи -> 401")
        r = client.post("/webhooks/bird", content=body)
        check("401 без заголовков", r.status_code == 401)

        print("[5] не-JSON с валидной подписью -> 200 (ретрай бессмыслен)")
        junk = b"not json"
        r = client.post("/webhooks/bird", content=junk,
                        headers={"webhook-id": "msg_bad", "webhook-timestamp": ts,
                                 "webhook-signature": sign(SECRET, "msg_bad", ts, junk)})
        check("200 на битый JSON", r.status_code == 200 and r.json() == {"ok": True})

        print("[6] чужое событие -> 200 без обработки")
        other = json.dumps({"type": "whatsapp.delivered", "data": {}}).encode()
        r = client.post("/webhooks/bird", content=other,
                        headers={"webhook-id": "msg_other", "webhook-timestamp": ts,
                                 "webhook-signature": sign(SECRET, "msg_other", ts, other)})
        check("200 на чужое событие", r.status_code == 200)

        print("[7] приветствие по слову start")
        body_start = json.dumps({
            "type": "whatsapp.received",
            "data": {"from": {"phone_number": "+77770000002"}, "text": {"body": "start"}},
        }).encode()
        r = client.post("/webhooks/bird", content=body_start,
                        headers={"webhook-id": "msg_start", "webhook-timestamp": ts,
                                 "webhook-signature": sign(SECRET, "msg_start", ts, body_start)})
        check("200", r.status_code == 200)
        greetings = [t for to, t in fake_bird.sent if to == "+77770000002"]
        check("приветствие отправлено", any("Здравствуйте" in t for t in greetings))

        print("[8] healthz")
        r = client.get("/healthz")
        check("healthz ok", r.status_code == 200 and r.json()["status"] == "ok")

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
