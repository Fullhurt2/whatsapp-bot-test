# Подключение Zernio из панели: генерация ссылки Embedded Signup, авто-сохранение
# accountId и регистрация вебхука. Запросы к Zernio перехватываются MockTransport.
# Запуск: python tests/test_zernio_admin_connect.py

import json
import logging
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
from whatsapp.zernio_client import ZernioApiClient

API_KEY = "sk_test_key"
SECRET = "whsec_test"
BASE = "https://zernio.com/api/v1"
ACCOUNT_A = "66b2e19d8c3f5a7e9d0b1c2d"
PROFILE_A = "66a1f0c2a4b9d3e8f1a2b3c4"
SLUG_A = "nails-studio"
SLUG_TG = "123456789"
ADMIN_TOKEN = "unit-admin-token-0123456789abcdef"
PUBLIC = "https://bot.example.com"

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


class ZernioApiCapture:
    """Клиент Zernio API-уровня с MockTransport + управлением ответами."""

    def __init__(self):
        self.calls: list[dict] = []
        self.accounts: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(request.content)
        except ValueError:
            body = None
        self.calls.append({
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query or b""),
            "json": body,
            "auth": request.headers.get("Authorization", ""),
        })
        if request.url.path == "/api/v1/profiles":
            return httpx.Response(201, json={"message": "ok", "profile": {
                "_id": PROFILE_A, "name": (body or {}).get("name", "")}})
        if request.url.path == "/api/v1/accounts":
            return httpx.Response(200, json={"accounts": self.accounts})
        if request.url.path == "/api/v1/connect/whatsapp":
            return httpx.Response(200, json={"authUrl": "https://zernio.com/signup/abc",
                                             "state": "s"})
        if request.url.path == "/api/v1/webhooks/settings":
            return httpx.Response(201, json={"success": True,
                                             "webhook": {"_id": "w1"}})
        return httpx.Response(404, json={"error": "not found"})

    def factory(self, settings):
        return ZernioApiClient(API_KEY, base_url=BASE,
                               transport=httpx.MockTransport(self.handler))

    def calls_to(self, suffix: str) -> list[dict]:
        return [c for c in self.calls if c["path"].endswith(suffix)]


def write_client(tmp: Path, slug: str, *, provider="zernio", account_id="",
                 profile_id="") -> None:
    cfg = {
        "provider": provider,
        "business_name": "Студия А" if provider == "zernio" else "TG",
        "tone": "вежливый", "language": "ru", "knowledge_base": "тест",
        "owner_whatsapp_phone": "", "fallback_triggers": [],
        "llm": {"model": "", "temperature": 0.6, "max_tokens": 100, "timeout_seconds": 15},
        "management_token": "",
    }
    if provider == "zernio":
        cfg["zernio_account_id"] = account_id
        cfg["zernio_profile_id"] = profile_id
    else:
        cfg["telegram_bot_token"] = f"{slug}:AAHh-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    (tmp / f"{slug}.yaml").write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")


def read_cfg(tmp: Path, slug: str) -> dict:
    return yaml.safe_load((tmp / f"{slug}.yaml").read_text(encoding="utf-8"))


def build_settings(clients_dir: str, *, webhook_secret=SECRET, public=PUBLIC) -> Settings:
    return Settings(
        messaging_provider="zernio",
        whatsapp_access_token="", whatsapp_phone_number_id="",
        meta_app_secret="", meta_verify_token="", meta_graph_version="v21.0",
        zernio_api_key=API_KEY, zernio_webhook_secret=webhook_secret,
        zernio_base_url=BASE, zernio_account_id="",
        app_host="127.0.0.1", app_port=8000,
        llm_api_url="https://llm.test/v1", llm_api_key="test",
        business_name="", tone="", language="ru", knowledge_base="", owner_phone=None,
        llm=LLMParams("m", 0.6, 100, 15, None),
        clients_dir=clients_dir, admin_token=ADMIN_TOKEN, public_base_url=public,
    )


def build_app(tmp: Path, capture: ZernioApiCapture, **kw):
    from main import create_app
    return create_app(build_settings(str(tmp), **kw), sender_factory=lambda s: None)


def main():
    from main import create_app

    tmp = Path(tempfile.mkdtemp(prefix="bot_zconnect_"))
    write_client(tmp, SLUG_A)                 # zernio, без account/profile
    write_client(tmp, SLUG_TG, provider="tg")

    capture = ZernioApiCapture()
    app = build_app(tmp, capture)
    headers = {"X-Admin-Token": ADMIN_TOKEN}
    link_url = f"/admin/clients/{SLUG_A}/zernio/connect-link"
    sync_url = f"/admin/clients/{SLUG_A}/zernio/sync-account"

    try:
        with TestClient(app) as client:
            app.state.state.zernio_api_factory = capture.factory
            print("[0] Zernio-клиент без accountId зарегистрирован (номер ещё не подключён)")
            check("клиент в реестре по slug", SLUG_A in app.state.state.tenants)

            print("[1] connect-link: профиль создаётся и сохраняется, ссылка отдана")
            r = client.post(link_url, headers=headers,
                            json={"redirect_url": PUBLIC + "/connect/done"})
            body = r.json()
            check("200 и authUrl", r.status_code == 200
                  and body.get("authUrl") == "https://zernio.com/signup/abc")
            check("профиль создан (POST /profiles)",
                  len(capture.calls_to("/profiles")) == 1)
            check("profileId сохранён в yaml",
                  read_cfg(tmp, SLUG_A).get("zernio_profile_id") == PROFILE_A)
            call = capture.calls_to("/connect/whatsapp")[-1]
            check("ссылка: profileId+redirect+hosted+api",
                  f"profileId={PROFILE_A}" in call["query"]
                  and "signup=hosted" in call["query"]
                  and "onboarding=api" in call["query"]
                  and "redirect_url=" in call["query"])
            audit = json.loads((tmp / ".audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            check("аудит: zernio_profile", audit["action"] == "zernio_profile")

            print("[2] повторный connect-link не создаёт новый профиль")
            r = client.post(link_url, headers=headers, json={})
            check("200", r.status_code == 200)
            check("POST /profiles всё ещё один", len(capture.calls_to("/profiles")) == 1)
            check("redirect_url по умолчанию — PUBLIC_BASE_URL/connect/done",
                  "redirect_url=https%3A%2F%2Fbot.example.com%2Fconnect%2Fdone"
                  in capture.calls_to("/connect/whatsapp")[-1]["query"])

            print("[3] sync-account: пока нет аккаунта -> 409")
            capture.accounts = []
            r = client.post(sync_url, headers=headers)
            check("409 и подсказка", r.status_code == 409
                  and "нет подключённого" in r.json().get("error", ""))

            print("[4] sync-account: аккаунт появился -> сохраняется")
            capture.accounts = [{"_id": ACCOUNT_A, "platform": "whatsapp",
                                 "username": "+77001234567", "profileId": PROFILE_A}]
            r = client.post(sync_url, headers=headers)
            check("200 и accountId", r.status_code == 200
                  and r.json()["accountId"] == ACCOUNT_A)
            check("accountId в yaml", read_cfg(tmp, SLUG_A).get("zernio_account_id") == ACCOUNT_A)
            check("клиент зарегистрирован по accountId",
                  ACCOUNT_A in app.state.state.tenants)

            print("[5] два WhatsApp-аккаунта в профиле -> 409")
            capture.accounts = [
                {"_id": ACCOUNT_A, "platform": "whatsapp", "username": "a"},
                {"_id": "77c3f20e9d4a6b8f0e1c2d3e", "platform": "whatsapp", "username": "b"},
            ]
            r = client.post(sync_url, headers=headers)
            check("409 про неоднозначность", r.status_code == 409
                  and "больше одного" in r.json().get("error", ""))

            print("[6] ошибка Zernio -> 502 с текстом")
            capture.accounts = None  # заставим handler упасть
            def failing(request):
                return httpx.Response(400, json={"error": "Bad request", "code": "X"})
            app.state.state.zernio_api_factory = lambda s: ZernioApiClient(
                API_KEY, base_url=BASE, transport=httpx.MockTransport(failing))
            r = client.post(sync_url, headers=headers)
            check("502 и текст ошибки", r.status_code == 502
                  and "Bad request" in r.json().get("error", ""))
            app.state.state.zernio_api_factory = capture.factory

            print("[7] права и провайдер")
            r = client.post(link_url)
            check("без токена -> 401", r.status_code == 401)
            r = client.post(f"/admin/clients/{SLUG_TG}/zernio/connect-link", headers=headers)
            check("не zernio-клиент -> 409", r.status_code == 409)

            print("[7b] создание Zernio-клиента без accountId через PUT")
            r = client.put("/admin/clients/new-shop", headers=headers, json={
                "provider": "zernio", "business_name": "Новый", "tone": "в",
                "language": "ru", "knowledge_base": "тест", "llm": {}})
            check("200 и warning про подключение", r.status_code == 200
                  and any("Сгенерировать ссылку" in w for w in r.json().get("warnings", [])))
            check("новый клиент в реестре по slug",
                  "new-shop" in app.state.state.tenants)

            print("[8] вебхук сервиса")
            r = client.post("/admin/zernio/register-webhook",
                            headers={"X-Client-Token": "nope"})
            check("клиентский токен -> 403", r.status_code == 403)
            r = client.post("/admin/zernio/register-webhook", headers=headers)
            check("админ -> 200 и url", r.status_code == 200
                  and r.json()["url"] == PUBLIC + "/webhooks/zernio")
            hook = capture.calls_to("/webhooks/settings")[-1]
            check("секрет и событие ушли в Zernio",
                  hook["json"]["secret"] == SECRET
                  and hook["json"]["events"] == ["message.received"]
                  and hook["json"]["url"] == PUBLIC + "/webhooks/zernio")

        print("[9] нет ZERNIO_WEBHOOK_SECRET -> 409 с подсказкой")
        app2 = build_app(tmp, capture, webhook_secret="")
        with TestClient(app2) as client2:
            app2.state.state.zernio_api_factory = capture.factory
            r = client2.post("/admin/zernio/register-webhook", headers=headers)
            check("409 про .env", r.status_code == 409
                  and "ZERNIO_WEBHOOK_SECRET" in r.json().get("error", ""))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
