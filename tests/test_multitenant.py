# Мультитенантный режим: маршрутизация по phone_number_id, свой конфиг и свой
# токен у каждого клиента, hot-reload реестра, неизвестные номера, дедуп.
# Отправка идёт через настоящий MetaWhatsAppClient с MockTransport — фиксируем
# URL и Authorization, наружу ничего не уходит; LLM подменён стабом.
# Запуск: python tests/test_multitenant.py

import hashlib
import hmac
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.WARNING)

import httpx
import yaml
from fastapi.testclient import TestClient

from config.settings import LLMParams, Settings
from whatsapp.meta_client import MetaWhatsAppClient

# --- тестовые константы ---
VERIFY_TOKEN = "multi-verify"
APP_SECRET = "multi-app-secret"
GLOBAL_TOKEN = "global-fallback-token"

PID_A = "111111111111111"
PID_B = "222222222222222"
PID_C = "333333333333333"
PID_D = "444444444444444"
PID_UNKNOWN = "999999999999999"

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


def sign(body: bytes) -> str:
    """Подпись так же, как её делает Meta: sha256=<hex(hmac-sha256)>."""
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


class StubLLM:
    """Стаб LLM: возвращает заранее заданный ответ."""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def chat(self, system_prompt, user_message, history=None):
        return self._reply


class SentCapture:
    """Перехват отправок через настоящий MetaWhatsAppClient + MockTransport.

    Запоминает url, Authorization и тело каждой отправки — видно, какой
    токен и чей phone_number_id использованы.
    """

    def __init__(self):
        self.items: list[dict] = []

    def make_sender(self, tenant_settings: Settings):
        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.items.append({
                "url": str(request.url),
                "auth": request.headers.get("Authorization", ""),
                "to": str(payload.get("to") or ""),
                "text": str((payload.get("text") or {}).get("body") or ""),
            })
            return httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})

        return MetaWhatsAppClient(
            tenant_settings.whatsapp_access_token,
            tenant_settings.whatsapp_phone_number_id,
            graph_version=tenant_settings.meta_graph_version,
            transport=httpx.MockTransport(handler),
        )

    def sends_for(self, phone_number_id: str) -> list[dict]:
        return [s for s in self.items if f"/{phone_number_id}/messages" in s["url"]]

    def total(self) -> int:
        return len(self.items)


class LogCapture(logging.Handler):
    """Собирает записи лога для проверки предупреждений."""

    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())

    def has(self, fragment: str) -> bool:
        return any(fragment in message for message in self.records)


def write_client(clients_dir: Path, pid: str, business: str, *, token: str = "") -> None:
    """Пишет yaml клиента (access_token пустой -> глобальный fallback)."""
    (clients_dir / f"{pid}.yaml").write_text(yaml.safe_dump({
        "business_name": business,
        "tone": "вежливый",
        "language": "ru",
        "knowledge_base": f"Услуги: маникюр 3000 ₸ ({business}).",
        "owner_whatsapp_phone": "",
        "fallback_triggers": ["жалоб"],
        "fallback_reply_ru": f"Передаю ваш вопрос команде {business}.",
        "llm": {"model": "", "temperature": 0.6, "max_tokens": 100,
                "timeout_seconds": 15},
        "access_token": token,
    }, allow_unicode=True), encoding="utf-8")


def bump_mtimes(clients_dir: Path) -> None:
    """Явно двигает mtime всех yaml, чтобы снапшот реестра точно изменился."""
    stamp = time.time() + 10
    for path in clients_dir.iterdir():
        os.utime(path, (stamp, stamp))


def event_body(phone_number_id: str, text: str, message_id: str) -> bytes:
    """Payload вебхука Meta для клиента с данным phone_number_id."""
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": phone_number_id},
                    "contacts": [{"profile": {"name": "Аня"}, "wa_id": "77770000001"}],
                    "messages": [{"from": "77770000001", "id": message_id,
                                  "timestamp": "1609685060", "type": "text",
                                  "text": {"body": text}}],
                },
            }],
        }],
    }).encode()


def post_event(client, phone_number_id: str, text: str, message_id: str):
    body = event_body(phone_number_id, text, message_id)
    return client.post("/webhooks/meta", content=body,
                       headers={"X-Hub-Signature-256": sign(body)})


def build_global_settings(clients_dir: str) -> Settings:
    """Глобальные настройки мультитенанта (то, что остаётся в env)."""
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
    )


def main():
    from main import create_app

    tmp = Path(tempfile.mkdtemp(prefix="bot_clients_"))
    log = LogCapture()
    logging.getLogger().addHandler(log)
    logging.getLogger().setLevel(logging.WARNING)

    write_client(tmp, PID_A, "Студия А", token="token-A")
    write_client(tmp, PID_B, "Кофейня Б")  # без токена -> глобальный fallback

    settings = build_global_settings(str(tmp))
    capture = SentCapture()
    app = create_app(settings, sender_factory=capture.make_sender)

    try:
        with TestClient(app) as client:
            state = app.state.state

            print("[1] оба клиента загружены, процессоры разные")
            check("оба клиента в реестре", set(state.tenants) == {PID_A, PID_B})
            proc_a = state.tenants[PID_A].processor
            proc_b = state.tenants[PID_B].processor
            check("процессоры — разные экземпляры", proc_a is not proc_b)
            check("system prompt'ы разные", proc_a.system_prompt != proc_b.system_prompt)
            check("токен A из своего yaml",
                  state.tenants[PID_A].settings.whatsapp_access_token == "token-A")
            check("токен B — глобальный fallback",
                  state.tenants[PID_B].settings.whatsapp_access_token == GLOBAL_TOKEN)

            print("[2] событие клиенту A: ответ из его конфига и его токеном")
            r = post_event(client, PID_A, "жалоба", "wamid_a1")
            check("200 и ack", r.status_code == 200 and r.json() == {"ok": True})
            sent_a = capture.sends_for(PID_A)
            check("отправка на endpoint клиента A",
                  len(sent_a) == 1 and f"/{PID_A}/messages" in sent_a[0]["url"])
            check("токен клиента A в Authorization",
                  sent_a[0]["auth"] == "Bearer token-A")
            check("текст из конфига клиента A", "Студия А" in sent_a[0]["text"])

            print("[3] событие клиенту B: глобальный токен как fallback")
            r = post_event(client, PID_B, "жалоба", "wamid_b1")
            sent_b = capture.sends_for(PID_B)
            check("200", r.status_code == 200)
            check("отправка с глобальным токеном",
                  len(sent_b) == 1 and sent_b[0]["auth"] == f"Bearer {GLOBAL_TOKEN}")
            check("текст из конфига B", "Кофейня Б" in sent_b[0]["text"])

            print("[4] LLM-ветка: у каждого клиента свой system prompt")
            state.tenants[PID_A].processor.llm = StubLLM("ответ Студии А")
            r = post_event(client, PID_A, "сколько стоит маникюр", "wamid_a2")
            check("200", r.status_code == 200)
            sent_a = capture.sends_for(PID_A)
            check("ответ из LLM-стаба A отправлен его токеном",
                  len(sent_a) == 2 and sent_a[-1]["text"] == "ответ Студии А"
                  and sent_a[-1]["auth"] == "Bearer token-A")

            print("[5] неизвестный phone_number_id -> ack без обработки")
            before = capture.total()
            r = post_event(client, PID_UNKNOWN, "привет", "wamid_u1")
            check("200", r.status_code == 200)
            check("отправок нет", capture.total() == before)
            check("warning о незарегистрированном номере",
                  log.has("незарегистрированного номера"))

            print("[6] дедуп wamid в мультитенанте")
            r = post_event(client, PID_A, "ещё вопрос", "wamid_a3")
            count_a = len(capture.sends_for(PID_A))
            r = post_event(client, PID_A, "ещё вопрос", "wamid_a3")
            check("повтор wamid -> 200", r.status_code == 200)
            check("ответ не задублирован", len(capture.sends_for(PID_A)) == count_a)

            print("[7] hot-reload: новый клиент без рестарта")
            write_client(tmp, PID_C, "Пекарня Ц", token="token-C")
            bump_mtimes(tmp)
            r = post_event(client, PID_C, "жалоба", "wamid_c1")
            sent_c = capture.sends_for(PID_C)
            check("200", r.status_code == 200)
            check("третий клиент обслужен своим токеном и конфигом",
                  len(sent_c) == 1 and sent_c[0]["auth"] == "Bearer token-C"
                  and "Пекарня Ц" in sent_c[0]["text"])

            print("[8] hot-reload: изменённый yaml клиента")
            old_processor = state.tenants[PID_A].processor
            write_client(tmp, PID_A, "Студия А Новая", token="token-A2")
            bump_mtimes(tmp)
            r = post_event(client, PID_A, "жалоба", "wamid_a4")
            sent_a = capture.sends_for(PID_A)
            check("новый конфиг применён (fallback-текст)",
                  "Студия А Новая" in sent_a[-1]["text"])
            check("процессор пересоздан",
                  state.tenants[PID_A].processor is not old_processor)

            print("[9] hot-reload: удаление клиента")
            sent_b_before = len(capture.sends_for(PID_B))
            (tmp / f"{PID_B}.yaml").unlink()
            bump_mtimes(tmp)
            r = post_event(client, PID_B, "жалоба", "wamid_b3")
            check("200 после удаления", r.status_code == 200)
            check("клиент B отключён", PID_B not in state.tenants)
            check("отправок для B больше нет",
                  len(capture.sends_for(PID_B)) == sent_b_before)

            print("[10] битый yaml не роняет сервис")
            (tmp / f"{PID_D}.yaml").write_text(": : [[[[ это не yaml", encoding="utf-8")
            bump_mtimes(tmp)
            r = post_event(client, PID_A, "жив ли сервис", "wamid_a5")
            check("200 на валидное событие", r.status_code == 200)
            check("битый клиент не в реестре", PID_D not in state.tenants)
            check("остальные клиенты живы", PID_A in state.tenants)

            print("[11] healthz и /privacy в мультитенанте")
            r = client.get("/healthz")
            check("healthz с числом клиентов",
                  r.status_code == 200 and r.json()["status"] == "ok"
                  and r.json()["clients"] == len(state.tenants))
            r = client.get("/privacy")
            check("privacy 200", r.status_code == 200)

            print("[12] GET-верификация подписки (общий verify token)")
            r = client.get("/webhooks/meta", params={
                "hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN,
                "hub.challenge": "123"})
            check("challenge возвращён", r.status_code == 200 and r.text == "123")
            r = client.get("/webhooks/meta", params={
                "hub.mode": "subscribe", "hub.verify_token": "чужой",
                "hub.challenge": "1"})
            check("неверный verify_token -> 403", r.status_code == 403)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
