# Тесты проверки подписи вебхука Bird (схема Standard Webhooks).
# Запуск: python tests/test_webhook_security.py

import base64
import hashlib
import hmac
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whatsapp.webhook_security import verify_signature

# Секрет тестовый в формате Bird: whsec_ + base64(случайные байты).
SECRET = "whsec_" + base64.b64encode(b"test-secret-0123456789abcdef").decode()
OTHER_SECRET = "whsec_" + base64.b64encode(b"another-secret-0000").decode()
RAW_SECRET = base64.b64encode(b"raw-key").decode()  # валиден и без префикса

# Фиксированные тело и время: тесты детерминированы.
BODY = b'{"type": "whatsapp.received", "data": {}}'
TS = "1758800000"

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


def sign(secret: str, webhook_id: str, timestamp: str, body: bytes) -> str:
    """Вычисляет подпись так же, как Bird: hmac-sha256 от "id.timestamp.body"."""
    key = base64.b64decode(secret.removeprefix("whsec_"))
    payload = f"{webhook_id}.{timestamp}.".encode() + body
    digest = hmac.new(key, payload, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def main():
    valid = sign(SECRET, "msg_1", TS, BODY)

    print("[1] корректная подпись принимается")
    check("валидная подпись принята", verify_signature(SECRET, "msg_1", TS, valid, BODY, now=int(TS)))
    check("подпись с другим id отклонена",
          not verify_signature(SECRET, "msg_2", TS, valid, BODY, now=int(TS)))

    print("[2] подделанное тело отклоняется")
    tampered = BODY + b" "
    check("изменённое тело отклонено",
          not verify_signature(SECRET, "msg_1", TS, valid, tampered, now=int(TS)))

    print("[3] чужой секрет отклоняется")
    check("неверный секрет", not verify_signature(OTHER_SECRET, "msg_1", TS, valid, BODY, now=int(TS)))

    print("[4] свежесть timestamp")
    old_ts = str(int(TS) - 301)
    check("старше 5 минут — отклонён",
          not verify_signature(SECRET, "msg_1", old_ts, sign(SECRET, "msg_1", old_ts, BODY), BODY, now=int(TS)))
    fresh_ts = str(int(TS) + 10)
    check("свежий — принимается",
          verify_signature(SECRET, "msg_1", fresh_ts, sign(SECRET, "msg_1", fresh_ts, BODY), BODY, now=int(TS)))

    print("[5] отсутствие/порча заголовков")
    check("нет секрета", not verify_signature("", "msg_1", TS, valid, BODY, now=int(TS)))
    check("нет webhook-id", not verify_signature(SECRET, "", TS, valid, BODY, now=int(TS)))
    check("нет timestamp", not verify_signature(SECRET, "msg_1", "", valid, BODY, now=int(TS)))
    check("нет подписи", not verify_signature(SECRET, "msg_1", TS, "", BODY, now=int(TS)))
    check("нечисловое время", not verify_signature(SECRET, "msg_1", "завтра", valid, BODY, now=int(TS)))
    check("пустое тело", not verify_signature(SECRET, "msg_1", TS, valid, b"", now=int(TS)))

    print("[6] несколько подписей в заголовке")
    wrong = "v1," + base64.b64encode(b"wrong").decode()
    check("одна из двух верна — принимается",
          verify_signature(SECRET, "msg_1", TS, f"{wrong} {valid}", BODY, now=int(TS)))
    check("обе неверны — отклоняется",
          not verify_signature(SECRET, "msg_1", TS, f"{wrong} v1,%%%bad%%%", BODY, now=int(TS)))

    print("[7] секрет без префикса whsec_")
    raw_sig = sign(RAW_SECRET, "msg_1", TS, BODY)
    check("секрет без префикса работает", verify_signature(RAW_SECRET, "msg_1", TS, raw_sig, BODY, now=int(TS)))

    print("[8] байты тела важны (raw-body)")
    reserialized = b' {"type": "whatsapp.received"}  '
    check("переформатированный JSON отклонён",
          not verify_signature(SECRET, "msg_1", TS, valid, reserialized, now=int(TS)))

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
