"""Проверка подписи вебхуков Zernio.

Zernio подписывает каждое доставленное событие заголовком X-Zernio-Signature:
нижний hex от HMAC-SHA256 по сырым байтам тела, ключ — секрет эндпоинта,
заданный при создании вебхука (POST /v1/webhooks/settings). Substring
"sha256=" у Zernio нет, в отличие от Meta. Устаревший алиас
X-Late-Signature несёт то же значение.

Подпись считается по сырым байтам тела: пере-сериализация JSON меняет
байты, и подпись «разъедется» (см. tests/test_zernio_security.py).
"""

import hashlib
import hmac

# Заголовок с подписью события (голый hex-дайджест).
HEADER_SIGNATURE = "X-Zernio-Signature"

# Устаревший алиас того же заголовка — принимаем как запасной вариант.
HEADER_SIGNATURE_LEGACY = "X-Late-Signature"


def verify_zernio_signature(
    secret: str, raw_body: bytes, signature_header: str,
) -> bool:
    """Проверяет X-Zernio-Signature; True — событие пришло от Zernio.

    Подпись = hex(HMAC-SHA256(secret, raw_body)), lower-case. Считать нужно
    по сырым байтам тела. Пустой секрет/подпись или пустое тело — False.
    """
    if not (secret and signature_header and raw_body):
        return False
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    # Сравниваем байты: compare_digest со str падает на не-ASCII подписи.
    provided = signature_header.strip().lower().encode("utf-8")
    return hmac.compare_digest(provided, expected.encode("utf-8"))
