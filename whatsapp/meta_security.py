"""Проверка подписи вебхуков Meta Cloud API и верификация подписки.

Meta подписывает каждое доставленное событие заголовком
X-Hub-Signature-256: "sha256=<hex(hmac-sha256(app_secret, raw_body))>".
Это заметно проще схемы Bird: один секрет (App Secret приложения),
одна подпись, constant-time сравнение. Как и у Bird, подпись считается
по сырым байтам тела — пере-сериализация JSON ломает подпись.

Первичная привязка вебхука: Meta дёргает наш URL GET-запросом с параметрами
hub.mode=subscribe, hub.verify_token, hub.challenge — при совпадении
verify_token нужно вернуть challenge как чистый текст (см. main.py).
"""

import hashlib
import hmac

# Заголовок с подписью события (hex-дайджест с префиксом "sha256=").
META_HEADER_SIGNATURE = "X-Hub-Signature-256"

# Параметры GET-запроса при первичной привязке вебхука в дашборде Meta.
QUERY_MODE = "hub.mode"
QUERY_VERIFY_TOKEN = "hub.verify_token"
QUERY_CHALLENGE = "hub.challenge"

# Значение hub.mode при подписке.
SUBSCRIBE_MODE = "subscribe"


def verify_meta_signature(app_secret: str, raw_body: bytes, signature_header: str) -> bool:
    """Проверяет X-Hub-Signature-256; True — событие пришло от Meta.

    Подпись = "sha256=" + hex(HMAC-SHA256(app_secret, raw_body)). Считать
    нужно по сырым байтам тела: пере-сериализация JSON меняет байты, и
    подпись «разъедется» (см. tests/test_meta_security.py).
    """
    if not (app_secret and signature_header and raw_body):
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature_header.strip(), "sha256=" + expected)


def verify_subscription(hub_mode: str, verify_token: str, expected_token: str) -> bool:
    """True, если GET-верификация подписки от Meta корректна.

    hub_mode — значение hub.mode из запроса; verify_token — значение
    hub.verify_token; expected_token — VERIFY_TOKEN из наших настроек.
    """
    if not (hub_mode == SUBSCRIBE_MODE and verify_token and expected_token):
        return False
    return hmac.compare_digest(
        verify_token.encode("utf-8"), expected_token.encode("utf-8")
    )
