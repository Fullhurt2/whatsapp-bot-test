# Тесты админ-API и панели /admin: токены (админ/клиент), валидация перед
# записью, бэкапы .history, аудит .audit.jsonl, hot-reload после PUT/DELETE,
# права клиентского токена, отключение роутов без ADMIN_TOKEN.
# Отправка перехватывается MockTransport'ом настоящего MetaWhatsAppClient.
# Запуск: python tests/test_admin_api.py

import hashlib
import hmac
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.CRITICAL)

import httpx
import yaml
from fastapi.testclient import TestClient

from config.settings import LLMParams, Settings
from whatsapp.meta_client import MetaWhatsAppClient

# --- тестовые константы ---
VERIFY_TOKEN = "admin-verify"
APP_SECRET = "admin-app-secret"
GLOBAL_TOKEN = "global-fallback-token"
ADMIN_TOKEN = "unit-admin-token-0123456789abcdef"   # > 32 символов
MGMT_A = "mgmt-a-0123456789abcdef"

PID_A = "111111111111111"
PID_B = "222222222222222"
PID_NEW = "444444444444444"
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


class SentCapture:
    """Перехват отправок: url + Authorization + тело каждой отправки."""

    def __init__(self):
        self.items = []

    def make_sender(self, tenant_settings):
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


def write_client(tmp: Path, pid: str, business: str, *, token: str = "",
                 mgmt_token: str = "") -> None:
    (tmp / f"{pid}.yaml").write_text(yaml.safe_dump({
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
        "management_token": mgmt_token,
    }, allow_unicode=True), encoding="utf-8")


def build_settings(clients_dir: str, admin_token: str) -> Settings:
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
        admin_token=admin_token,
    )


def event_body(pid: str, text: str, message_id: str) -> bytes:
    return json.dumps({
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "field": "messages",
                "value": {
                    "metadata": {"phone_number_id": pid},
                    "contacts": [{"profile": {"name": "Аня"}, "wa_id": "77770000001"}],
                    "messages": [{"from": "77770000001", "id": message_id,
                                  "timestamp": "1609685060", "type": "text",
                                  "text": {"body": text}}],
                },
            }],
        }],
    }).encode()


def post_event(client, pid: str, text: str, message_id: str):
    body = event_body(pid, text, message_id)
    digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/webhooks/meta", content=body,
                       headers={"X-Hub-Signature-256": "sha256=" + digest})


def main():
    from main import create_app

    tmp = Path(tempfile.mkdtemp(prefix="bot_admin_"))

    write_client(tmp, PID_A, "Студия А", token="waba-token-A-0123456789abcdef",
                 mgmt_token=MGMT_A)
    write_client(tmp, PID_B, "Кофейня Б")  # без токенов

    settings = build_settings(str(tmp), ADMIN_TOKEN)
    capture = SentCapture()
    app = create_app(settings, sender_factory=capture.make_sender)

    try:
        with TestClient(app) as client:
            state = app.state.state
            admin_headers = {"X-Admin-Token": ADMIN_TOKEN}
            client_headers = {"X-Client-Token": MGMT_A}

            print("[1] авторизация админ-API")
            r = client.get("/admin/clients")
            check("без токена -> 401", r.status_code == 401)
            r = client.get("/admin/clients", headers={"X-Admin-Token": "wrong-token"})
            check("неверный токен -> 403", r.status_code == 403)
            r = client.get("/admin/clients", headers=admin_headers)
            body = r.json()
            check("админ видит список", r.status_code == 200
                  and {c["phone_number_id"] for c in body["clients"]} == {PID_A, PID_B})
            entry_a = next(c for c in body["clients"] if c["phone_number_id"] == PID_A)
            check("у A свой токен и management_token",
                  entry_a["has_own_token"] is True and entry_a["has_management_token"] is True)
            entry_b = next(c for c in body["clients"] if c["phone_number_id"] == PID_B)
            check("у B нет своего токена", entry_b_ := True)
            r = client.get("/admin/whoami", headers=admin_headers)
            check("whoami: admin", r.status_code == 200 and r.json()["role"] == "admin")
            r = client.get("/admin/whoami", headers=client_headers)
            check("whoami: client + свой pid",
                  r.json()["role"] == "client" and r.json()["phone_number_id"] == PID_A)

            print("[3] GET конфига: маскирование секретов")
            r = client.get(f"/admin/clients/{PID_A}", headers=admin_headers)
            text = r.text
            check("токены не раскрыты",
                  "waba-token-A-0123456789abcdef" not in text and MGMT_A not in text)
            check("маска присутствует", "…" in text)
            r = client.get(f"/admin/clients/{PID_B}", headers=client_headers)
            check("клиентский токен на чужой pid -> 403", r.status_code == 403)
            r = client.get(f"/admin/clients/{PID_UNKNOWN}",
                           headers={"X-Admin-Token": "wrong-token"})
            check("неизвестный токен -> 401/403", r.status_code in (401, 403))

            print("[4] PUT админом: обновление + бэкап + аудит + hot-reload")
            history_dir = tmp / ".history" / PID_A
            backup_before = len(list(history_dir.glob("*.yaml"))) if history_dir.exists() else 0
            r = client.put(f"/admin/clients/{PID_A}", headers=admin_headers,
                           json={"business_name": "Студия А Обновлённая"})
            check("PUT -> 200 и ok", r.status_code == 200 and r.json().get("ok") is True)
            saved = yaml.safe_load((tmp / f"{PID_A}.yaml").read_text(encoding="utf-8"))
            check("business_name обновлён",
                  saved["business_name"] == "Студия А Обновлённая")
            check("остальные поля не потерялись",
                  saved.get("access_token") == "waba-token-A-0123456789abcdef")
            backup_after = len(list(history_dir.glob("*.yaml"))) if history_dir.exists() else 0
            check("бэкап создан", backup_after == backup_before + 1)
            audit_lines = [json.loads(line)
                           for line in (tmp / ".audit.jsonl").read_text(encoding="utf-8").splitlines()]
            last = audit_lines[-1]
            check("аудит: actor/action/изменённое поле",
                  last["actor"] == "admin" and last["action"] == "put"
                  and last["phone_number_id"] == PID_A
                  and "business_name" in last["changed"])
            check("hot-reload применил конфиг",
                  state.tenants[PID_A].settings.business_name == "Студия А Обновлённая")

            print("[5] PUT битым конфигом -> 400, файл не тронут")
            r = client.put(f"/admin/clients/{PID_A}", headers=admin_headers,
                           json={"business_name": ""})
            check("400 со списком проблем",
                  r.status_code == 400 and r.json().get("problems"))
            saved = yaml.safe_load((tmp / f"{PID_A}.yaml").read_text(encoding="utf-8"))
            check("файл остался прежним", saved["business_name"] == "Студия А Обновлённая")

            print("[6] клиентский токен: свой конфиг, чужой — нет")
            r = client.get(f"/admin/clients/{PID_A}", headers=client_headers)
            check("чтение своего конфига", r.status_code == 200)
            r = client.get(f"/admin/clients/{PID_B}", headers=client_headers)
            check("чужой конфиг -> 403", r.status_code == 403)
            r = client.put(f"/admin/clients/{PID_A}", headers=client_headers,
                           json={"knowledge_base": "Обновлённая база от клиента."})
            check("клиент обновил базу знаний", r.status_code == 200)
            saved = yaml.safe_load((tmp / f"{PID_A}.yaml").read_text(encoding="utf-8"))
            check("правка клиента в файле", "клиента" in saved["knowledge_base"])
            audit_lines = [json.loads(line)
                           for line in (tmp / ".audit.jsonl").read_text(encoding="utf-8").splitlines()]
            check("аудит: actor=client:<pid>",
                  audit_lines[-1]["actor"] == f"client:{PID_A}")
            r = client.put(f"/admin/clients/{PID_A}", headers=client_headers,
                           json={"access_token": "другой-токен"})
            check("смена access_token клиентом -> 403", r.status_code == 403)
            r = client.put(f"/admin/clients/{PID_A}", headers=client_headers,
                           json={"management_token": "другой-токен"})
            check("смена management_token клиентом -> 403", r.status_code == 403)
            r = client.put(f"/admin/clients/{PID_A}", headers=client_headers,
                           json={"llm": {"model": "дорогая-модель"}})
            check("смена llm клиентом -> 403", r.status_code == 403)

            print("[8] замаскированный секрет сохраняет прежний токен")
            cfg_before = yaml.safe_load((tmp / f"{PID_A}.yaml").read_text(encoding="utf-8"))
            real_token = cfg_before["access_token"]
            mask = real_token[:4] + "…" + real_token[-4:]
            r = client.put(f"/admin/clients/{PID_A}", headers=admin_headers,
                           json={"business_name": "Студия А", "access_token": mask})
            check("PUT с маской -> 200", r.status_code == 200)
            saved = yaml.safe_load((tmp / f"{PID_A}.yaml").read_text(encoding="utf-8"))
            check("токен не затёрт", saved.get("access_token") == real_token)

            print("[9] DELETE отключает клиента")
            backup_b = len(list((tmp / ".history" / PID_B).glob("*.yaml")))
            r = client.delete(f"/admin/clients/{PID_B}", headers=client_headers)
            check("DELETE клиентским токеном -> 401/403", r.status_code in (401, 403))
            r = client.delete(f"/admin/clients/{PID_B}", headers=admin_headers)
            check("DELETE админом -> 200", r.status_code == 200)
            check("файл удалён", not (tmp / f"{PID_B}.yaml").exists())
            check("бэкап остался",
                  len(list((tmp / ".history" / PID_B).glob("*.yaml"))) == 1)
            check("клиент отключён", PID_B not in state.tenants)
            r = client.delete(f"/admin/clients/{PID_B}", headers=admin_headers)
            check("повторный DELETE -> 404", r.status_code == 404)

            print("[9] PUT создаёт клиента -> сразу работает вебхук")
            payload = {
                "business_name": "Кофейня Новая",
                "tone": "вежливый",
                "language": "ru",
                "knowledge_base": "Кофе 1000 ₸.",
                "owner_whatsapp_phone": "",
                "fallback_triggers": ["жалоб"],
                "fallback_reply_ru": "Передаю ваш вопрос команде Кофейня Новая.",
                "llm": {"model": "", "temperature": 0.6, "max_tokens": 100,
                        "timeout_seconds": 15},
                "access_token": "waba-token-C-0123456789abcdef",
                "management_token": "mgmt-C-0123456789abcdef",
            }
            r = client.put(f"/admin/clients/{PID_NEW}", headers=admin_headers, json=payload)
            check("PUT -> 200", r.status_code == 200)
            check("клиент появился в реестре", PID_NEW in state.tenants)
            check("токен из PUT", state.tenants[PID_NEW].settings.whatsapp_access_token == "waba-token-C-0123456789abcdef")
            body = event_body(PID_NEW, "жалоба", "wamid_c1")
            digest = hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
            r = client.post("/webhooks/meta", content=body,
                            headers={"X-Hub-Signature-256": "sha256=" + digest})
            sent_c = capture.sends_for(PID_NEW)
            check("событие маршрутизировано с токеном из PUT",
                  len(sent_c) == 1 and sent_c[0]["auth"] == "Bearer waba-token-C-0123456789abcdef"
                  and "Новая" in sent_c[0]["text"])

            print("[10] панель /admin отдаётся")
            r = client.get("/admin")
            check("200 и HTML", r.status_code == 200 and "<html" in r.text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
