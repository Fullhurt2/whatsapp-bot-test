#!/usr/bin/env python3
"""wa_onboard.py — подключение клиента к WhatsApp Cloud API в одну команду.

Автономный скрипт: один файл, стандартная библиотека Python 3.10+, без зависимостей.
Скопируйте его куда угодно — проект бота ему не нужен.

Сценарий: клиент создал свой BM → WABA → добавил номер → добавил ваш BM
в партнёры (Full control на ассет WhatsApp Account). Дальше этот скрипт:

  1. --list       показывает WABA и номера, доступные вашему токену;
  2. --pin        задаёт пин двухфакторки номера при регистрации в Cloud API
                  (POST /{phone_number_id}/register);
  3. подписывает WABA клиента на вебхуки вашего приложения с override
     callback на URL деплоя клиента (POST /{waba}/subscribed_apps);
  4. проверяет, что деплой отвечает на GET-верификацию (challenge);
  5. --send-test отправляет клиенту контрольное сообщение;
  6. печатает готовый .env для нового деплоя.

Примеры:
  python wa_onboard.py --list --token EAAY...
  python wa_onboard.py --token EAAY... --waba-id 4434... \\
      --webhook-url https://nails.up.railway.app/webhooks/meta \\
      --verify-token nailsstudio --pin 123456 --send-test +77081178202
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

GRAPH = "https://graph.facebook.com"
DEFAULT_VERSION = "v21.0"


def api(method: str, path: str, token: str, params: dict | None = None,
        payload: dict | None = None, version: str = DEFAULT_VERSION) -> tuple[int, dict]:
    """Запрос к Graph API. Возвращает (HTTP-статус, словарь ответа)."""
    query = dict(params or {})
    query["access_token"] = token
    url = f"{GRAPH}/{version}/{path.lstrip('/')}?{urllib.parse.urlencode(query)}"
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return 200, json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            body = {}
        return error.code, body


def http_get(url: str) -> tuple[int, str]:
    """Простой GET (для проверки challenge на деплое клиента)."""
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return 200, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, ""


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Подключение клиента к WhatsApp Cloud API: вебхук + env")
    parser.add_argument("--token", required=True,
                        help="System User токен с партнёрским доступом к WABA клиента")
    parser.add_argument("--waba-id", help="ID WABA клиента (4434...)")
    parser.add_argument("--phone-number-id",
                        help="ID номера клиента (1354...) — если задан, WABA не нужна")
    parser.add_argument("--webhook-url", help="URL вебхука деплоя клиента (.../webhooks/meta)")
    parser.add_argument("--verify-token", help="META_VERIFY_TOKEN деплоя клиента")
    parser.add_argument("--pin", help="пин 2-FA, 6 цифр: регистрация номера в Cloud API")
    parser.add_argument("--send-test", help="номер для тестовой отправки (E.164)")
    parser.add_argument("--app-secret", help="ваш App Secret (попадёт в печатаемый .env)")
    parser.add_argument("--list", action="store_true",
                        help="показать WABA и номера, доступные токену")
    parser.add_argument("--graph-version", default=DEFAULT_VERSION)
    args = parser.parse_args()

    # 1. Токен работает? Кто он такой?
    code, me = api("GET", "me", args.token, version=args.graph_version)
    if code != 200 or "id" not in me:
        print(f"!! Токен не работает: {json.dumps(me, ensure_ascii=False)[:250]}")
        sys.exit(1)
    print(f"Токен: System User «{me.get('name', '')}» (id={me['id']})")

    # 2. Доступные WABA через партнёрство
    code, wabas = api("GET", f"{me['id']}/assigned_whatsapp_business_accounts",
                      args.token, params={"fields": "id,name"}, version=args.graph_version)
    waba_list = wabas.get("data", [])

    if args.list or (not args.waba_id and not args.phone_number_id):
        print(f"\nДоступные WABA ({len(waba_list)}):")
        for waba in waba_list:
            print(f"  {waba['id']}: {waba.get('name', '')}")
        if not waba_list:
            print("  Пусто. Проверьте: клиент добавил ваш BM в Partners с доступом")
            print("  к WhatsApp Account (Full control) и подтвердил приглашение.")
            print("  Либо укажите --waba-id или --phone-number-id напрямую.")
        return

    # 3. Профиль WABA и её номера (пропускаем, если передан --phone-number-id)
    phone_id = args.phone_number_id or ""
    if not phone_id:
        code, waba_info = api("GET", args.waba_id, args.token,
                              params={"fields": "id,name,account_review_status"}, version=args.graph_version)
        print("WABA:", json.dumps(waba_info, ensure_ascii=False)[:250])
        if code != 200 or "id" not in waba_info:
            # Meta иногда не отдаёт узел WABA даже Admin-токену, если ассет не
            # назначен. Это не блокирует подписку — номера передаются явно.
            print("!! Профиль WABA недоступен этому токену (нет ассета).")
            print("   Передайте --phone-number-id из дашборда — этого достаточно")
            print("   для подписки WABA, пина и отправки. Ассет назначьте всё равно:")
            print("   Business Settings -> Users -> System users -> Assign assets.")
            sys.exit(1)

        code, phones = api("GET", f"{args.waba_id}/phone_numbers", args.token, version=args.graph_version)
        phone_list = phones.get("data", [])
        if not phone_list:
            print("!! В WABA нет номеров — добавьте номер в WhatsApp Manager и повторите.")
            sys.exit(1)
        for phone in phone_list:
            print(f"  номер {phone.get('display_phone_number')} -> "
                  f"phone_number_id={phone['id']}")
        phone_id = phone_list[0]["id"]
    else:
        print(f"Номер клиента: phone_number_id={phone_id} (задан явно)")

    # 4. Пин двухфакторки: регистрация/перерегистрация номера в Cloud API
    if args.pin and phone_id:
        code, resp = api("POST", f"{phone_id}/register", args.token,
                         payload={"messaging_product": "whatsapp", "pin": args.pin},
                         version=args.graph_version)
        if code == 200:
            print(f"Пин 2-FA задан ({args.pin}) — номер зарегистрирован в Cloud API")
        else:
            message = str(resp.get("error", {}).get("message", resp))[:150]
            print(f"Регистрация с пином: HTTP {code} — {message}")
            print("(«already registered» — номер уже на Cloud API; это не ошибка,")
            print(" пин можно оставить прежний или задать позже в WhatsApp Manager)")

    # 5. Подписка WABA на вебхуки приложения с override на деплой клиента
    if args.webhook_url and args.verify_token:
        # Подписка делается на уровне WABA; если известен только phone_number_id,
        # пытаемся вытащить WABA из профиля номера.
        waba_id = args.waba_id
        if not waba_id and phone_id:
            code, probe = api("GET", phone_id, args.token,
                              params={"fields": "whatsapp_business_account"},
                              version=args.graph_version)
            waba_node = probe.get("whatsapp_business_account")
            if isinstance(waba_node, dict) and waba_node.get("id"):
                waba_id = str(waba_node["id"])
        if not waba_id:
            print("!! Не удалось определить WABA номера — укажите --waba-id явно.")
            sys.exit(1)
        code, resp = api("POST", f"{waba_id}/subscribed_apps", args.token,
                         params={"override_callback_uri": args.webhook_url,
                                 "verify_token": args.verify_token},
                         version=args.graph_version)
        print("подписка WABA:", code, str(resp)[:200])
        if code != 200:
            sys.exit(1)

        # 6. GET-верификация деплоя: деплой должен вернуть присланный challenge
        challenge = "onboard-ok-42"
        code, body = http_get(
            f"{args.webhook_url}?{urllib.parse.urlencode({'hub.mode': 'subscribe', 'hub.verify_token': args.verify_token, 'hub.challenge': challenge})}")
        ok = code == 200 and body == challenge
        print(f"верификация деплоя: {code} -> {body[:80]!r}{' (OK)' if ok else ' (challenge не совпал)'}")

    # 7. Тестовое сообщение клиенту
    if args.send_test and phone_id:
        code, resp = api("POST", f"{phone_id}/messages", args.token,
                         payload={"messaging_product": "whatsapp",
                                  "recipient_type": "individual",
                                  "to": args.send_test.lstrip("+"), "type": "text",
                                  "text": {"preview_url": False,
                                           "body": "Проверка онбординга: бот подключён ✅"}},
                         version=args.graph_version)
        print("send:", code, str(resp)[:200])

    # 8. Готовый .env для деплоя клиента
    print("\n--- Готовый .env для деплоя клиента ---")
    print("MESSAGING_PROVIDER=meta")
    print(f"WHATSAPP_ACCESS_TOKEN={args.token}")
    print(f"WHATSAPP_PHONE_NUMBER_ID={phone_id}")
    print(f"META_APP_SECRET={args.app_secret or '<ваш App Secret>'}")
    print(f"META_VERIFY_TOKEN={args.verify_token or '<придумайте строку>'}")
    print(f"META_GRAPH_VERSION={args.graph_version}")
    print("# OWNER_WHATSAPP_NUMBER=+7...        — номер владельца клиента")
    print("# CLIENT_CONFIG=client_config_<клиент>.yaml")
    print("# LLM_API_URL / LLM_API_KEY / LLM_MODEL — ваши")
    print("# APP_HOST=0.0.0.0 (APP_PORT не задавайте: Railway передаёт PORT)")
    print("\nГотово: передеплойте деплой клиента с этим .env и напишите номеру.")


if __name__ == "__main__":
    main()
