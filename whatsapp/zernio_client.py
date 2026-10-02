"""Клиент Zernio API — WhatsApp-транспорт вместо Meta Cloud API.

Документация: https://docs.zernio.com (полный справочник — other/zernio_full_llm_doc.txt)
База: https://zernio.com/api/v1, авторизация Authorization: Bearer $ZERNIO_API_KEY
(один ключ на всю команду Zernio — per-tenant токен не нужен, поэтому клиенту
достаточно знать свой accountId).

Особенность Zernio: ответ уходит не «на номер», а в диалог —
POST /inbox/conversations/{conversationId}/messages с телом {accountId, message}.
conversationId приходит во входящем вебхуке (message.received) и передаётся
сюда из InboundMessage. Свободный текст вне 24-часового окна WhatsApp
запрещён: чтобы написать первым (например, уведомить владельца), нужно
открыть диалог approved-шаблоном (send_template).

Задача модуля — изолировать HTTP-детали Zernio: хендлеры вызывают только
send_text()/send_template() и не знают про коды ответов и конверт ошибок.
"""

import asyncio
import logging
import uuid

import httpx

from config.settings import normalize_phone
from whatsapp.errors import MessagingError, MessagingTimeout

logger = logging.getLogger(__name__)

# База API Zernio (переопределяется ZERNIO_BASE_URL в .env).
DEFAULT_BASE_URL = "https://zernio.com/api/v1"

# Лимит длины одного WhatsApp-текстового сообщения (ограничение Meta):
# длинные ответы режем на части, как в Meta-клиенте.
WHATSAPP_TEXT_LIMIT = 4096

# Повтор при разовом сбое (5xx) и лимите (429).
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
RATE_LIMIT_MAX_RETRY_SEC = 5.0

SEND_TIMEOUT_SEC = 20.0
CONNECT_TIMEOUT_SEC = 10.0


class ZernioError(MessagingError):
    """Любая ошибка вызова Zernio API (сеть, HTTP-ошибка, отказ платформы)."""


class ZernioTimeout(MessagingTimeout):
    """Zernio не ответил за отведённое время."""


class ZernioWhatsAppClient:
    """Асинхронный клиент отправки WhatsApp-сообщений через Zernio.

    account_id — id подключённого WhatsApp-аккаунта в Zernio (24 hex-символа),
    он обязателен в теле каждого запроса. Реализует общий интерфейс
    send_text(to, text, conversation_id)/close(), поэтому MessageProcessor и
    notify_owner работают с ним без изменений.
    """

    def __init__(
        self,
        api_key: str,
        account_id: str,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._account_id = str(account_id or "").strip()
        self._base = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self._client = httpx.AsyncClient(
            # Content-Type не задаём: httpx сам ставит application/json для
            # json=-запросов и multipart/form-data (со своей границей) для files=.
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(SEND_TIMEOUT_SEC, connect=CONNECT_TIMEOUT_SEC),
            transport=transport,  # точка для тестов: подменяется на MockTransport
        )

    async def send_text(self, to: str, text: str, conversation_id: str = "") -> None:
        """Отправляет текст в диалог `conversation_id`.

        Zernio не умеет слать свободный текст «в никуда»: без conversation_id
        (его даёт входящий вебхук) отправка невозможна — для первого контакта
        используйте send_template(). Длинные тексты бьёт на части по лимиту
        WhatsApp; каждая часть — своя идемпотентная отправка.
        """
        text = text or ""
        if not text:
            return
        if not conversation_id:
            raise ZernioError(
                "Zernio: нельзя отправить свободный текст без conversation_id "
                "(вне 24-часового окна нужен шаблон — send_template)"
            )
        for i in range(0, len(text), WHATSAPP_TEXT_LIMIT):
            await self._send_reply(conversation_id, text[i:i + WHATSAPP_TEXT_LIMIT])

    async def send_template(
        self,
        to: str,
        template_name: str,
        language: str,
        params: list[str] | None = None,
    ) -> None:
        """Открывает диалог с номером `to` approved-шаблоном Zernio/Meta.

        Используется для уведомлений владельцу и любого первого контакта.
        `to` — номер в любом формате (уйдёт как participantId без «+»),
        params — плоский список значений переменных тела шаблона по порядку.
        """
        if not (template_name and language):
            raise ZernioError(
                "Zernio: для шаблонного сообщения нужны template_name и language"
            )
        payload = {
            "accountId": self._account_id,
            "participantId": normalize_phone(to).lstrip("+"),
            "templateName": str(template_name),
            "templateLanguage": str(language),
            "templateParams": [str(p) for p in (params or [])],
        }
        response = await self._request_with_retry(
            "POST", f"{self._base}/inbox/conversations", json=payload,
        )
        if response.status_code not in (200, 201):
            raise ZernioError(_zernio_error_text(response))

    async def close(self) -> None:
        await self._client.aclose()

    # --- профиль бизнес-номера -------------------------------------------------
    # Тот же интерфейс, что у MetaWhatsAppClient (get/update_business_profile,
    # upload_profile_photo): админ-API правит профиль одинаково для Meta и Zernio.

    async def get_business_profile(self) -> dict:
        """Читает профиль номера из Zernio: что сейчас видно рядом с именем.

        Возвращает нормализованный словарь в той же форме, что Meta-клиент
        (все поля строки, websites — список, photo_url — ссылка на аватар).
        """
        response = await self._call(
            "GET",
            f"{self._base}/whatsapp/business-profile",
            params={"accountId": self._account_id},
        )
        if response.status_code != 200:
            raise ZernioError(_zernio_error_text(response))
        try:
            body = response.json() or {}
        except ValueError as exc:
            raise ZernioError("Zernio вернул нечитаемый ответ профиля") from exc
        profile = body.get("businessProfile") if isinstance(body, dict) else None
        profile = profile if isinstance(profile, dict) else {}
        websites = profile.get("websites")
        websites = websites if isinstance(websites, list) else []
        return {
            "about": _as_text(profile.get("about")),
            "description": _as_text(profile.get("description")),
            "email": _as_text(profile.get("email")),
            "websites": [url for url in (_as_text(s) for s in websites) if url],
            "vertical": _as_text(profile.get("vertical")),
            "address": _as_text(profile.get("address")),
            "photo_url": _as_https_url(profile.get("profilePictureUrl")),
        }

    async def update_business_profile(self, fields: dict) -> None:
        """Обновляет текстовые поля профиля (только присланные).

        Проверку форматов (длина, ссылки, email) делает вызывающий код:
        Zernio/Meta отвечают обрезанным текстом ошибки, в панели нужен
        понятный русский текст.
        """
        payload = {key: value for key, value in (fields or {}).items() if value is not None}
        if not payload:
            return
        payload["accountId"] = self._account_id
        response = await self._call(
            "POST", f"{self._base}/whatsapp/business-profile", json=payload,
        )
        if response.status_code != 200:
            raise ZernioError(_zernio_error_text(response))

    async def upload_profile_photo(
        self, filename: str, content: bytes, content_type: str,
    ) -> None:
        """Заменяет аватар номера: multipart с полем file.

        Файл уходит в Zernio (и дальше в Meta) и у нас нигде не остаётся.
        Meta принимает JPEG/PNG до 5 МБ; на coexistence-номерах фото заперто
        (Zernio отвечает 422) — это ограничение самого WhatsApp Business app.
        """
        response = await self._call(
            "POST",
            f"{self._base}/whatsapp/business-profile/photo",
            data={"accountId": self._account_id},
            files={"file": (filename, content, content_type)},
        )
        if response.status_code != 200:
            raise ZernioError(_zernio_error_text(response))

    # --- внутреннее -----------------------------------------------------------

    async def _send_reply(self, conversation_id: str, body: str) -> None:
        """POST /inbox/conversations/{id}/messages: один кусок текста в диалог."""
        payload = {"accountId": self._account_id, "message": body}
        response = await self._request_with_retry(
            "POST",
            f"{self._base}/inbox/conversations/{conversation_id}/messages",
            json=payload,
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        if response.status_code != 200:
            raise ZernioError(_zernio_error_text(response))

    async def _request_with_retry(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Запрос с одним повтором на 429/5xx (Idempotency-Key делает его безопасным).

        Заголовок Idempotency-Key генерируется вызывающим и повторяется при
        повторе: Zernio вернёт исходный результат вместо второго сообщения.
        """
        response = await self._call(method, url, **kwargs)
        if response.status_code in RETRYABLE_STATUSES:
            delay = _retry_after_seconds(response, RATE_LIMIT_MAX_RETRY_SEC)
            logger.warning(
                "Zernio вернул статус %d — повторяю запрос через %.1f сек",
                response.status_code, delay,
            )
            await asyncio.sleep(delay)
            response = await self._call(method, url, **kwargs)
        return response

    async def _call(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ZernioTimeout(f"Zernio не ответил за {SEND_TIMEOUT_SEC} сек") from exc
        except httpx.HTTPError as exc:  # DNS, обрыв соединения и т.п.
            raise ZernioError(f"Сетевая ошибка при вызове Zernio API: {exc}") from exc


class ZernioApiClient:
    """Клиент Zernio на уровне API-ключа: профили, аккаунты, ссылка подключения.

    Отвечает на вопрос «до подключения аккаунта»: у клиента ещё нет accountId,
    известен только профиль (или его ещё надо создать). Используется админ-API
    (кнопки в панели) и scripts/zernio_connect.py. Отправка сообщений и профиль
    номера — в ZernioWhatsAppClient, которому нужен accountId.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = str(base_url or DEFAULT_BASE_URL).rstrip("/")
        self._client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(SEND_TIMEOUT_SEC, connect=CONNECT_TIMEOUT_SEC),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def create_profile(self, name: str) -> dict:
        """POST /profiles: создаёт профиль (группу) клиента, возвращает profile."""
        response = await self._call("POST", f"{self._base}/profiles", json={"name": name})
        if response.status_code not in (200, 201):
            raise ZernioError(_zernio_error_text(response))
        body = _json_dict(response)
        profile = body.get("profile")
        return profile if isinstance(profile, dict) else {}

    async def list_accounts(self, profile_id: str = "") -> list[dict]:
        """GET /accounts: подключённые аккаунты (можно сузить по profileId)."""
        params = {"profileId": profile_id} if profile_id else None
        response = await self._call("GET", f"{self._base}/accounts", params=params)
        if response.status_code != 200:
            raise ZernioError(_zernio_error_text(response))
        accounts = _json_dict(response).get("accounts")
        return [a for a in accounts if isinstance(a, dict)] if isinstance(accounts, list) else []

    async def whatsapp_connect_url(
        self,
        profile_id: str,
        redirect_url: str,
        *,
        onboarding: str = "api",
        hosted: bool = True,
        brand_name: str = "",
        language: str = "",
    ) -> str:
        """GET /connect/whatsapp: ссылка Embedded Signup для клиента.

        onboarding=api — только Cloud API (для бота), hosted=True — страница
        Zernio сама открывает попап Meta и запоминает выбранный номер.
        """
        params: dict[str, str] = {
            "profileId": profile_id,
            "redirect_url": redirect_url,
            "onboarding": onboarding,
        }
        if hosted:
            params["signup"] = "hosted"
            if brand_name:
                params["brandName"] = brand_name
            if language:
                params["language"] = language
        response = await self._call("GET", f"{self._base}/connect/whatsapp", params=params)
        if response.status_code != 200:
            raise ZernioError(_zernio_error_text(response))
        return str(_json_dict(response).get("authUrl") or "")

    async def create_webhook(
        self, name: str, url: str, secret: str, events: list[str],
    ) -> dict:
        """POST /webhooks/settings: вебхук на наш сервис, возвращает webhook."""
        response = await self._call("POST", f"{self._base}/webhooks/settings", json={
            "name": name, "url": url, "secret": secret, "events": events,
        })
        if response.status_code not in (200, 201):
            raise ZernioError(_zernio_error_text(response))
        webhook = _json_dict(response).get("webhook")
        return webhook if isinstance(webhook, dict) else {}

    async def _call(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            return await self._client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ZernioTimeout(f"Zernio не ответил за {SEND_TIMEOUT_SEC} сек") from exc
        except httpx.HTTPError as exc:
            raise ZernioError(f"Сетевая ошибка при вызове Zernio API: {exc}") from exc


def _json_dict(response: httpx.Response) -> dict:
    """Тело ответа как словарь; нечитаемое/не-словарь -> {}."""
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _as_text(value) -> str:
    """Поле профиля как строка без обрезки краёв; None/absent -> пустая строка."""
    return "" if value is None else str(value).strip()

def _as_https_url(value) -> str:
    """Ссылка на аватар, только если она https (панель без mixed-content)."""
    url = _as_text(value)
    return url if url.startswith("https://") else ""


def _zernio_error_text(response: httpx.Response) -> str:
    """Человеческий текст ошибки Zernio из плоского конверта.

    Конверт: {"error": "...", "type": "...", "code": "...", "platform": "meta",
    "platformError": {...}}. Показываем текст, код и платформу, когда они есть,
    — по коду видно причину (TEMPLATE_REQUIRED, PLATFORM_LIMITATION, 131056).
    """
    fallback = response.text[:200] or f"Zernio API вернул статус {response.status_code}"
    try:
        body = response.json() or {}
    except ValueError:
        return fallback
    if not isinstance(body, dict):
        return fallback
    message = str(body.get("error") or "").strip()
    if not message:
        return fallback
    code = str(body.get("code") or "").strip()
    platform = str(body.get("platform") or "").strip()
    suffix = " ".join(part for part in (
        f"(код {code})" if code else "",
        f"[{platform}]" if platform else "",
    ) if part)
    return f"{message} {suffix}".strip()


def _retry_after_seconds(response: httpx.Response, cap: float) -> float:
    """Retry-After из ответа Zernio (в секундах), ограниченный сверху потолком."""
    raw = response.headers.get("Retry-After", "")
    try:
        return min(max(float(raw), 0.0), cap)
    except ValueError:
        return 1.0
