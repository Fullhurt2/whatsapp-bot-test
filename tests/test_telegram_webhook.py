# Интеграция Telegram-вебхука в мультитенанте: приём update с проверкой секрета,
# маршрутизация к TG-клиенту, ответ и уведомление владельцу через Telegram,
# сосуществование с WhatsApp-клиентом, дедуп, hot-reload, админ-API для TG.
# Отправка идёт через настоящий TelegramClient с MockTransport — наружу ничего
# не уходит; LLM подменён стабом.
# Запуск: python tests/test_telegram_webhook.py

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

import httpx
import yaml
from fastapi.testclient import TestClient

from config.settings import LLMParams, Settings
from whatsapp.meta_client import MetaWhatsAppClient
from whatsapp.telegram_client import TelegramClient

# --- тестовые константы ---
VERIFY_TOKEN = "tg-verify"
APP_SECRET = "tg-app-secret"
GLOBAL_TOKEN = "global-token"
ADMIN_TOKEN = "a" * 40

WA_PID = "111111111111111"
BOT_ID = "123456789"
BOT_TOKEN = f"{BOT_ID}:{'a' * 30}"
TG_SECRET = "webhook-secret-value"

CLIENT_CHAT = "777"
OWNER_CHAT = "55555"
GROUP_CHAT = "-1001234567890"

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


class StubLLM:
    def __init__(self, reply):
        self._reply = reply

    async def chat(self, system_prompt, user_message, history=None):
        return self._reply


class Capture:
    """Перехват отправок: TG — TelegramClient, WA — MetaWhatsAppClient (MockTransport)."""

    def __init__(self):
        self.items = []

    def make_sender(self, tenant_settings: Settings):
        if tenant_settings.messaging_provider == "telegram":
            def handler(request: httpx.Request) -> httpx.Response:
                payload = json.loads(request.content)
                self.items.append({
                    "provider": "tg",
                    "token": tenant_settings.telegram_bot_token,
                    "chat_id": str(payload.get("chat_id") or ""),
                    "text": str(payload.get("text") or ""),
                })
                return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

            return TelegramClient(tenant_settings.telegram_bot_token,
                                  transport=httpx.MockTransport(handler))

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.items.append({
                "provider": "wa",
                "auth": request.headers.get("Authorization", ""),
                "pid": tenant_settings.whatsapp_phone_number_id,
                "to": str(payload.get("to") or ""),
                "text": str((payload.get("text") or {}).get("body") or ""),
            })
            return httpx.Response(200, json={"messages": [{"id": "x"}]})

        return MetaWhatsAppClient(
            tenant_settings.whatsapp_access_token,
            tenant_settings.whatsapp_phone_number_id,
            graph_version=tenant_settings.meta_graph_version,
            transport=httpx.MockTransport(handler),
        )

    def tg_sends(self, chat_id=None):
        return [s for s in self.items if s["provider"] == "tg"
                and (chat_id is None or s["chat_id"] == chat_id)]

    def wa_sends(self):
        return [s for s in self.items if s["provider"] == "wa"]


class LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record.getMessage())

    def has(self, fragment):
        return any(fragment in message for message in self.records)


def write_wa_client(clients_dir: Path, pid: str, business: str, token: str) -> None:
    (clients_dir / f"{pid}.yaml").write_text(yaml.safe_dump({
        "provider": "wa",
        "business_name": business,
        "tone": "вежливый",
        "language": "ru",
        "knowledge_base": f"Услуги: маникюр ({business}).",
        "fallback_triggers": ["жалоб"],
        "llm": {"model": "", "temperature": 0.6, "max_tokens": 100, "timeout_seconds": 15},
        "access_token": token,
    }, allow_unicode=True), encoding="utf-8")


def write_tg_client(clients_dir: Path, bot_id: str, business: str, *,
                    token: str, secret: str) -> None:
    (clients_dir / f"{bot_id}.yaml").write_text(yaml.safe_dump({
        "provider": "tg",
        "business_name": business,
        "tone": "вежливый",
        "language": "ru",
        "knowledge_base": f"Услуги: маникюр ({business}).",
        "owner_telegram_chat_id": OWNER_CHAT,
        "fallback_triggers": ["жалоб"],
        "fallback_reply_ru": f"Передаю ваш вопрос команде {business}.",
        "telegram_bot_token": token,
        "telegram_webhook_secret": secret,
        "llm": {"model": "", "temperature": 0.6, "max_tokens": 100, "timeout_seconds": 15},
    }, allow_unicode=True), encoding="utf-8")


def build_settings(clients_dir: str) -> Settings:
    return Settings(
        messaging_provider="meta",
        whatsapp_access_token=GLOBAL_TOKEN,
        whatsapp_phone_number_id="",
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
        business_name="",
        tone="",
        language="ru",
        knowledge_base="",
        owner_phone=None,
        llm=LLMParams(model="global-model", temperature=0.6, max_tokens=100,
                      timeout_seconds=15, reasoning_effort=None),
        clients_dir=clients_dir,
        admin_token=ADMIN_TOKEN,
        public_base_url="",
    )


def tg_update(update_id, text, chat_id=CLIENT_CHAT, name="Аня"):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": int(chat_id), "is_bot": False, "first_name": name},
            "chat": {"id": int(chat_id), "type": "private", "first_name": name},
            "date": 1700000000,
            "text": text,
        },
    }


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


def post_tg(client, payload, secret=TG_SECRET, bot_id=BOT_ID):
    return client.post(f"/webhooks/telegram/{bot_id}", json=payload,
                       headers={"X-Telegram-Bot-Api-Secret-Token": secret})


def post_wa(client, text, message_id):
    body = json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "metadata": {"phone_number_id": WA_PID},
            "contacts": [{"profile": {"name": "Бек"}, "wa_id": "77770000002"}],
            "messages": [{"from": "77770000002", "id": message_id, "timestamp": "1",
                          "type": "text", "text": {"body": text}}],
        }}]}],
    }).encode()
    return client.post("/webhooks/meta", content=body,
                       headers={"X-Hub-Signature-256": sign(body)})


def main():
    from main import create_app

    tmp = Path(tempfile.mkdtemp(prefix="bot_tg_clients_"))
    log = LogCapture()
    logging.getLogger().addHandler(log)

    write_wa_client(tmp, WA_PID, "Студия WA", "token-A")
    write_tg_client(tmp, BOT_ID, "Кофейня TG", token=BOT_TOKEN, secret=TG_SECRET)

    settings = build_settings(str(tmp))
    capture = Capture()
    app = create_app(settings, sender_factory=capture.make_sender)

    try:
        with TestClient(app) as client:
            state = app.state.state

            print("[1] оба клиента в реестре, провайдеры разные")
            check("оба тенанта загружены", set(state.tenants) == {WA_PID, BOT_ID})
            check("WA — meta", state.tenants[WA_PID].settings.messaging_provider == "meta")
            check("TG — telegram",
                  state.tenants[BOT_ID].settings.messaging_provider == "telegram")

            print("[2] TG-событие с ключевым словом: ответ клиенту и уведомление владельцу")
            r = post_tg(client, tg_update(1, "жалоба"))
            check("200 и ack", r.status_code == 200 and r.json() == {"ok": True})
            client_sends = capture.tg_sends(CLIENT_CHAT)
            owner_sends = capture.tg_sends(OWNER_CHAT)
            check("клиенту ушёл fallback-текст его бизнеса",
                  len(client_sends) == 1 and "Кофейня TG" in client_sends[0]["text"])
            check("владельцу ушло уведомление в его chat id",
                  len(owner_sends) == 1 and "Вопрос вне базы знаний" in owner_sends[0]["text"])
            check("отправляли токеном этого бота",
                  client_sends[0]["token"] == BOT_TOKEN)

            print("[3] неверный секрет вебхука -> 401 без обработки")
            before = len(capture.items)
            r = post_tg(client, tg_update(2, "ещё жалоба"), secret="wrong-secret")
            check("401", r.status_code == 401)
            check("отправок нет", len(capture.items) == before)

            print("[4] неизвестный бот -> 404")
            r = post_tg(client, tg_update(3, "привет"), bot_id="999999999")
            check("404", r.status_code == 404)

            print("[5] дедуп по update_id")
            r = post_tg(client, tg_update(4, "жалоба"))
            count = len(capture.tg_sends(CLIENT_CHAT))
            r = post_tg(client, tg_update(4, "жалоба"))
            check("повтор update_id -> 200", r.status_code == 200)
            check("ответ не задублирован",
                  len(capture.tg_sends(CLIENT_CHAT)) == count)

            print("[6] /start -> приветствие в тот же чат")
            r = post_tg(client, tg_update(5, "/start"))
            sends = capture.tg_sends(CLIENT_CHAT)
            check("200", r.status_code == 200)
            check("приветствие отправлено",
                  "помощник" in sends[-1]["text"].lower())

            print("[7] LLM-ветка TG-клиента")
            state.tenants[BOT_ID].processor.llm = StubLLM("Ответ в Telegram")
            r = post_tg(client, tg_update(6, "сколько стоит маникюр"))
            sends = capture.tg_sends(CLIENT_CHAT)
            check("ответ LLM отправлен", sends[-1]["text"] == "Ответ в Telegram")

            print("[8] WhatsApp-клиент продолжает работать рядом")
            r = post_wa(client, "жалоба", "wamid_wa1")
            wa = capture.wa_sends()
            check("200", r.status_code == 200)
            check("WA-отправка своим токеном и номером",
                  len(wa) == 1 and wa[0]["auth"] == "Bearer token-A"
                  and wa[0]["pid"] == WA_PID and "Студия WA" in wa[0]["text"])

            print("[9] админ-список знает провайдера")
            r = client.get("/admin/clients", headers={"X-Admin-Token": ADMIN_TOKEN})
            by_pid = {c["phone_number_id"]: c for c in r.json()["clients"]}
            check("TG-клиент помечен provider=tg", by_pid[BOT_ID]["provider"] == "tg")
            check("WA-клиент помечен provider=wa", by_pid[WA_PID]["provider"] == "wa")

            print("[10] создание TG-клиента через админ-API")
            new_bot = "987654321"
            r = client.put(f"/admin/clients/{new_bot}", headers={"X-Admin-Token": ADMIN_TOKEN},
                           json={
                               "provider": "tg",
                               "business_name": "Новый TG",
                               "knowledge_base": "Услуги: стрижка.",
                               "telegram_bot_token": f"{new_bot}:{'b' * 30}",
                           })
            check("200", r.status_code == 200)
            saved = yaml.safe_load((tmp / f"{new_bot}.yaml").read_text(encoding="utf-8"))
            check("provider сохранён", saved.get("provider") == "tg")
            check("секрет вебхука сгенерирован",
                  bool(str(saved.get("telegram_webhook_secret") or "").strip()))
            check("предупреждение про PUBLIC_BASE_URL",
                  any("PUBLIC_BASE_URL" in w for w in (r.json().get("warnings") or [])))

            print("[11] профиль недоступен для TG-клиента")
            r = client.get(f"/admin/clients/{BOT_ID}/profile",
                           headers={"X-Admin-Token": ADMIN_TOKEN})
            check("409 для TG", r.status_code == 409)

            print("[12] неверный токен бота при создании -> 400")
            r = client.put("/admin/clients/555000111", headers={"X-Admin-Token": ADMIN_TOKEN},
                           json={"provider": "tg", "business_name": "X",
                                 "knowledge_base": "Y",
                                 "telegram_bot_token": "555000111:короткий"})
            check("400 (токен не похож на бота)", r.status_code == 400)
            check("проблема про формат токена",
                  any("не похож на токен бота" in p for p in r.json().get("problems", [])))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
