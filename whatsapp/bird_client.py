"""Клиент Bird API для отправки WhatsApp-сообщений.

Документация: https://bird.com/docs/guides/whatsapp/sending-whatsapp
Отправка: POST https://{region}.platform.bird.com/v1/whatsapp/messages,
регион выводится из префикса API-ключа (bk_eu1_ -> eu1.platform.bird.com)
в config/settings.py и приходит сюда готовым base_url.

Задача модуля — изолировать HTTP-детали Bird: хендлеры вызывают только
send_text() и не знают про заголовки, идемпотентность и коды ответов.
"""

import asyncio
import logging
import uuid

import httpx

from config.settings import normalize_phone
from whatsapp.errors import MessagingError, MessagingTimeout

logger = logging.getLogger(__name__)

# Лимит длины одного WhatsApp-текстового сообщения (ограничение Meta):
# длинные ответы режем на части, как в Telegram-версии бота.
WHATSAPP_TEXT_LIMIT = 4096

# Пауза перед единственным повтором при разовом сбое Bird (5xx).
SERVER_ERROR_RETRY_DELAY_SEC = 1.0
# 429: Bird советует Retry-After; ждём не дольше этого потолка, чтобы
# фоновая обработка вебхука не растягивалась на минуты.
RATE_LIMIT_MAX_RETRY_SEC = 5.0

# Bird быстро отвечает 202 Accepted; запас — на сеть и прокси.
SEND_TIMEOUT_SEC = 20.0
CONNECT_TIMEOUT_SEC = 10.0


class BirdError(MessagingError):
    """Любая ошибка вызова Bird API (сеть, HTTP-ошибка, отказ)."""


class BirdTimeout(MessagingTimeout):
    """Bird не ответил за отведённое время."""


class BirdWhatsAppClient:
    """Асинхронный клиент отправки WhatsApp-сообщений. Один на всё приложение.

    sender_number — бизнес-номер (поле "from"): Bird требует его в каждом
    сообщении (ошибка WhatsAppSenderRequired, если не передать).
    """

    def __init__(self, api_key: str, api_url: str, sender_number: str,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._sender = normalize_phone(sender_number)
        self._client = httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(SEND_TIMEOUT_SEC, connect=CONNECT_TIMEOUT_SEC),
            transport=transport,  # точка для тестов: подменяется на MockTransport
        )

    async def send_text(self, to: str, text: str) -> None:
        """Отправляет текст получателю `to` (номер в любом формате).

        Длинные тексты бьёт на части по лимиту WhatsApp. Бросает BirdError
        при любой неудаче — вызывающий код решает, логировать сбой (ответ
        клиенту) или просто пропустить (уведомление владельцу).
        """
        text = text or ""
        if not text:
            return
        for i in range(0, len(text), WHATSAPP_TEXT_LIMIT):
            await self._send_text_chunk(receiver=normalize_phone(to), body=text[i:i + WHATSAPP_TEXT_LIMIT])

    async def close(self) -> None:
        await self._client.aclose()

    # --- внутреннее -----------------------------------------------------------

    async def _send_text_chunk(self, receiver: str, body: str) -> None:
        """POST /v1/whatsapp/messages: один получатель, один кусок текста.

        Bird отвечает 202 Accepted (доставка асинхронная), поэтому успех =
        200/202. Разовый сбой (5xx) и лимит (429) — один повтор с паузой:
        постоянные сбои сюда не спрятать, их видно в логах.
        """
        payload = {"from": self._sender, "to": receiver, "text": {"body": body}}
        headers = {"Idempotency-Key": str(uuid.uuid4())}

        response = await self._post(payload, headers)
        if response.status_code == 429:
            delay = _retry_after_seconds(response, RATE_LIMIT_MAX_RETRY_SEC)
            logger.warning("Bird вернул 429 — повторяю отправку через %.1f сек", delay)
            await asyncio.sleep(delay)
            response = await self._post(payload, headers)
        elif response.status_code >= 500:
            logger.warning(
                "Bird вернул статус %d — повторяю отправку через %d сек",
                response.status_code, SERVER_ERROR_RETRY_DELAY_SEC,
            )
            await asyncio.sleep(SERVER_ERROR_RETRY_DELAY_SEC)
            response = await self._post(payload, headers)

        if response.status_code not in (200, 202):
            raise BirdError(
                f"Bird API вернул статус {response.status_code}: {response.text[:200]}"
            )

    async def _post(self, payload: dict, headers: dict) -> httpx.Response:
        try:
            return await self._client.post(
                "/v1/whatsapp/messages", json=payload, headers=headers
            )
        except httpx.TimeoutException as exc:
            raise BirdTimeout(f"Bird не ответил за {SEND_TIMEOUT_SEC} сек") from exc
        except httpx.HTTPError as exc:  # DNS, обрыв соединения и т.п.
            raise BirdError(f"Сетевая ошибка при вызове Bird API: {exc}") from exc


def _retry_after_seconds(response: httpx.Response, cap: float) -> float:
    """Retry-After из ответа Bird (в секундах), ограниченный сверху потолком."""
    raw = response.headers.get("Retry-After", "")
    try:
        return min(max(float(raw), 0.0), cap)
    except ValueError:
        return 1.0
