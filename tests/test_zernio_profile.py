# Профиль WhatsApp у Zernio-клиентов: методы клиента (get/update/photo) и
# роуты админ-API /admin/clients/{pid}/profile для provider: zernio.
# Все обращения к Zernio API перехватываются MockTransport — наружу ничего.
# Запуск: python tests/test_zernio_profile.py

import asyncio
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
from whatsapp.zernio_client import ZernioApiClient, ZernioWhatsAppClient

API_KEY = "sk_test_key"
SECRET = "whsec_test"
ACCOUNT_A = "66b2e19d8c3f5a7e9d0b1c2d"
PROFILE_A = "66a1f0c2a4b9d3e8f1a2b3c4"
SLUG_A = "nails-studio"
SLUG_TG = "123456789"
ADMIN_TOKEN = "unit-admin-token-0123456789abcdef"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"avatar-bytes"

PROFILE_PATH = "/api/v1/whatsapp/business-profile"

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


class ZernioCapture:
    """Перехват запросов клиента к Zernio API + управление ошибкой."""

    def __init__(self):
        self.calls: list[dict] = []
        self.error = None
        self.accounts_api: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        record = {
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query or b""),
            "auth": request.headers.get("Authorization", ""),
            "content_type": request.headers.get("Content-Type", ""),
            "body": request.content,
        }
        try:
            record["json"] = json.loads(request.content)
        except ValueError:
            record["json"] = None
        self.calls.append(record)

        if self.error is not None:
            return httpx.Response(self.error.get("status", 400),
                                  json=self.error.get("body", {}))
        if request.url.path == "/api/v1/accounts":
            return httpx.Response(200, json={"accounts": self.accounts_api})
        if request.url.path.endswith("/whatsapp/register"):
            return httpx.Response(200, json={"registered": True, "accountId": ACCOUNT_A,
                                             "phoneNumberId": "1875844705851813"})
        if request.url.path.endswith("/whatsapp/number-info"):
            return httpx.Response(200, json={
                "phone": {"status": "CONNECTED", "display_phone_number": "+7 700 123 45 67",
                          "name_status": "APPROVED", "quality_rating": "GREEN",
                          "messaging_limit_tier": "TIER_1K", "platform_type": "CLOUD_API"},
                "waba": {"name": "Acme WABA"},
            })
        if request.url.path.endswith("/photo"):
            return httpx.Response(200, json={"success": True, "message": "ok"})
        if request.method == "GET":
            return httpx.Response(200, json={"success": True, "businessProfile": {
                "about": "Кофе и десерты",
                "description": "Кофейня на Абая, 8:00–20:00",
                "email": "hello@shop.example",
                "websites": ["https://shop.example"],
                "vertical": "RETAIL",
                "address": "Абай 12, Алматы",
                "profilePictureUrl": "https://media.zernio.com/avatar.jpg",
            }})
        return httpx.Response(200, json={"success": True, "message": "ok"})

    def make_sender(self, tenant_settings: Settings) -> ZernioWhatsAppClient:
        return ZernioWhatsAppClient(
            tenant_settings.zernio_api_key or API_KEY,
            tenant_settings.zernio_account_id or ACCOUNT_A,
            base_url="https://zernio.com/api/v1",
            transport=httpx.MockTransport(self.handler),
        )

    def make_api_client(self, settings: Settings) -> ZernioApiClient:
        return ZernioApiClient(
            settings.zernio_api_key or API_KEY,
            base_url="https://zernio.com/api/v1",
            transport=httpx.MockTransport(self.handler),
        )

    def profile_calls(self) -> list[dict]:
        return [c for c in self.calls if "/whatsapp/business-profile" in c["path"]]


def write_zernio_client(tmp: Path, slug: str, account_id: str, business: str) -> None:
    (tmp / f"{slug}.yaml").write_text(yaml.safe_dump({
        "provider": "zernio",
        "zernio_account_id": account_id,
        "zernio_profile_id": PROFILE_A,
        "business_name": business,
        "tone": "вежливый",
        "language": "ru",
        "knowledge_base": f"Услуги ({business}).",
        "owner_whatsapp_phone": "",
        "fallback_triggers": [],
        "llm": {"model": "", "temperature": 0.6, "max_tokens": 100,
                "timeout_seconds": 15},
        "management_token": "",
    }, allow_unicode=True), encoding="utf-8")


def write_tg_client(tmp: Path, bot_id: str) -> None:
    (tmp / f"{bot_id}.yaml").write_text(yaml.safe_dump({
        "provider": "tg",
        "telegram_bot_token": f"{bot_id}:AAHh-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "business_name": "TG Бот",
        "language": "ru",
        "knowledge_base": "тест",
        "llm": {"model": "", "temperature": 0.6, "max_tokens": 100,
                "timeout_seconds": 15},
    }, allow_unicode=True), encoding="utf-8")


def build_settings(clients_dir: str) -> Settings:
    return Settings(
        messaging_provider="zernio",
        whatsapp_access_token="", whatsapp_phone_number_id="",
        meta_app_secret="", meta_verify_token="", meta_graph_version="v21.0",
        zernio_api_key=API_KEY, zernio_webhook_secret=SECRET,
        zernio_base_url="https://zernio.com/api/v1", zernio_account_id="",
        app_host="127.0.0.1", app_port=8000,
        llm_api_url="https://llm.test/v1", llm_api_key="test",
        business_name="", tone="", language="ru", knowledge_base="", owner_phone=None,
        llm=LLMParams("m", 0.6, 100, 15, None),
        clients_dir=clients_dir, admin_token=ADMIN_TOKEN,
    )


def unit_tests(capture: ZernioCapture) -> None:
    print("[1] клиент: чтение профиля")
    client = capture.make_sender(build_settings(""))
    profile = asyncio.run(client.get_business_profile())
    check("нормализация полей", profile == {
        "about": "Кофе и десерты",
        "description": "Кофейня на Абая, 8:00–20:00",
        "email": "hello@shop.example",
        "websites": ["https://shop.example"],
        "vertical": "RETAIL",
        "address": "Абай 12, Алматы",
        "photo_url": "https://media.zernio.com/avatar.jpg",
    })
    call = capture.profile_calls()[-1]
    check("GET с accountId в query",
          call["method"] == "GET" and call["path"] == PROFILE_PATH
          and f"accountId={ACCOUNT_A}" in call["query"])
    check("Bearer-авторизация", call["auth"] == f"Bearer {API_KEY}")

    print("[2] клиент: обновление профиля")
    capture.calls.clear()
    asyncio.run(client.update_business_profile({"about": "Новое", "websites": ["https://a.example"]}))
    call = capture.profile_calls()[-1]
    check("POST с accountId и полями",
          call["method"] == "POST" and call["path"] == PROFILE_PATH
          and call["json"] == {"about": "Новое", "websites": ["https://a.example"],
                               "accountId": ACCOUNT_A})

    print("[3] клиент: аватар multipart")
    capture.calls.clear()
    asyncio.run(client.upload_profile_photo("whatsapp-profile.png", PNG_BYTES, "image/png"))
    call = capture.profile_calls()[-1]
    check("POST на /photo, multipart",
          call["method"] == "POST" and call["path"] == PROFILE_PATH + "/photo"
          and call["content_type"].startswith("multipart/form-data"))
    check("accountId и файл в теле",
          b'name="accountId"' in call["body"] and ACCOUNT_A.encode() in call["body"]
          and b'name="file"; filename=' in call["body"] and PNG_BYTES in call["body"])

    print("[3b] клиент: регистрация номера с PIN")
    capture.calls.clear()
    result = asyncio.run(client.register_number("481902"))
    check("registered=true", result.get("registered") is True)
    call = capture.calls[-1]
    check("POST в /accounts/{id}/whatsapp/register с pin",
          call["path"] == f"/api/v1/accounts/{ACCOUNT_A}/whatsapp/register"
          and call["json"] == {"pin": "481902"})
    capture.calls.clear()
    asyncio.run(client.register_number(""))
    check("пустой pin -> пустое тело (дефолт Zernio)",
          capture.calls[-1]["json"] == {})

    print("[3c] клиент: статус номера")
    capture.calls.clear()
    info = asyncio.run(client.get_number_info())
    check("phone.status=CONNECTED", info["phone"]["status"] == "CONNECTED")
    check("waba прочитана", info["waba"]["name"] == "Acme WABA")
    check("GET number-info с accountId",
          capture.calls[-1]["path"] == "/api/v1/whatsapp/number-info"
          and f"accountId={ACCOUNT_A}" in capture.calls[-1]["query"])


def admin_tests(capture: ZernioCapture) -> None:
    from main import create_app

    tmp = Path(tempfile.mkdtemp(prefix="bot_zprofile_"))
    write_zernio_client(tmp, SLUG_A, ACCOUNT_A, "Студия А")
    write_tg_client(tmp, SLUG_TG)

    settings = build_settings(str(tmp))
    app = create_app(settings, sender_factory=capture.make_sender)
    try:
        with TestClient(app) as client:
            headers = {"X-Admin-Token": ADMIN_TOKEN}
            url = f"/admin/clients/{SLUG_A}/profile"

            print("[4] админ GET профиля Zernio-клиента")
            capture.calls.clear()
            r = client.get(url, headers=headers)
            check("200 и нормализованный профиль",
                  r.status_code == 200 and r.json()["about"] == "Кофе и десерты"
                  and r.json()["photo_url"] == "https://media.zernio.com/avatar.jpg")
            call = capture.profile_calls()[-1]
            check("запрос ушёл в Zernio (accountId + Bearer ключ)",
                  call["path"] == PROFILE_PATH and f"accountId={ACCOUNT_A}" in call["query"]
                  and call["auth"] == f"Bearer {API_KEY}")

            print("[5] админ PATCH профиля")
            capture.calls.clear()
            r = client.patch(url, headers=headers, json={"description": "Салон красоты"})
            check("200 и changed", r.status_code == 200 and r.json() == {
                "ok": True, "changed": ["description"]})
            call = capture.profile_calls()[-1]
            check("POST в Zernio с accountId и полем",
                  call["method"] == "POST"
                  and call["json"] == {"description": "Салон красоты", "accountId": ACCOUNT_A})
            audit = json.loads((tmp / ".audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            check("аудит: profile", audit["action"] == "profile"
                  and audit["phone_number_id"] == SLUG_A)

            print("[6] админ аватар")
            capture.calls.clear()
            r = client.post(f"{url}/photo", headers=headers,
                            files={"file": ("logo.png", PNG_BYTES, "image/png")})
            check("200", r.status_code == 200 and r.json().get("ok") is True)
            call = capture.profile_calls()[-1]
            check("multipart на /photo с accountId",
                  call["path"] == PROFILE_PATH + "/photo"
                  and call["content_type"].startswith("multipart/form-data")
                  and ACCOUNT_A.encode() in call["body"] and PNG_BYTES in call["body"])

            print("[7] отказ Zernio -> 502 с его текстом")
            capture.error = {"status": 400, "body": {
                "error": "Template required", "code": "TEMPLATE_REQUIRED"}}
            r = client.patch(url, headers=headers, json={"description": "Кофе"})
            check("502", r.status_code == 502)
            check("текст ошибки показан", "Template required" in r.json().get("error", ""))
            capture.error = None

            print("[8] Telegram-клиент: профиль недоступен (409)")
            r = client.get(f"/admin/clients/{SLUG_TG}/profile", headers=headers)
            check("409 для provider: tg", r.status_code == 409)

            print("[9] админ: регистрация номера с PIN")
            capture.calls.clear()
            r = client.post(f"/admin/clients/{SLUG_A}/zernio/register-number",
                            headers=headers, json={"pin": "481902"})
            check("200 registered", r.status_code == 200 and r.json()["registered"] is True)
            call = [c for c in capture.calls
                    if c["path"].endswith("/whatsapp/register")][-1]
            check("pin ушёл в Zernio",
                  call["json"] == {"pin": "481902"}
                  and call["path"] == f"/api/v1/accounts/{ACCOUNT_A}/whatsapp/register")
            audit = json.loads((tmp / ".audit.jsonl").read_text(encoding="utf-8").splitlines()[-1])
            check("аудит: zernio_register", audit["action"] == "zernio_register")
            r = client.post(f"/admin/clients/{SLUG_A}/zernio/register-number",
                            headers=headers, json={"pin": "12"})
            check("кривой PIN -> 400", r.status_code == 400)

            print("[10] админ: статус номера из Meta")
            r = client.get(f"/admin/clients/{SLUG_A}/zernio/number-info", headers=headers)
            check("200 и CONNECTED", r.status_code == 200
                  and r.json()["status"] == "CONNECTED"
                  and r.json()["qualityRating"] == "GREEN")
            check("wabaName показан", r.json()["wabaName"] == "Acme WABA")

            print("[11] админ: список аккаунтов профиля (диагностика)")
            capture.accounts_api = [{"_id": ACCOUNT_A, "platform": "whatsapp",
                                     "username": "+77001234567", "isActive": True}]
            app.state.state.zernio_api_factory = capture.make_api_client
            r = client.get(f"/admin/clients/{SLUG_A}/zernio/accounts", headers=headers)
            check("200 и аккаунт в списке", r.status_code == 200
                  and r.json()["accounts"][0]["accountId"] == ACCOUNT_A)
            check("запрошен профиль клиента",
                  f"profileId={PROFILE_A}" in capture.calls[-1]["query"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    capture = ZernioCapture()
    unit_tests(capture)
    admin_tests(capture)
    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
