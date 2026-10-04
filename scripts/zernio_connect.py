"""Утилита подключения WhatsApp-аккаунтов к Zernio (до запуска бота).

Делает то, что бот не делает сам: создаёт профиль клиента в Zernio, выдаёт
ссылку Embedded Signup, показывает accountId для clients/<slug>.yaml и
регистрирует вебхук на наш /webhooks/zernio.

Запуск из корня проекта (нужен ZERNIO_API_KEY в .env):
    python scripts/zernio_connect.py profiles
    python scripts/zernio_connect.py create-profile --name nails-studio
    python scripts/zernio_connect.py accounts --profile-id 66a1...
    python scripts/zernio_connect.py link --client clients/nails-studio.yaml \\
        --redirect-url https://bot.example.com/connect/callback --name "Nails Studio"
    python scripts/zernio_connect.py register-webhook \\
        --url https://bot.example.com/webhooks/zernio --secret <секрет>

После того как клиент прошёл по ссылке, он возвращается на redirect-url с
параметром accountId — впишите его в yaml клиента как zernio_account_id.
Режим `link --client` сам создаёт профиль (если нужно) и сохраняет его id в
поле zernio_profile_id, чтобы ссылку можно было выдать повторно.
"""

import argparse
import os
import sys
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DEFAULT_BASE_URL = "https://zernio.com/api/v1"
# Поддерживаемые события: message.received (входящие), message.sent (исходящие от бизнеса),
# message.failed (ошибки доставки), message.delivered, message.read
DEFAULT_EVENTS = "message.received,message.sent,message.failed"


def _client(base_url: str, api_key: str) -> httpx.Client:
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30.0,
    )


def _fail(response: httpx.Response) -> None:
    """Печатает ошибку Zernio и завершает скрипт кодом 1."""
    detail = response.text[:500]
    print(f"ОШИБКА {response.status_code}: {detail}", file=sys.stderr)
    raise SystemExit(1)


def cmd_profiles(client: httpx.Client, args) -> None:
    response = client.get("/profiles")
    if response.status_code != 200:
        _fail(response)
    profiles = response.json().get("profiles", [])
    if not profiles:
        print("Профилей нет. Создайте: create-profile --name <имя>")
        return
    for profile in profiles:
        print(f"{profile.get('_id')}  {profile.get('name')}")


def cmd_create_profile(client: httpx.Client, args) -> None:
    response = client.post("/profiles", json={"name": args.name})
    if response.status_code not in (200, 201):
        _fail(response)
    profile = response.json().get("profile", {})
    print(f"Профиль создан: {profile.get('_id')}  {profile.get('name')}")


def cmd_accounts(client: httpx.Client, args) -> None:
    params = {"profileId": args.profile_id} if args.profile_id else {}
    response = client.get("/accounts", params=params)
    if response.status_code != 200:
        _fail(response)
    accounts = response.json().get("accounts", [])
    if not accounts:
        print("Аккаунтов нет — подключите номер через ссылку Embedded Signup (link).")
        return
    for account in accounts:
        print(
            f"{account.get('_id')}  {account.get('platform')}  "
            f"{account.get('username')}  profile={account.get('profileId')}  "
            f"active={account.get('isActive')}"
        )


def _read_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_yaml(path: Path, cfg: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def _profile_id_for_client(client: httpx.Client, path: Path, name: str) -> str:
    """Профиль клиента из yaml; создаём и запоминаем, если ещё нет."""
    cfg = _read_yaml(path)
    profile_id = str(cfg.get("zernio_profile_id") or "").strip()
    if profile_id:
        print(f"Профиль из yaml: {profile_id}")
        return profile_id
    response = client.post("/profiles", json={"name": name})
    if response.status_code not in (200, 201):
        _fail(response)
    profile = response.json().get("profile", {})
    profile_id = str(profile.get("_id") or "").strip()
    cfg["zernio_profile_id"] = profile_id
    _write_yaml(path, cfg)
    print(f"Профиль создан и сохранён в {path.name}: {profile_id}")
    return profile_id


def cmd_link(client: httpx.Client, args) -> None:
    if args.client:
        path = Path(args.client)
        if not path.exists():
            print(f"Нет файла клиента: {path}", file=sys.stderr)
            raise SystemExit(1)
        cfg = _read_yaml(path)
        name = args.name or str(cfg.get("business_name") or "").strip() or path.stem
        profile_id = _profile_id_for_client(client, path, name)
    elif args.profile_id:
        profile_id = args.profile_id
        name = args.name
    else:
        print("Нужен --profile-id или --client clients/<slug>.yaml", file=sys.stderr)
        raise SystemExit(2)

    query = {
        "profileId": profile_id,
        "redirect_url": args.redirect_url,
        "onboarding": args.onboarding,
    }
    if name:
        query["brandName"] = name
    if args.headless:
        query["headless"] = "true"

    response = client.get("/connect/whatsapp", params=query)
    if response.status_code != 200:
        _fail(response)
    auth_url = response.json().get("authUrl", "")
    print("\nСсылка Embedded Signup (отправьте клиенту):\n")
    print(auth_url)
    print(
        "\nПосле подключения клиент вернётся на redirect-url с параметрами\n"
        "  ?connected=whatsapp&profileId=...&accountId=...&username=...\n"
        "Впишите accountId в yaml клиента как zernio_account_id "
        "(или проверьте: accounts --profile-id <profileId>)."
    )


def cmd_register_webhook(client: httpx.Client, args) -> None:
    events = [e.strip() for e in args.events.split(",") if e.strip()]
    response = client.post("/webhooks/settings", json={
        "name": args.name,
        "url": args.url,
        "secret": args.secret,
        "events": events,
    })
    if response.status_code not in (200, 201):
        _fail(response)
    webhook = response.json().get("webhook", {})
    print(
        f"Вебхук создан: {webhook.get('_id')}  {webhook.get('url')}\n"
        f"Секрет положите в .env как ZERNIO_WEBHOOK_SECRET."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Подключение WhatsApp к Zernio")
    parser.add_argument("--base-url", default=os.getenv("ZERNIO_BASE_URL", "").strip()
                        or DEFAULT_BASE_URL, help="база API Zernio")
    parser.add_argument("--api-key", default=os.getenv("ZERNIO_API_KEY", "").strip(),
                        help="API-ключ (по умолчанию ZERNIO_API_KEY из .env)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("profiles", help="список профилей")

    p = sub.add_parser("create-profile", help="создать профиль клиента")
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_create_profile)

    p = sub.add_parser("accounts", help="список подключённых аккаунтов")
    p.add_argument("--profile-id", default="")
    p.set_defaults(func=cmd_accounts)

    p = sub.add_parser("link", help="ссылка Embedded Signup")
    p.add_argument("--profile-id", default="")
    p.add_argument("--client", default="", help="путь к clients/<slug>.yaml")
    p.add_argument("--redirect-url", required=True, help="куда вернуть клиента после подключения")
    p.add_argument("--onboarding", choices=["api", "business_app"], default="api",
                   help="api = только Cloud API (рекомендуется), business_app = coexistence")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("register-webhook", help="создать вебхук на наш сервис")
    p.add_argument("--url", required=True, help="https://<домен>/webhooks/zernio")
    p.add_argument("--secret", required=True, help="секрет -> ZERNIO_WEBHOOK_SECRET")
    p.add_argument("--name", default="wa-bot")
    p.add_argument("--events", default=DEFAULT_EVENTS, help="через запятую")
    p.set_defaults(func=cmd_register_webhook)

    args = parser.parse_args()
    if not args.api_key:
        print("Не задан ZERNIO_API_KEY (.env или --api-key)", file=sys.stderr)
        raise SystemExit(2)

    with _client(args.base_url, args.api_key) as client:
        args.func(client, args)


if __name__ == "__main__":
    main()
