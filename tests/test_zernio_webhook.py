# Мультитенантный Zernio-вебхук: маршрутизация по accountId, ответ в диалог
# (conversationId), дедуп, проверка подписи, шаблон владельцу.
# Отправка идёт через фейковый sender — наружу ничего не уходит.
# Запуск: python tests/test_zernio_webhook.py

import hashlib
import hmac
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.WARNING)

import yaml
from fastapi.testclient import TestClient

from config.settings import LLMParams, Settings
from whatsapp.errors import MessagingError

SECRET = "whsec_zernio_test"
API_KEY = "sk_test_key"

ACC_A = "66b2e19d8c3f5a7e9d0b1c2d"
ACC_B = "77c3f20e9d4a6b8f0e1c2d3e"
ACC_UNKNOWN = "88d4a31f0e5b7c9a1f2d3e4f"

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


class CaptureSender:
    """Фейковый Zernio-клиент: собирает reply/шаблоны, наружу не ходит."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.replies: list[dict] = []
        self.templates: list[dict] = []

    async def send_text(self, to, text, conversation_id=""):
        self.replies.append({
            "account_id": self.settings.whatsapp_phone_number_id,
            "to": to,
            "text": text,
            "conversation_id": conversation_id,
        })

    async def send_template(self, to, template_name, language, params=None):
        self.templates.append({
            "to": to, "name": template_name, "language": language, "params": params or [],
        })

    async def close(self):
        pass


class StubLLM:
    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def chat(self, system_prompt, user_message, history=None):
        return self._reply


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())

    def has(self, fragment: str) -> bool:
        return any(fragment in m for m in self.records)


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def write_client(clients_dir: Path, slug: str, account_id: str, business: str) -> None:
    """Пишет yaml Zernio-клиента: slug-имя + zernio_account_id внутри."""
    (clients_dir / f"{slug}.yaml").write_text(yaml.safe_dump({
        "provider": "zernio",
        "zernio_account_id": account_id,
        "business_name": business,
        "tone": "вежливый",
        "language": "ru",
        "knowledge_base": f"Услуги ({business}).",
        "owner_whatsapp_phone": "+77000000099",
        "owner_template_name": "owner_alert",
        "owner_template_language": "ru",
        "fallback_triggers": ["жалоб"],
        "fallback_reply_ru": f"Передаю ваш вопрос команде {business}.",
        "llm": {"model": "", "temperature": 0.6, "max_tokens": 100,
                "timeout_seconds": 15},
    }, allow_unicode=True), encoding="utf-8")


def envelope(account_id: str, text: str, message_id: str, *,
             direction="incoming", event="message.received") -> bytes:
    return json.dumps({
        "id": f"evt-{message_id}",
        "event": event,
        "timestamp": "2027-01-04T14:00:04Z",
        "message": {
            "id": message_id,
            "conversationId": f"conv-{account_id}",
            "platform": "whatsapp",
            "platformMessageId": message_id,
            "direction": direction,
            "text": text,
            "attachments": [],
            "sender": {"id": "77770000001", "phoneNumber": "+77770000001",
                       "name": "Аня"},
            "sentAt": "2027-01-04T14:00:03Z",
            "isRead": False,
            "sentVia": None,
        },
        "conversation": {"id": f"conv-{account_id}"},
        "account": {"id": account_id, "accountId": account_id, "profileId": "p1",
                    "platform": "whatsapp"},
    }).encode()


def post(client, body: bytes, signature: str | None = None):
    return client.post("/webhooks/zernio", content=body, headers={
        "X-Zernio-Signature": signature if signature is not None else sign(body),
        "Content-Type": "application/json",
    })


def build_settings(clients_dir: str) -> Settings:
    return Settings(
        messaging_provider="zernio",
        whatsapp_access_token="",
        whatsapp_phone_number_id="",
        meta_app_secret="",
        meta_verify_token="",
        meta_graph_version="v21.0",
        zernio_api_key=API_KEY,
        zernio_webhook_secret=SECRET,
        zernio_base_url="https://zernio.com/api/v1",
        zernio_account_id="",
        app_host="127.0.0.1",
        app_port=8000,
        llm_api_url="https://llm.test/v1",
        llm_api_key="test",
        business_name="",
        tone="",
        language="ru",
        knowledge_base="",
        owner_phone=None,
        llm=LLMParams(model="global-model", temperature=0.6, max_tokens=100,
                      timeout_seconds=15, reasoning_effort=None),
        clients_dir=clients_dir,
    )


def main():
    from main import create_app

    tmp = Path(tempfile.mkdtemp(prefix="bot_zernio_"))
    import os
    os.environ["JAUAP_DB_PATH"] = str(tmp / "test.db")
    log = LogCapture()
    logging.getLogger().addHandler(log)
    logging.getLogger().setLevel(logging.WARNING)

    write_client(tmp, "nails", ACC_A, "Студия А")
    write_client(tmp, "coffee", ACC_B, "Кофейня Б")

    settings = build_settings(str(tmp))
    senders: dict[str, CaptureSender] = {}

    def factory(tenant_settings: Settings) -> CaptureSender:
        sender = CaptureSender(tenant_settings)
        senders[tenant_settings.whatsapp_phone_number_id] = sender
        return sender

    app = create_app(settings, sender_factory=factory)

    try:
        with TestClient(app) as client:
            state = app.state.state
            # LLM подменяем стабом: без сети, предсказуемый ответ.
            state.tenants[ACC_A].processor.llm = StubLLM("ответ от LLM")
            state.tenants[ACC_B].processor.llm = StubLLM("ответ от LLM")

            print("[1] slug-клиенты загружены, ключ = accountId")
            check("оба клиента в реестре", set(state.tenants) == {ACC_A, ACC_B})
            check("провайдер клиентов zernio",
                  state.tenants[ACC_A].settings.messaging_provider == "zernio")

            print("[2] событие клиенту A: ответ в его диалог")
            body = envelope(ACC_A, "Сколько стоит?", "wamid_a1")
            r = post(client, body)
            check("200 и ack", r.status_code == 200 and r.json() == {"ok": True})
            check("ответ ушёл", len(senders[ACC_A].replies) == 1)
            reply = senders[ACC_A].replies[0]
            check("свой accountId", reply["account_id"] == ACC_A)
            check("conversationId прокинут", reply["conversation_id"] == f"conv-{ACC_A}")
            check("ответ от LLM-стаба", reply["text"] == "ответ от LLM")

            print("[3] клиенту B — свой диалог")
            r = post(client, envelope(ACC_B, "Привет", "wamid_b1"))
            check("200", r.status_code == 200)
            check("ответ B в его диалог",
                  len(senders[ACC_B].replies) == 1
                  and senders[ACC_B].replies[0]["conversation_id"] == f"conv-{ACC_B}")
            check("ответов у A не прибавилось", len(senders[ACC_A].replies) == 1)

            print("[4] дедуп по id сообщения")
            before = len(senders[ACC_A].replies)
            r = post(client, envelope(ACC_A, "Сколько стоит?", "wamid_a1"))
            check("повтор -> 200", r.status_code == 200)
            check("ответ не задублирован", len(senders[ACC_A].replies) == before)

            print("[5] незарегистрированный accountId -> ack без обработки")
            r = post(client, envelope(ACC_UNKNOWN, "привет", "wamid_u1"))
            check("200", r.status_code == 200)
            check("warning о незарегистрированном аккаунте",
                  log.has("незарегистрированного аккаунта Zernio"))

            print("[6] подпись")
            bad = post(client, envelope(ACC_A, "текст", "wamid_sig"), signature="0" * 64)
            check("неверная подпись -> 401", bad.status_code == 401)
            check("нет заголовка -> 401",
                  client.post("/webhooks/zernio", content=envelope(ACC_A, "x", "wamid_s2"))
                  .status_code == 401)

            print("[7] не наше событие -> ack без отправки")
            before_total = len(senders[ACC_A].replies) + len(senders[ACC_B].replies)
            r = post(client, envelope(ACC_A, "", "wamid_evt",
                                      event="message.delivered"))
            check("200", r.status_code == 200)
            check("отправок не добавилось",
                  len(senders[ACC_A].replies) + len(senders[ACC_B].replies) == before_total)

            print("[8] fallback -> уведомление владельцу шаблоном")
            r = post(client, envelope(ACC_A, "жалоба на сервис", "wamid_fb1"))
            check("200", r.status_code == 200)
            check("клиенту ушёл fallback-ответ",
                  senders[ACC_A].replies[-1]["text"].startswith("Передаю ваш вопрос"))
            check("владельцу ушёл шаблон", len(senders[ACC_A].templates) == 1)
            tpl = senders[ACC_A].templates[0]
            check("имя шаблона", tpl["name"] == "owner_alert")
            check("язык шаблона", tpl["language"] == "ru")
            check("получатель — номер владельца", tpl["to"] == "+77000000099")
            check("две переменные тела", len(tpl["params"]) == 2)
            check("первая переменная — отправитель",
                  "Аня" in tpl["params"][0] and "+77770000001" in tpl["params"][0])

            print("[9] healthz")
            r = client.get("/healthz")
            check("healthz с числом клиентов",
                  r.status_code == 200 and r.json()["clients"] == len(state.tenants))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
