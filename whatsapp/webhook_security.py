"""Проверка подписи входящих вебхуков Bird (схема Standard Webhooks).

Bird подписывает каждую доставку так:
  - заголовок webhook-id        — id доставки (тот же при повторах);
  - заголовок webhook-timestamp — unix-время в секундах;
  - заголовок webhook-signature — "v1,<base64(hmac-sha256)>"; подписей может
    быть несколько через пробел (актуальная + предыдущая при ротации секрета).

Подписывается строка "{webhook-id}.{webhook-timestamp}.{raw_body}"; ключ —
base64-декод секрета без префикса "whsec_". Проверять нужно сырые байты
тела: повторная сериализация JSON меняет пробелы и порядок ключей, и
подпись «разъедется» (см. tests/test_webhook_security.py).
"""

import base64
import binascii
import hashlib
import hmac
import logging
import time

logger = logging.getLogger(__name__)

# Окно свежести: доставки старше 5 минут не принимаем — защита от replay
# (дефолт схемы Standard Webhooks, которой следует Bird).
SIGNATURE_MAX_AGE_SEC = 300

# Префикс версии подписи в заголовке webhook-signature ("v1,<base64>").
SIGNATURE_VERSION = "v1"
# Префикс секрета вебхука, выдаваемого Bird (Developers > Webhooks).
SECRET_PREFIX = "whsec_"

# Заголовки, которые Bird добавляет к каждой доставке.
HEADER_WEBHOOK_ID = "webhook-id"
HEADER_TIMESTAMP = "webhook-timestamp"
HEADER_SIGNATURE = "webhook-signature"


def verify_signature(
    secret: str,
    webhook_id: str,
    timestamp: str,
    signature_header: str,
    raw_body: bytes,
    *,
    now: int | None = None,
) -> bool:
    """Проверяет подпись вебхука Bird; True — запрос пришёл от Bird.

    secret — секрет вебхука (whsec_...); webhook_id/timestamp/signature —
    значения заголовков webhook-id / webhook-timestamp / webhook-signature;
    raw_body — точное тело запроса (bytes), до какого-либо парсинга.

    Любое несоответствие (нет заголовков, нечисловой или устаревший
    timestamp, неверная подпись) — False; исключения наружу не протекают.
    now — точка отсчёта возраста (секунды) для тестов.
    """
    if not (secret and webhook_id and timestamp and signature_header and raw_body):
        return False
    if not timestamp.isdigit():
        return False

    current = time.time() if now is None else now
    try:
        age = abs(int(timestamp) - int(current))
    except (ValueError, OverflowError):
        return False
    if age > SIGNATURE_MAX_AGE_SEC:
        # Устаревшие доставки не принимаем — защита от повторного проигрывания.
        return False

    key = _decode_secret(secret)
    if not key:
        logger.error(
            "BIRD_WEBHOOK_SECRET не декодируется: ожидается значение вида whsec_<base64>"
        )
        return False

    signed_payload = f"{webhook_id}.{timestamp}.".encode("utf-8") + raw_body
    expected = hmac.new(key, signed_payload, hashlib.sha256).digest()

    # В заголовке бывает несколько подписей ("v1,aaa v1,bbb") — достаточно
    # совпадения любой; сравнение constant-time (защита от timing-атак).
    for candidate in signature_header.split():
        if not candidate.startswith(SIGNATURE_VERSION + ","):
            continue
        if hmac.compare_digest(_b64decode(candidate[len(SIGNATURE_VERSION) + 1:]), expected):
            return True
    return False


def _b64decode(value: str) -> bytes:
    try:
        return base64.b64decode(value)
    except (binascii.Error, ValueError):
        return b""


def _decode_secret(secret: str) -> bytes:
    """Ключ HMAC = base64-декод секрета без префикса whsec_."""
    if secret.startswith(SECRET_PREFIX):
        secret = secret[len(SECRET_PREFIX):]
    return _b64decode(secret)
