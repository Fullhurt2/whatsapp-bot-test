# Тесты проверки подписи вебхука Zernio (X-Zernio-Signature).
# Запуск: python tests/test_zernio_security.py

import hashlib
import hmac
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whatsapp.zernio_security import verify_zernio_signature

SECRET = "whsec_test_123"
BODY = b'{"event":"message.received","id":"evt-1"}'

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


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def main():
    print("[1] корректная подпись")
    check("верная подпись проходит", verify_zernio_signature(SECRET, BODY, sign(SECRET, BODY)))
    check("uppercase hex тоже проходит",
          verify_zernio_signature(SECRET, BODY, sign(SECRET, BODY).upper()))
    check("пробелы вокруг подписи игнорируются",
          verify_zernio_signature(SECRET, BODY, "  " + sign(SECRET, BODY) + "  "))

    print("[2] отклонения")
    check("чужая подпись", not verify_zernio_signature(SECRET, BODY, sign("other", BODY)))
    check("другой секрет", not verify_zernio_signature("wrong", BODY, sign(SECRET, BODY)))
    check("подпись по изменённому телу",
          not verify_zernio_signature(SECRET, BODY + b"x", sign(SECRET, BODY)))
    check("мусорная подпись", not verify_zernio_signature(SECRET, BODY, "не-hex"))

    print("[3] пустые входы")
    check("пустой секрет", not verify_zernio_signature("", BODY, sign(SECRET, BODY)))
    check("пустая подпись", not verify_zernio_signature(SECRET, BODY, ""))
    check("пустое тело", not verify_zernio_signature(SECRET, b"", sign(SECRET, b"")))

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
