"""Клиент WhatsApp Cloud API (Graph API) — прямой транспорт Meta.

Документация: https://developers.facebook.com/docs/whatsapp/cloud-api
Отправка: POST https://graph.facebook.com/{версия}/{phone_number_id}/messages
с заголовком Authorization: Bearer <доступ-токен>.

Сервисные сообщения (ответы на входящие в течение 24-часового окна) у Meta
бесплатны; платные — только шаблонные категории (маркетинг/утилиты/авторизация),
которые этот бот не использует.

Задача модуля — изолировать HTTP-детали Graph API: хендлеры вызывают только
send_text() и не знают про версии API и коды ответов.

Профиль бизнес-номера (то, что видно рядом с именем в WhatsApp: «о компании»,
контакты, аватар) живёт на том же Graph API, но на другом узле —
whatsapp_business_profile. Методы get/update_business_profile и
upload_profile_photo обслуживают его из админ-панели.
"""

import asyncio
import logging

import httpx

from config.settings import normalize_phone
from whatsapp.errors import MessagingError, MessagingTimeout

logger = logging.getLogger(__name__)

# Версия Graph API (переопределяется переменной META_GRAPH_VERSION в .env).
DEFAULT_GRAPH_VERSION = "v21.0"

# Лимит длины одного WhatsApp-текстового сообщения (ограничение Meta):
# длинные ответы режем на части, как в Zernio-клиенте.
WHATSAPP_TEXT_LIMIT = 4096

# Повтор при разовом сбое Graph API (5xx) и лимите (429).
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
RATE_LIMIT_MAX_RETRY_SEC = 5.0

# Graph API отвечает быстро (200 = сообщение принято в очередь); запас — на сеть.
SEND_TIMEOUT_SEC = 20.0
CONNECT_TIMEOUT_SEC = 10.0

# Поля профиля бизнес-номера, которые запрашиваем у Graph API. Ответ может
# содержать не все — отсутствующее трактуем как пустое. profile_picture_url
# отдаётся только для номера с уже загруженным аватаром (проверено на живом
# API: без аватара поля просто нет).
PROFILE_FIELDS = "about,description,email,websites,vertical,address,profile_picture_url"

# Лимит Meta на «о компании» (символы). Проверяется и на бэкенде, и в панели.
ABOUT_MAX_LENGTH = 139

# Метаданные multipart-тела для загрузки аватара: границу задаём сами, иначе
# клиент оставил бы свой заголовок Content-Type: application/json.
_MULTIPART_PREFIX = "----wa-profile"


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
        self._profile_endpoint = f"/{graph_version}/{phone_number_id}/whatsapp_business_profile"
        self._client = httpx.AsyncClient(
            base_url="https://graph.facebook.com",
            headers={
                "Authorization": f"Bearer {access_token}",
            },
            timeout=httpx.Timeout(SEND_TIMEOUT_SEC, connect=CONNECT_TIMEOUT_SEC),
            transport=transport,  # точка для тестов: подменяется на MockTransport
        )

    async def send_text(self, to: str, text: str, conversation_id: str = "") -> None:
        """Отправляет текст получателю `to` (номер в любом формате).

        Длинные тексты бьёт на части по лимиту WhatsApp. Бросает MetaError
        при любой неудаче — вызывающий код решает, логировать сбой (ответ
        клиенту) или просто пропустить (уведомление владельцу).
        `conversation_id` — часть общего интерфейса отправки (нужен Zernio,
        где ответ уходит в диалог); Meta адресует сообщения по номеру и
        параметр игнорирует.
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

    # --- профиль бизнес-номера -------------------------------------------------

    async def get_business_profile(self) -> dict:
        """Читает профиль номера из Meta: что сейчас видно рядом с именем.

        Возвращает нормализованный словарь (все поля строки, websites — список);
        поле, которого Meta не прислала, = "" — вызывающий код не различает
        «не заполнено» и «Meta не отдала».
        """
        response = await self._call(
            "GET", self._profile_endpoint, params={"fields": PROFILE_FIELDS},
        )
        if response.status_code != 200:
            raise MetaError(_meta_error_text(response))
        try:
            body = response.json() or {}
        except ValueError as exc:
            raise MetaError("Meta вернула нечитаемый ответ профиля") from exc
        data = body.get("data") if isinstance(body, dict) else None
        entry = data[0] if isinstance(data, list) and data else {}
        entry = entry if isinstance(entry, dict) else {}
        websites = entry.get("websites")
        websites = websites if isinstance(websites, list) else []
        return {
            "about": _as_text(entry.get("about")),
            "description": _as_text(entry.get("description")),
            "email": _as_text(entry.get("email")),
            "websites": [url for url in (_as_text(s) for s in websites) if url],
            "vertical": _as_text(entry.get("vertical")),
            "address": _as_text(entry.get("address")),
            "photo_url": _as_https_url(entry.get("profile_picture_url")),
        }

    async def update_business_profile(self, fields: dict) -> None:
        """PATCH текстовых полей профиля: about, description, email, белые сайты, address.

        Пустое значение поля допустимо — так клиент очищает его. Проверку
        форматов (длина, ссылки, email) делает вызывающий код: Meta отвечает
        обрезанным текстом ошибки, в панели нужен понятный русский текст.
        """
        payload = {key: value for key, value in (fields or {}).items() if value is not None}
        if not payload:
            return
        # Meta требует messaging_product в теле правки профиля — без него
        # запрос отклоняется и поля не применяются.
        payload["messaging_product"] = "whatsapp"
        response = await self._call("PATCH", self._profile_endpoint, json=payload)
        if response.status_code != 200:
            raise MetaError(_meta_error_text(response))

    async def upload_profile_photo(
        self, filename: str, content: bytes, content_type: str,
    ) -> None:
        """Заменяет аватар номера: multipart POST с photo и messaging_product.

        Файл уходит в Meta и у нас нигде не остаётся. Расширение в filename
        Meta обязательно (jpg/png) — вызывающий код подставляет его по типу.
        """
        response = await self._call(
            "POST",
            self._profile_endpoint,
            files={
                "photo": (filename, content, content_type),
                "messaging_product": (None, "whatsapp"),
            },
        )
        if response.status_code != 200:
            raise MetaError(_meta_error_text(response))

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

    async def _call(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Запрос к Graph API с тем же маппингом сетевых ошибок, что у отправки.

        Повторов здесь нет: правка профиля — редкая ручная операция, и молча
        переотправить PATCH после 5xx опаснее, чем показать ошибку.
        """
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise MetaTimeout(f"Meta не ответила за {SEND_TIMEOUT_SEC} сек") from exc
        except httpx.HTTPError as exc:  # DNS, обрыв соединения и т.п.
            raise MetaError(f"Сетевая ошибка при вызове Graph API: {exc}") from exc


def _as_text(value) -> str:
    """Поле профиля как строка без обрезки краёв; None/absent -> пустая строка."""
    return "" if value is None else str(value).strip()


def _as_https_url(value) -> str:
    """Ссылка на аватар, только если она https: страницу Meta отдаёт такой,
    но подстраховка от «просто http» уберегает панель от mixed-content."""
    url = _as_text(value)
    return url if url.startswith("https://") else ""


def _meta_error_text(response: httpx.Response) -> str:
    """Человеческий текст ошибки Graph API из {"error": {"message", "code"}}.

    Код добавляем, когда он есть: по нему видно, чего не хватает токену
    (например, прав на whatsapp_business_management).
    """
    fallback = response.text[:200] or f"Meta API вернул статус {response.status_code}"
    try:
        body = response.json() or {}
    except ValueError:
        return fallback
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return fallback
    message = str(error.get("message") or "").strip()
    if not message:
        return fallback
    code = error.get("code")
    return f"{message} (код {code})" if code else message


def _retry_after_seconds(response: httpx.Response, cap: float) -> float:
    """Retry-After из ответа Meta (в секундах), ограниченный сверху потолком."""
    raw = response.headers.get("Retry-After", "")
    try:
        return min(max(float(raw), 0.0), cap)
    except ValueError:
        return 1.0
