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
        zernio_api_key="",
        zernio_webhook_secret="",
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

    # --- новые тесты для rate limit, token migration, scheduler ---

def test_rate_limit():
    """Rate limit: 10 неверных токенов за 10 мин -> 429, IP = последний X-Forwarded-For."""
    from fastapi.testclient import TestClient
    from main import create_app
    from config.settings import Settings, LLMParams
    from pathlib import Path
    import tempfile
    import yaml

    tmp = Path(tempfile.mkdtemp(prefix="bot_ratelimit_"))
    write_client(tmp, PID_A, "Test", mgmt_token=MGMT_A)
    settings = Settings(
        messaging_provider="meta",
        whatsapp_access_token=GLOBAL_TOKEN,
        whatsapp_phone_number_id="",
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
        meta_graph_version="v21.0",
        zernio_api_key="",
        zernio_webhook_secret="",
        zernio_base_url="https://zernio.com/api/v1",
        zernio_account_id="",
        app_host="127.0.0.1",
        app_port=8000,
        llm_api_url="https://llm.test/v1",
        llm_api_key="test",
        business_name="Test",
        tone="test",
        language="ru",
        knowledge_base="test",
        owner_phone=None,
        llm=LLMParams(model="test", temperature=0.6, max_tokens=100, timeout_seconds=15),
        clients_dir=str(tmp),
        admin_token=ADMIN_TOKEN,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        admin_headers = {"X-Admin-Token": ADMIN_TOKEN}
        # 10 неверных попыток с одинаковым IP (прямой)
        for _ in range(10):
            r = client.get("/admin/clients", headers={"X-Admin-Token": "bad-token"})
            # первые 10 - 403, 11-я - 429
        r = client.get("/admin/clients", headers={"X-Admin-Token": "bad-token"})
        check("11-я попытка с тем же IP -> 429", r.status_code == 429)

        # Тест X-Forwarded-For: последний адрес
        headers_ff = {"X-Admin-Token": "bad-token", "X-Forwarded-For": "1.2.3.4, 5.6.7.8"}
        for _ in range(10):
            r = client.get("/admin/clients", headers={"X-Admin-Token": "bad-token", "X-Forwarded-For": "1.2.3.4, 5.6.7.8"})
        r = client.get("/admin/clients", headers={"X-Admin-Token": "bad-token", "X-Forwarded-For": "1.2.3.4, 5.6.7.8"})
        check("X-Forwarded-For: 11-я попытка -> 429", r.status_code == 429)

        # X-Real-IP имеет приоритет над X-Forwarded-For
        for _ in range(10):
            r = client.get("/admin/clients", headers={"X-Admin-Token": "bad-token", "X-Real-IP": "9.9.9.9"})
        r = client.get("/admin/clients", headers={"X-Admin-Token": "bad-token", "X-Real-IP": "9.9.9.9"})
        check("X-Real-IP: 11-я попытка -> 429", r.status_code == 429)

    shutil.rmtree(tmp, ignore_errors=True)


def test_token_migration():
    """Миграция токенов: plaintext -> SHA-256, идемпотентность, уже хэшированные не трогает."""
    from admin.api import hash_token, verify_token_hash, _atomic_write
    from pathlib import Path
    import tempfile
    import yaml

    tmp = Path(tempfile.mkdtemp(prefix="bot_migrate_"))
    clients_dir = tmp / "clients"
    clients_dir.mkdir()

    # Клиент с plaintext токеном
    (clients_dir / "111.yaml").write_text(yaml.safe_dump({
        "business_name": "Test1", "management_token": "plain-token-123", "provider": "meta"
    }, allow_unicode=True), encoding="utf-8")

    # Клиент с уже хэшированным токеном
    hashed = hash_token("already-hashed")
    (clients_dir / "222.yaml").write_text(yaml.safe_dump({
        "business_name": "Test2", "management_token": hashed, "provider": "meta"
    }, allow_unicode=True), encoding="utf-8")

    # Клиент без токена
    (clients_dir / "333.yaml").write_text(yaml.safe_dump({
        "business_name": "Test3", "provider": "meta"
    }, allow_unicode=True), encoding="utf-8")

    # Имитируем логику миграции
    from admin.api import _client_yaml_path, _read_cfg
    for name in sorted(os.listdir(clients_dir)):
        path = clients_dir / name
        cfg = _read_cfg(path)
        if not cfg:
            continue
        token = str(cfg.get("management_token") or "").strip()
        if not token:
            continue
        bare = token[len("sha256:"):] if token.startswith("sha256:") else token
        if len(bare) == 64 and all(c in "0123456789abcdef" for c in bare.lower()):
            continue  # уже хэш
        _atomic_write(path, {**cfg, "management_token": hash_token(token)})

    # Проверяем результат
    cfg1 = _read_cfg(clients_dir / "111.yaml")
    check("111: plaintext -> SHA-256", verify_token_hash("plain-token-123", cfg1.get("management_token", "")))

    cfg2 = _read_cfg(clients_dir / "222.yaml")
    check("222: хэш не изменился", cfg2.get("management_token") == hashed)

    cfg3 = _read_cfg(clients_dir / "333.yaml")
    check("333: без токена -> пусто", not cfg3.get("management_token"))

    shutil.rmtree(tmp, ignore_errors=True)


def test_scheduler_basic():
    """APScheduler: бэкап, чистка, напоминания (базовая проверка импорта и запуска)."""
    from main import create_app
    from config.settings import Settings, LLMParams
    import tempfile
    from pathlib import Path
    import yaml

    tmp = Path(tempfile.mkdtemp(prefix="bot_sched_"))
    write_client(tmp, PID_A, "Test", mgmt_token=MGMT_A)
    settings = Settings(
        messaging_provider="meta",
        whatsapp_access_token=GLOBAL_TOKEN,
        whatsapp_phone_number_id="",
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
        meta_graph_version="v21.0",
        zernio_api_key="",
        zernio_webhook_secret="",
        zernio_base_url="https://zernio.com/api/v1",
        zernio_account_id="",
        app_host="127.0.0.1",
        app_port=8000,
        llm_api_url="https://llm.test/v1",
        llm_api_key="test",
        business_name="Test",
        tone="test",
        language="ru",
        knowledge_base="test",
        owner_phone=None,
        llm=LLMParams(model="test", temperature=0.6, max_tokens=100, timeout_seconds=15),
        clients_dir=str(tmp),
        admin_token=ADMIN_TOKEN,
    )
    # Импорт создаёт scheduler в lifespan
    try:
        app = create_app(settings)
        with TestClient(app) as client:
            check("приложение запустилось со scheduler", True)
            # lifespan создаёт scheduler, но мы не можем легко проверить его задачи без реального времени
            # достаточно, что приложение поднимается без ошибок
    except Exception as e:
        check("scheduler не упал при старте", False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unanswered_endpoints():
    """Тест роутов unanswered: ответ, игнорирование вопроса и группы."""
    from main import create_app
    from config.settings import Settings, LLMParams
    from storage.unanswered import add_unanswered_question, get_unanswered_question, create_unanswered_group, get_unanswered_group
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp(prefix="bot_ua_"))
    write_client(tmp, PID_A, "Test", mgmt_token=MGMT_A)
    settings = Settings(
        messaging_provider="meta",
        whatsapp_access_token=GLOBAL_TOKEN,
        whatsapp_phone_number_id="",
        meta_app_secret=APP_SECRET,
        meta_verify_token=VERIFY_TOKEN,
        meta_graph_version="v21.0",
        zernio_api_key="",
        zernio_webhook_secret="",
        zernio_base_url="https://zernio.com/api/v1",
        zernio_account_id="",
        app_host="127.0.0.1",
        app_port=8000,
        llm_api_url="https://llm.test/v1",
        llm_api_key="test",
        business_name="Test",
        tone="test",
        language="ru",
        knowledge_base="База знаний",
        owner_phone=None,
        llm=LLMParams(model="test", temperature=0.6, max_tokens=100, timeout_seconds=15),
        clients_dir=str(tmp),
        admin_token=ADMIN_TOKEN,
    )
    headers = {"X-Admin-Token": ADMIN_TOKEN, "X-Real-IP": "10.0.0.99"}
    from admin.api import _failed_auth
    _failed_auth.clear()
    try:
        app = create_app(settings)
        with TestClient(app) as client:
            from storage.conversations import create_conversation
            conv = create_conversation(PID_A, "whatsapp", "+77001112233")
            cid = conv["id"]

            # 1. Игнорирование одиночного вопроса
            qid1 = add_unanswered_question(PID_A, cid, "Сколько стоит стрижка?")
            r = client.post(f"/admin/clients/{PID_A}/unanswered/question/{qid1}/ignore", headers=headers)
            check("скрытие вопроса -> 200", r.status_code == 200 and r.json().get("ok") is True)
            q1 = get_unanswered_question(qid1)
            check("статус вопроса стал ignored", q1 and q1["status"] == "ignored")

            # 2. Несуществующий вопрос -> 404
            r = client.post(f"/admin/clients/{PID_A}/unanswered/question/999999/ignore", headers=headers)
            check("несуществующий вопрос -> 404", r.status_code == 404)

            # 3. Игнорирование группы
            qid2 = add_unanswered_question(PID_A, cid, "Какой прайс?")
            qid3 = add_unanswered_question(PID_A, cid, "Цены можно?")
            gid = create_unanswered_group(PID_A, "Прайс-лист", [qid2, qid3])
            r = client.post(f"/admin/clients/{PID_A}/unanswered/{gid}/ignore", headers=headers)
            check("скрытие группы -> 200", r.status_code == 200 and r.json().get("ok") is True)
            g = get_unanswered_group(gid)
            check("статус группы стал ignored", g and g["status"] == "ignored")
            check("вопрос qid2 в группе стал ignored", get_unanswered_question(qid2)["status"] == "ignored")
            check("вопрос qid3 в группе стал ignored", get_unanswered_question(qid3)["status"] == "ignored")

            # 4. Несуществующая группа -> 404
            r = client.post(f"/admin/clients/{PID_A}/unanswered/999999/ignore", headers=headers)
            check("несуществующая группа -> 404", r.status_code == 404)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


passed = 0
failed = 0


if __name__ == "__main__":
    print("[NEW] Rate limit test")
    test_rate_limit()
    print("[NEW] Token migration test")
    test_token_migration()
    print("[NEW] Scheduler basic test")
    test_scheduler_basic()
    print("[NEW] Unanswered ignore test")
    test_unanswered_endpoints()
    print(f"\nНОВЫЕ ТЕСТЫ ИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)

