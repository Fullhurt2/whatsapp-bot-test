# Тесты блока «Профиль WhatsApp» админ-API: чтение/правка полей профиля через
# Meta Graph API, загрузка аватара, валидация (512 символов, https, email),
# права клиентского токена, маппинг ошибок Meta в 502/504 и аудит-строки.
# Обращения к Graph API перехватываются MockTransport'ом настоящего
# MetaWhatsAppClient — наружу (в Meta) ничего не уходит.
# Запуск: python tests/test_admin_profile.py

import json
import logging
import shutil
import sys
import tempfile
from dataclasses import replace
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
WABA_A = "waba-token-A-0123456789abcdef"

PID_A = "111111111111111"
PID_B = "222222222222222"
PID_UNKNOWN = "999999999999999"

PROFILE_SUFFIX = "/whatsapp_business_profile"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"avatar-bytes"
WEBP_BYTES = b"RIFF____WEBP" + b"webp-bytes"

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


class GraphCapture:
    """Перехват всех обращений клиента к Graph API + управление ответами.

    profile_error — когда задан, любой вызов профиля возвращает этот отказ
    Meta (сообщение и код), чтобы проверить маппинг ошибок в 502.
    """

    def __init__(self):
        self.calls = []
        self.profile_error = None

    def make_sender(self, tenant_settings):
        def handler(request: httpx.Request) -> httpx.Response:
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

            if request.url.path.endswith("/messages"):
                return httpx.Response(200, json={"messages": [{"id": "wamid.OUT"}]})
            if self.profile_error is not None:
                return httpx.Response(
                    self.profile_error.get("status", 400),
                    json=self.profile_error.get("body", {}),
                )
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{
                    "description": "Кофейня на Абая, 8:00–20:00",
                    "about": "Кофе и десерты",
                    "email": "hello@shop.example",
                    "websites": ["https://shop.example"],
                    "vertical": "OTHER",
                    "address": "Абай 12, Алматы",
                    "profile_picture_url": "https://pps.whatsapp.net/v/t61/avatar.jpg",
                }]})
            return httpx.Response(200, json={"success": True})

        return MetaWhatsAppClient(
            tenant_settings.whatsapp_access_token,
            tenant_settings.whatsapp_phone_number_id,
            graph_version=tenant_settings.meta_graph_version,
            transport=httpx.MockTransport(handler),
        )

    def profile_calls(self) -> list[dict]:
        return [c for c in self.calls if c["path"].endswith(PROFILE_SUFFIX)]


def write_client(tmp: Path, pid: str, business: str, *, token: str = "",
                 mgmt_token: str = "") -> None:
    (tmp / f"{pid}.yaml").write_text(yaml.safe_dump({
        "business_name": business,
        "tone": "вежливый",
        "language": "ru",
        "knowledge_base": f"Услуги: кофе 1000 ₸ ({business}).",
        "owner_whatsapp_phone": "",
        "fallback_triggers": ["жалоба"],
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


def audit_tail(tmp: Path) -> dict:
    lines = (tmp / ".audit.jsonl").read_text(encoding="utf-8").splitlines()
    return json.loads(lines[-1]) if lines else {}


def main():
    from main import create_app

    tmp = Path(tempfile.mkdtemp(prefix="bot_profile_"))
    write_client(tmp, PID_A, "Студия А", token=WABA_A, mgmt_token=MGMT_A)
    write_client(tmp, PID_B, "Кофейня Б")  # без своих токенов, общий WABA-токен

    settings = build_settings(str(tmp), ADMIN_TOKEN)
    capture = GraphCapture()
    app = create_app(settings, sender_factory=capture.make_sender)

    try:
        with TestClient(app) as client:
            admin_headers = {"X-Admin-Token": ADMIN_TOKEN}
            client_headers = {"X-Client-Token": MGMT_A}
            profile_url = f"/admin/clients/{PID_A}/profile"

            print("[1] GET профиля: разбор ответа Meta")
            r = client.get(profile_url)
            check("без токена -> 401", r.status_code == 401)
            r = client.get(profile_url, headers={"X-Admin-Token": "wrong"})
            check("неверный токен -> 403", r.status_code == 403)
            r = client.get(f"/admin/clients/{PID_B}/profile", headers=client_headers)
            check("клиентский токен на чужой pid -> 403", r.status_code == 403)
            r = client.get(f"/admin/clients/{PID_UNKNOWN}/profile", headers=admin_headers)
            check("неизвестный клиент -> 404", r.status_code == 404)

            r = client.get(profile_url, headers=admin_headers)
            body = r.json()
            check("200 и поля профиля", r.status_code == 200 and body == {
                "description": "Кофейня на Абая, 8:00–20:00",
                "about": "Кофе и десерты",
                "email": "hello@shop.example",
                "websites": ["https://shop.example"],
                "address": "Абай 12, Алматы",
                "photo_url": "https://pps.whatsapp.net/v/t61/avatar.jpg",
            })
            call = capture.profile_calls()[-1]
            check("GET на whatsapp_business_profile нужной версии",
                  call["method"] == "GET"
                  and call["path"] == f"/v21.0/{PID_A}{PROFILE_SUFFIX}")
            check("запрошены поля профиля",
                  all(field in call["query"]
                      for field in ("about", "description", "email", "websites", "address",
                                    "profile_picture_url")))
            check("токен клиента в Bearer", call["auth"] == f"Bearer {WABA_A}")
            r = client.get(f"/admin/clients/{PID_B}/profile", headers=admin_headers)
            check("клиент без своего токена берёт общий",
                  r.status_code == 200
                  and capture.profile_calls()[-1]["auth"] == f"Bearer {GLOBAL_TOKEN}")

            print("[2] пустые поля Meta -> пустые значения, не ошибка")
            capture.calls.clear()

            def handler_empty(request):
                return httpx.Response(200, json={"data": [{}]})

            empty = MetaWhatsAppClient(WABA_A, PID_A, transport=httpx.MockTransport(handler_empty))
            import asyncio
            empty_profile = asyncio.run(empty.get_business_profile())
            check("все поля пустые, websites — список, фото — пустое",
                  empty_profile == {"description": "", "about": "", "email": "",
                                    "websites": [], "vertical": "", "address": "",
                                    "photo_url": ""})

            def handler_http_photo(request):
                return httpx.Response(200, json={"data": [{"profile_picture_url": "http://cdn/a.jpg"}]})

            http_photo = asyncio.run(MetaWhatsAppClient(
                WABA_A, PID_A, transport=httpx.MockTransport(handler_http_photo)
            ).get_business_profile())
            check("не-https ссылка на фото отбрасывается", http_photo["photo_url"] == "")

            print("[3] PATCH description: payload в Meta + аудит")
            r = client.patch(profile_url, headers=admin_headers,
                             json={"description": "Кофейня на Абая 12, работаем 8:00–20:00"})
            check("200 и ok", r.status_code == 200 and r.json() == {
                "ok": True, "changed": ["description"]})
            call = capture.profile_calls()[-1]
            check("PATCH с телом в Meta",
                  call["method"] == "PATCH" and call["json"] == {
                      "description": "Кофейня на Абая 12, работаем 8:00–20:00"})
            entry = audit_tail(tmp)
            check("аудит: admin/profile/description/phone_number_id",
                  entry.get("actor") == "admin" and entry.get("action") == "profile"
                  and entry.get("changed") == ["description"]
                  and entry.get("phone_number_id") == PID_A)
            r = client.patch(profile_url, headers=admin_headers,
                             json={"description": "x" * 512})
            check("ровно 512 символов проходят", r.status_code == 200)
            r = client.patch(profile_url, headers=admin_headers,
                             json={"description": "x" * 513})
            check("513 символов -> 400 с объяснением",
                  r.status_code == 400 and r.json().get("problems"))
            before = len(capture.profile_calls())
            client.patch(profile_url, headers=admin_headers, json={"description": "x" * 600})
            check("Meta не звали при невалидном поле",
                  len(capture.profile_calls()) == before)

            print("[4] PATCH: валидация сайта и email")
            for payload, why in (
                ({"websites": ["http://shop.example"]}, "сайт без https"),
                ({"websites": ["https://a.example", "https://b.example",
                               "https://c.example"]}, "три сайта"),
                ({"websites": "https://shop.example"}, "сайт не списком"),
                ({"email": "hello.example"}, "email без @"),
                ({"email": "@example.com"}, "email без имени"),
            ):
                r = client.patch(profile_url, headers=admin_headers, json=payload)
                check(f"{why} -> 400", r.status_code == 400 and r.json().get("problems"))
            r = client.patch(profile_url, headers=admin_headers,
                             json={"websites": ["https://a.example", "https://b.example"],
                                   "email": "hello@shop.example",
                                   "description": "", "address": "Абай 12"})
            check("валидный набор -> 200",
                  r.status_code == 200
                  and capture.profile_calls()[-1]["json"] == {
                      "websites": ["https://a.example", "https://b.example"],
                      "email": "hello@shop.example",
                      "description": "",
                      "address": "Абай 12"})
            check("в ответе и в аудите — имена полей без значений",
                  "hello@shop.example" not in json.dumps(audit_tail(tmp), ensure_ascii=False))
            r = client.patch(profile_url, headers=admin_headers, json={"vertical": "RETAIL"})
            check("поле вне списка -> 400", r.status_code == 400)
            r = client.patch(profile_url, headers=admin_headers, json={})
            check("пустое тело -> 400", r.status_code == 400)
            r = client.patch(profile_url, headers=admin_headers,
                             content="{не json".encode("utf-8"))
            check("битый JSON -> 400", r.status_code == 400)

            print("[5] аватар: multipart в Meta")
            r = client.post(f"{profile_url}/photo", headers=admin_headers,
                            files={"file": ("logo.png", PNG_BYTES, "image/png")})
            check("загрузка -> 200", r.status_code == 200 and r.json().get("ok") is True)
            call = capture.profile_calls()[-1]
            body_bytes = call["body"]
            check("POST multipart на whatsapp_business_profile",
                  call["method"] == "POST"
                  and call["content_type"].startswith("multipart/form-data")
                  and call["path"] == f"/v21.0/{PID_A}{PROFILE_SUFFIX}")
            check("messaging_product=whatsapp в теле",
                  b'name="messaging_product"' in body_bytes and b"whatsapp" in body_bytes)
            check("поле photo с байтами файла",
                  b'name="photo"; filename=' in body_bytes
                  and b".png" in body_bytes and PNG_BYTES in body_bytes)
            check("граница multipart согласована с телом",
                  call["content_type"].split("boundary=")[-1].encode() in body_bytes)
            check("аудит: profile_photo, без содержимого",
                  audit_tail(tmp)["action"] == "profile_photo"
                  and audit_tail(tmp)["changed"] == ["photo"])
            check("байты аватара не попали в аудит",
                  PNG_BYTES.decode("latin-1") not in
                  (tmp / ".audit.jsonl").read_text(encoding="utf-8"))
            r = client.post(f"{profile_url}/photo", headers=admin_headers,
                            files={"file": ("logo.webp", WEBP_BYTES, "image/webp")})
            check("webp принимается", r.status_code == 200)
            r = client.post(f"{profile_url}/photo", headers=admin_headers,
                            files={"file": ("logo.gif", b"GIF89a", "image/gif")})
            check("gif -> 400", r.status_code == 400)
            r = client.post(f"{profile_url}/photo", headers=admin_headers,
                            files={"file": ("big.png", b"0" * (5 * 1024 * 1024 + 1),
                                            "image/png")})
            check("файл больше 5 МБ -> 413", r.status_code == 413)

            print("[6] клиентский токен правит только свой профиль")
            r = client.patch(profile_url, headers=client_headers,
                             json={"description": "Салон красоты, запись по телефону"})
            check("клиент обновил свой профиль", r.status_code == 200)
            check("аудит: actor=client:<pid>",
                  audit_tail(tmp)["actor"] == f"client:{PID_A}"
                  and audit_tail(tmp)["action"] == "profile")
            r = client.patch(f"/admin/clients/{PID_B}/profile", headers=client_headers,
                             json={"description": "чужой профиль"})
            check("PATCH чужого профиля -> 403", r.status_code == 403)
            r = client.post(f"/admin/clients/{PID_B}/profile/photo", headers=client_headers,
                            files={"file": ("logo.png", PNG_BYTES, "image/png")})
            check("фото чужого профиля -> 403", r.status_code == 403)

            print("[7] отказ Meta -> 502 с её текстом")
            audit_before = len((tmp / ".audit.jsonl").read_text(encoding="utf-8").splitlines())
            capture.profile_error = {
                "status": 400,
                "body": {"error": {"message": "Unsupported post request.",
                                   "code": 131030}},
            }
            r = client.patch(profile_url, headers=admin_headers, json={"description": "Кофе"})
            check("PATCH -> 502", r.status_code == 502)
            check("текст ошибки Meta показан клиенту",
                  "Unsupported post request." in r.json().get("error", ""))
            r = client.get(profile_url, headers=admin_headers)
            check("GET -> 502", r.status_code == 502
                  and "Unsupported post request." in r.json().get("error", ""))
            r = client.post(f"{profile_url}/photo", headers=admin_headers,
                            files={"file": ("logo.png", PNG_BYTES, "image/png")})
            check("фото -> 502", r.status_code == 502)
            audit_after = len((tmp / ".audit.jsonl").read_text(encoding="utf-8").splitlines())
            check("неудачные правки в аудит не попали", audit_after == audit_before)
            capture.profile_error = None

            print("[8] нет токена для профиля -> 409; битый конфиг -> свой клиент")
            tmp_b = Path(tempfile.mkdtemp(prefix="bot_profile_b_"))
            no_global = replace(build_settings(str(tmp_b), ADMIN_TOKEN),
                                whatsapp_access_token="")
            try:
                # 8a: ни своего токена, ни общего — профилем управлять нечем.
                write_client(tmp_b, PID_UNKNOWN, "Без Токена")
                app2 = create_app(no_global, sender_factory=capture.make_sender)
                with TestClient(app2) as client2:
                    r = client2.get(f"/admin/clients/{PID_UNKNOWN}/profile",
                                   headers=admin_headers)
                    check("ни своего, ни общего токена -> 409", r.status_code == 409)
                    r = client2.patch(f"/admin/clients/{PID_UNKNOWN}/profile",
                                      headers=admin_headers, json={"description": "Кофе"})
                    check("PATCH без токена -> 409", r.status_code == 409)
                    r = client2.post(f"/admin/clients/{PID_UNKNOWN}/profile/photo",
                                     headers=admin_headers,
                                     files={"file": ("logo.png", PNG_BYTES, "image/png")})
                    check("фото без токена -> 409", r.status_code == 409)

                # 8b: свой токен есть, но конфиг не прошёл валидацию реестра —
                # профиль всё равно правится (одноразовый Meta-клиент из yaml).
                write_client(tmp_b, PID_UNKNOWN, "Битый Конфиг", token=WABA_A)
                broken = tmp_b / f"{PID_UNKNOWN}.yaml"
                cfg = yaml.safe_load(broken.read_text(encoding="utf-8"))
                cfg["knowledge_base"] = ""      # реестр такого клиента не возьмёт
                broken.write_text(yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
                app3 = create_app(no_global, sender_factory=capture.make_sender)
                with TestClient(app3) as client3:
                    check("клиент не зарегистрирован реестром",
                          PID_UNKNOWN not in app3.state.state.tenants)
                    capture.calls.clear()
                    r = client3.get(f"/admin/clients/{PID_UNKNOWN}/profile",
                                    headers=admin_headers)
                    check("профиль битого клиента читается его токеном",
                          r.status_code == 200
                          and capture.profile_calls()[-1]["auth"] == f"Bearer {WABA_A}")
            finally:
                shutil.rmtree(tmp_b, ignore_errors=True)

            print("[9] панель: профиль удалён, есть только конфиг")
            r = client.get("/admin")
            check("страница отдаётся", r.status_code == 200)
            check("нет вкладки профиля",
                  'id="f_description"' not in r.text
                  and 'id="descriptionCounter"' not in r.text
                  and 'id="f_profile_photo"' not in r.text
                  and 'id="avatarCurrent"' not in r.text)
            check("есть вкладки конфига",
                  'id="f_business_name"' in r.text
                  and 'id="f_knowledge_base"' in r.text)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
