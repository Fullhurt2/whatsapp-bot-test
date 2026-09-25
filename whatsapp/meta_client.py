"""Клиент WhatsApp Cloud API (Graph API) — прямой транспорт без Bird.

Документация: https://developers.facebook.com/docs/whatsapp/cloud-api
Отправка: POST https://graph.facebook.com/{версия}/{phone_number_id}/messages
с заголовком Authorization: Bearer <доступ-токен>.

Сервисные сообщения (ответы на входящие в течение 24-часового окна) у Meta
бесплатны; платные — только шаблонные категории (маркетинг/утилиты/авторизация),
которые этот бот не использует.

Задача модуля — изолировать HTTP-детали Graph API: хендлеры вызывают только
send_text() и не знают про версии API и коды ответов.
"""

import asyncio
import logging

import httpx

from config.settings import normalize_phone
from whatsapp.errors import MessagingError, MessagingTimeout

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

# Версия Graph API (переопределяется переменной META_GRAPH_VERSION в .env).
DEFAULT_GRAPH_VERSION = "v21.0"

# Лимит длины одного WhatsApp-текстового сообщения (ограничение Meta):
# длинные ответы режем на части, как в Bird-клиенте.
WHATSAPP_TEXT_LIMIT = 4096

# Повтор при разовом сбое Graph API (5xx) и лимите (429).
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
RATE_LIMIT_MAX_RETRY_SEC = 5.0

# Graph API отвечает быстро (200 = сообщение принято в очередь); запас — на сеть.
SEND_TIMEOUT_SEC = 20.0
CONNECT_TIMEOUT_SEC = 10.0


class MetaError(MessagingError):
    """Любая ошибка вызова Meta Graph API (сеть, HTTP-ошибка, отказ)."""


class MetaTimeout(MessagingTimeout):
    """Meta не ответила за отведённое время."""


class MetaWhatsAppClient:
    """Асинхронный клиент отправки WhatsApp-сообщений. Один на всё приложение.

    phone_number_id — идентификатор бизнес-номера отправителя из дашборда
    Meta (не сам номер): Graph API требует его в пути каждого запроса.
    """

    def __init__(
        self,
        access_token: str,
        phone_number_id: str,
        graph_version: str = DEFAULT_GRAPH_VERSION,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = f"/{graph_version}/{phone_number_id}/messages"
        self._client = httpx.AsyncClient(
            base_url="https://graph.facebook.com",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(SEND_TIMEOUT_SEC, connect=CONNECT_TIMEOUT_SEC),
            transport=transport,  # точка для тестов: подменяется на MockTransport
        )

    async def send_text(self, to: str, text: str) -> None:
        """Отправляет текст получателю `to` (номер в любом формате).

        Длинные тексты бьёт на части по лимиту WhatsApp. Бросает MetaError
        при любой неудаче — вызывающий код решает, логировать сбой (ответ
        клиенту) или просто пропустить (уведомление владельцу).
        """
        text = text or ""
        if not text:
            return
        for i in range(0, len(text), WHATSAPP_TEXT_LIMIT):
            await self._send_text_chunk(
                receiver=normalize_phone(to).lstrip("+"),
                body=text[i:i + WHATSAPP_TEXT_LIMIT],
            )

    async def close(self) -> None:
        await self._client.aclose()

    # --- внутреннее -----------------------------------------------------------

    async def _send_text_chunk(self, receiver: str, body: str) -> None:
        """POST /{phone_number_id}/messages: один получатель, один кусок текста.

        Meta отвечает 200 (сообщение принято в очередь). Разовый сбой (5xx)
        и rate-limit (429) — один повтор с паузой.
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": receiver,
            "type": "text",
            "text": {"preview_url": False, "body": body},
        }
        response = await self._post(payload)
        if response.status_code in RETRYABLE_STATUSES:
            delay = _retry_after_seconds(response, RATE_LIMIT_MAX_RETRY_SEC)
            logger.warning(
                "Meta вернула статус %d — повторяю отправку через %.1f сек",
                response.status_code, delay,
            )
            await asyncio.sleep(delay)
            response = await self._post(payload)

        if response.status_code != 200:
            raise MetaError(
                f"Meta API вернул статус {response.status_code}: {response.text[:200]}"
            )

    async def _post(self, payload: dict) -> httpx.Response:
        try:
            return await self._client.post(self._endpoint, json=payload)
        except httpx.TimeoutException as exc:
            raise MetaTimeout(f"Meta не ответила за {SEND_TIMEOUT_SEC} сек") from exc
        except httpx.HTTPError as exc:  # DNS, обрыв соединения и т.п.
            raise MetaError(f"Сетевая ошибка при вызове Graph API: {exc}") from exc


def _retry_after_seconds(response: httpx.Response, cap: float) -> float:
    """Retry-After из ответа Meta (в секундах), ограниченный сверху потолком."""
    raw = response.headers.get("Retry-After", "")
    try:
        return min(max(float(raw), 0.0), cap)
    except ValueError:
        return 1.0
