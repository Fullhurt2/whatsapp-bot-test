"""Клиент Telegram Bot API — транспорт ответов в Telegram.

Документация: https://core.telegram.org/bots/api
Отправка: POST https://api.telegram.org/bot<token>/sendMessage
(токен бота — часть пути, отдельного заголовка авторизации нет).

Особенность Telegram: Bot API отвечает HTTP 200 даже на логические ошибки
(например, «chat not found» или бот заблокирован пользователем) — тело при
этом содержит {"ok": false, "description": ...}. Поэтому успех проверяем не
только по статусу, но и по полю ok.

Модуль изолирует HTTP-детали Bot API: хендлеры вызывают только send_text().
Регистрация вебхука (setWebhook) и проверка токена (getMe) используются
админ-панелью при сохранении Telegram-клиента.
"""

import asyncio
import logging

import httpx

from whatsapp.errors import MessagingError, MessagingTimeout

logger = logging.getLogger(__name__)

# Лимит длины одного сообщения Telegram (ограничение Bot API): длинные ответы
# режем на части, как в WhatsApp-клиентах.
TELEGRAM_TEXT_LIMIT = 4096

# Повтор при разовом сбое (5xx) и лимите (429).
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
RATE_LIMIT_MAX_RETRY_SEC = 5.0

SEND_TIMEOUT_SEC = 20.0
CONNECT_TIMEOUT_SEC = 10.0

# Обновления, которые доставляем: только обычные сообщения (edited_message,
# callback_query и прочее боту не нужны).
ALLOWED_UPDATES = ["message"]


class TelegramError(MessagingError):
    """Любая ошибка вызова Telegram Bot API (сеть, HTTP-ошибка, ok: false)."""


class TelegramTimeout(MessagingTimeout):
    """Telegram не ответил за отведённое время."""


def webhook_path(bot_id: str) -> str:
    """Путь вебхука бота (совпадает с роутом в main.py)."""
    return f"/webhooks/telegram/{bot_id}"


def webhook_url(public_base_url: str, bot_id: str) -> str:
    """Полный адрес вебхука для setWebhook из публичного базового URL."""
    return f"{str(public_base_url or '').rstrip('/')}{webhook_path(bot_id)}"


class TelegramClient:
    """Асинхронный клиент отправки сообщений Telegram. Один на клиента.

    bot_token — токен бота от @BotFather (вида 123456789:AA…). Реализует тот
    же интерфейс send_text(to, text)/close(), что и WhatsApp-клиенты, поэтому
    MessageProcessor и notify_owner работают с ним без изменений; для Telegram
    `to` — это chat_id получателя, а не номер телефона.
    """

    def __init__(
        self,
        bot_token: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = str(bot_token or "").strip()
        self._base = f"/bot{self._token}"
        self._client = httpx.AsyncClient(
            base_url="https://api.telegram.org",
            timeout=httpx.Timeout(SEND_TIMEOUT_SEC, connect=CONNECT_TIMEOUT_SEC),
            transport=transport,  # точка для тестов: подменяется на MockTransport
        )

    async def send_text(self, to: str, text: str) -> None:
        """Отправляет текст получателю `to` (chat_id в любом формате).

        Длинные тексты бьёт на части по лимиту Bot API. Бросает TelegramError
        при любой неудаче — вызывающий код решает, логировать сбой или нет.
        """
        text = text or ""
        if not text:
            return
        receiver = str(to).strip()
        for i in range(0, len(text), TELEGRAM_TEXT_LIMIT):
            await self._send_text_chunk(receiver, text[i:i + TELEGRAM_TEXT_LIMIT])

    async def close(self) -> None:
        await self._client.aclose()

    # --- регистрация вебхука и проверка токена ---------------------------------

    async def get_me(self) -> dict:
        """getMe: возвращает данные бота (id, username). Ошибка -> TelegramError.

        Используется админ-панелью, чтобы проверить токен при сохранении и
        показать имя бота; id бота должен совпадать с ключом клиента.
        """
        return await self._call("getMe", {})

    async def set_webhook(self, url: str, secret: str = "") -> None:
        """setWebhook: привязывает адрес вебхука (и секрет) к боту."""
        payload: dict = {"url": url, "allowed_updates": ALLOWED_UPDATES}
        if secret:
            payload["secret_token"] = secret
        await self._call("setWebhook", payload)

    async def delete_webhook(self) -> None:
        """deleteWebhook: снимает вебхук (нужно перед long polling / отвязкой)."""
        await self._call("deleteWebhook", {})

    # --- внутреннее -----------------------------------------------------------

    async def _send_text_chunk(self, chat_id: str, body: str) -> None:
        """POST sendMessage: один получатель, один кусок текста.

        HTTP 429/5xx — один повтор с паузой. Успех — HTTP 200 И ok: true:
        Bot API отдаёт 200 с ok: false при логических ошибках.
        """
        payload = {"chat_id": chat_id, "text": body}
        response = await self._post("sendMessage", payload)
        if response.status_code in RETRYABLE_STATUSES:
            delay = _retry_after_seconds(response, RATE_LIMIT_MAX_RETRY_SEC)
            logger.warning(
                "Telegram вернул статус %d — повторяю отправку через %.1f сек",
                response.status_code, delay,
            )
            await asyncio.sleep(delay)
            response = await self._post("sendMessage", payload)

        if _ok_result(response) is None:
            raise TelegramError(_error_text(response))

    async def _call(self, method: str, payload: dict) -> dict:
        """Вызов метода Bot API с проверкой ok; возвращает result (dict)."""
        response = await self._post(method, payload)
        result = _ok_result(response)
        if result is None:
            raise TelegramError(_error_text(response))
        return result

    async def _post(self, method: str, payload: dict) -> httpx.Response:
        try:
            return await self._client.post(f"{self._base}/{method}", json=payload)
        except httpx.TimeoutException as exc:
            raise TelegramTimeout(f"Telegram не ответил за {SEND_TIMEOUT_SEC} сек") from exc
        except httpx.HTTPError as exc:  # DNS, обрыв соединения и т.п.
            raise TelegramError(f"Сетевая ошибка при вызове Telegram API: {exc}") from exc


def _ok_result(response: httpx.Response) -> dict | None:
    """result успешного ответа Bot API или None (не 200 либо ok != true)."""
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict) or not body.get("ok"):
        return None
    result = body.get("result")
    return result if isinstance(result, dict) else {}


def _error_text(response: httpx.Response) -> str:
    """Человеческий текст ошибки Bot API из {"ok": false, "description", ...}."""
    fallback = response.text[:200] or f"Telegram API вернул статус {response.status_code}"
    try:
        body = response.json() or {}
    except ValueError:
        return fallback
    if not isinstance(body, dict):
        return fallback
    description = str(body.get("description") or "").strip()
    if not description:
        return fallback
    code = body.get("error_code")
    return f"{description} (код {code})" if code else description


def _retry_after_seconds(response: httpx.Response, cap: float) -> float:
    """Пауза перед повтором: parameters.retry_after из тела или Retry-After.

    Telegram кладёт секунды в тело (parameters.retry_after), HTTP-заголовок
    Retry-After поддерживается не всегда — проверяем оба места.
    """
    try:
        body = response.json() or {}
    except ValueError:
        body = {}
    parameters = body.get("parameters") if isinstance(body, dict) else None
    raw = parameters.get("retry_after") if isinstance(parameters, dict) else None
    if raw is None:
        raw = response.headers.get("Retry-After", "")
    try:
        return min(max(float(raw), 0.0), cap)
    except (TypeError, ValueError):
        return 1.0
