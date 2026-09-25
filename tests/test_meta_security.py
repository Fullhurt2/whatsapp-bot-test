# Тесты проверки подписи Meta (X-Hub-Signature-256) и GET-верификации подписки.
# Запуск: python tests/test_meta_security.py

import hashlib
import hmac
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whatsapp.meta_security import verify_meta_signature, verify_subscription

APP_SECRET = "test-app-secret"
BODY = b'{"object": "whatsapp_business_account"}'

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


def sign(app_secret: str, body: bytes) -> str:
    """Подпись так же, как её делает Meta."""
    digest = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return "sha256=" + digest


def main():
    valid = sign("test-app-secret", BODY)

    print("[1] корректная подпись принимается")
    check("валидная подпись принята", verify_meta_signature(APP_SECRET, BODY, valid))
    check("подпись другого секрета отклонена",
          not verify_meta_signature("другой-секрет", BODY, valid))

    print("[2] подделка тела/заголовка")
    check("изменённое тело отклонено", not verify_meta_signature("s", BODY, sign("s", BODY + b" ")))
    check("подпись без префикса sha256= отклонена",
          not verify_meta_signature("s", BODY, "abcd1234"))
    check("пустая подпись отклонена", not verify_meta_signature("s", BODY, ""))
    check("пустой секрет отклонён", not verify_meta_signature("", BODY, "sha256=abc"))
    check("пустое тело отклонено", not verify_meta_signature("s", b"", "sha256=abc"))

    print("[3] байты тела важны (raw-body)")
    original_body = b'{"x": 1}'
    reserialized = b' {"x": 1} '
    check("переформатированный JSON отклонён",
          not verify_meta_signature("s", reserialized, sign("s", original_body)))

    print("[4] GET-верификация подписки")
    check("верные параметры принимаются", verify_subscription("subscribe", "tok", "tok"))
    check("неверный verify_token", not verify_subscription("subscribe", "tok", "другой"))
    check("чужой hub.mode", not verify_subscription("unsubscribe", "tok", "tok"))
    check("пустой verify_token", not verify_subscription("subscribe", "", "tok"))
    check("не задан expected token", not verify_subscription("subscribe", "tok", ""))

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
