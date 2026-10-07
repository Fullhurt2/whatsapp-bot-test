"""Админ-API: правка клиентских конфигов clients/*.yaml без деплоя и SSH.

Роли (без БД и сессий — токен в заголовке каждого запроса):
  - X-Admin-Token (env ADMIN_TOKEN) — полный доступ: список, чтение, запись,
    удаление любых клиентов;
  - X-Client-Token — персональный токен клиента (поле management_token в его
    yaml): доступ только к своему конфигу и только к бизнес-полям.

Правила записи:
  - перед записью конфиг прогоняется через тот же валидатор, что ест реестр
    (config.clients.validate_tenant_config) — битый файл на диск не попадёт;
  - запись атомарная (tmp + rename); предыдущая версия уходит в
    clients/.history/<pid>/<UTC-время>.yaml (откат возможен всегда);
  - каждое изменение — строка в clients/.audit.jsonl: кто, когда и какие
    поля поменял (значения токенов в лог никогда не попадают);
  - после записи реестр перечитывается штатным hot-reload.

Секреты (access_token, management_token, telegram_bot_token,
telegram_webhook_secret) в GET-ответах маскируются ("EAAY…ab12"); пустое или
замаскированное значение в PUT означает «оставить прежнее» — токен невозможно
затереть случайно.

Провайдер клиента (provider: wa|tg|zernio) задаётся при создании и не
меняется: смена транспорта означает другого клиента. Для Telegram-клиента
при сохранении генерируется telegram_webhook_secret и выполняется
best-effort setWebhook на PUBLIC_BASE_URL/webhooks/telegram/<bot_id>.

Профиль WhatsApp-номера (тексты рядом с именем и аватар) не хранится в
clients/*.yaml: /admin/clients/{pid}/profile читает и правит его напрямую
у провайдера — у Meta через Graph API (токеном клиента), у Zernio через его
API (accountId клиента + общий ZERNIO_API_KEY). Правится тем же токеном, что
и конфиг, — клиент может менять профиль своего номера, админ — любого. У
Telegram-клиентов такого прямого доступа нет (409).

Безопасность:
  - management_token хранится в YAML в виде SHA-256 хэша (constant-time compare)
  - Rate limit: 10 неверных токенов за 10 минут с одного IP -> 429
  - CSP заголовки на /admin
  - Токены маскируются в логах
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import File, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

from config.clients import is_valid_client_id, validate_tenant_config
from config.settings import Settings
from storage import (
    list_conversations,
    get_message,
    get_messages,
    create_conversation,
    get_conversation,
    get_conversation_by_client_and_phone,
    update_conversation_status,
    increment_unread,
    mark_read,
    add_message,
)

from whatsapp.meta_client import ABOUT_MAX_LENGTH, MetaWhatsAppClient
from whatsapp.errors import MessagingError, MessagingTimeout
from whatsapp.telegram_client import TelegramClient, TelegramError, TelegramTimeout, webhook_url
from whatsapp.telegram_token import bot_id_from_token
from whatsapp.zernio_client import ZernioApiClient, ZernioWhatsAppClient

logger = logging.getLogger(__name__)

HEADER_ADMIN = "X-Admin-Token"
HEADER_CLIENT = "X-Client-Token"

# Минимальная длина админ-токена для продакшена (иначе warning при старте).
ADMIN_TOKEN_MIN_LENGTH = 32

# Секретные поля: маскируются в GET, пустое/замаскированное значение в PUT
# означает «оставить прежнее».
SECRET_FIELDS = (
    "access_token",
    "management_token",
    "telegram_bot_token",
    "telegram_webhook_secret",
)

# Поля, которые клиент с management_token менять не может (только админ):
# его собственный транспорт (провайдер и токены) и параметры LLM (лимит расходов).
RESTRICTED_FIELDS = (
    "provider",
    "access_token",
    "management_token",
    "telegram_bot_token",
    "telegram_webhook_secret",
    "zernio_account_id",
    "owner_template_name",
    "owner_template_language",
    "llm",
)

# Поля, доступные клиенту для правки у себя (бизнес-конфиг).
CLIENT_EDITABLE_FIELDS = (
    "business_name",
    "tone",
    "language",
    "knowledge_base",
    "owner_whatsapp_phone",
    "owner_telegram_chat_id",
    "fallback_triggers",
    "style_examples",
    "fallback_reply_ru",
    "fallback_reply_kk",
    "timeout_reply_ru",
    "timeout_reply_kk",
    "media",
    "features",
)

HISTORY_DIR = ".history"
AUDIT_FILE = ".audit.jsonl"

MAX_BODY_BYTES = 256 * 1024  # база знаний бывает большой, но не безграничной

# --- профиль WhatsApp (живёт в Meta, а не в clients/*.yaml) ------------------

# Поля, которые панель показывает в ответе GET профиля.
PROFILE_RESPONSE_FIELDS = ("about", "description", "email", "websites", "address",
                           "vertical", "photo_url")

# Поля, которые PATCH умеет применять (display name через API не меняется).
PROFILE_WRITABLE_FIELDS = ("about", "description", "email", "websites", "address",
                         "vertical")

# Лимит Meta на «Описание» (символы). Проверяется и на бэкенде, и в панели.
DESCRIPTION_MAX_LENGTH = 512

# Ссылок на сайте Meta принимает не больше двух.
PROFILE_WEBSITES_MAX = 2

# Категория бизнеса (vertical) — закрытый список Meta, он не выдумывается:
# https://developers.facebook.com/docs/whatsapp/cloud-api/reference/business-profiles
PROFILE_VERTICALS = (
    "UNDEFINED", "OTHER", "AUTO", "BEAUTY", "APPAREL", "EDU", "ENTERTAIN",
    "EVENT_PLAN", "FINANCE", "GOVT", "GROCERY", "HEALTH", "HOTEL", "NONPROFIT",
    "ONLINE_GAMBLING", "OTC_DRUGS", "PHYSICAL_GAMBLING", "PROF_SERVICES",
    "RESTAURANT", "RETAIL", "TRAVEL", "ALCOHOL",
)

# Аватар: jpg/png/webp до 5 МБ, файл нигде у нас не остаётся.
PROFILE_PHOTO_MAX_BYTES = 5 * 1024 * 1024
PROFILE_PHOTO_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

_PAGE_PATH = Path(__file__).resolve().parent / "static" / "index.html"

_client_write_lock = threading.Lock()

# --- вспомогательные ----------------------------------------------------------


def tokens_equal(provided: str, expected: str) -> bool:
    """Constant-time сравнение токенов; пустые строки не проходят никогда."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(
        provided.strip().encode("utf-8"), expected.strip().encode("utf-8")
    )


def mask_secret(value: str) -> str:
    """Маска секрета для ответов API: "EAAY…ab12" (значение не раскрывается)."""
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) <= 12:
        return "••••"
    return f"{value[:4]}…{value[-4:]}"


# --- Безопасность: хэширование токенов, rate limiting, CSP -----------------------


# Префикс для обозначения SHA-256 хэша (чтобы не путать с plaintext токенами)
TOKEN_HASH_PREFIX = "sha256:"
TOKEN_PBKDF2_PREFIX = "pbkdf2:sha256:"


def hash_token(token: str) -> str:
    """PBKDF2-HMAC-SHA256 хэш токена с солью для хранения в YAML."""
    salt = secrets.token_hex(16)
    iterations = 100_000
    h = hashlib.pbkdf2_hmac("sha256", token.strip().encode("utf-8"), salt.encode("utf-8"), iterations).hex()
    return f"{TOKEN_PBKDF2_PREFIX}{iterations}${salt}${h}"


def verify_token_hash(provided: str, stored_hash: str) -> bool:
    """Constant-time проверка токена против хранящегося значения.
    
    Поддерживает форматы сохранённого токена:
    - `pbkdf2:sha256:<iter>$<salt>$<hex>` — безопасный хэш с солью
    - `sha256:<hex>` — устаревший SHA-256 хэш (обратная совместимость)
    - Plaintext — прямое сравнение байт (constant-time)
    """
    if not provided or not stored_hash:
        return False
    
    stored = stored_hash.strip()
    provided = provided.strip()
    
    # 1. PBKDF2 хэш с солью
    if stored.startswith(TOKEN_PBKDF2_PREFIX):
        try:
            parts = stored.split("$")
            if len(parts) == 3:
                iterations = int(parts[0].split(":")[-1])
                salt = parts[1]
                expected_hash = parts[2]
                computed = hashlib.pbkdf2_hmac(
                    "sha256", provided.encode("utf-8"), salt.encode("utf-8"), iterations
                ).hex()
                return hmac.compare_digest(computed.encode("utf-8"), expected_hash.encode("utf-8"))
        except Exception:
            return False

    # 2. Устаревший unsalted sha256:<hex> хэш
    if stored.startswith(TOKEN_HASH_PREFIX):
        provided_hash = TOKEN_HASH_PREFIX + hashlib.sha256(provided.encode("utf-8")).hexdigest()
        return hmac.compare_digest(provided_hash.encode("utf-8"), stored.encode("utf-8"))
    
    # 3. Plaintext — сравниваем байты напрямую в constant-time
    return hmac.compare_digest(provided.encode("utf-8"), stored.encode("utf-8"))


def mask_token_in_log(token: str) -> str:
    """Маскировка токена для логов (показываем только первые/последние 4 символа)."""
    token = str(token or "").strip()
    if not token:
        return "<empty>"
    if len(token) <= 8:
        return "****"
    return f"{token[:4]}****{token[-4:]}"


# In-memory rate limiter: IP -> список timestamp неудачных попыток
_failed_auth: dict[str, list[float]] = defaultdict(list)
RATE_LIMIT_WINDOW_SEC = 600  # 10 минут
RATE_LIMIT_MAX_ATTEMPTS = 10
_MAX_RATE_LIMIT_ENTRIES = 2000


def check_rate_limit(ip: str) -> tuple[bool, int]:
    """Проверка rate limit для IP.
    Возвращает (allowed, retry_after_seconds).
    """
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SEC
    # Очищаем старые записи
    timestamps = [ts for ts in _failed_auth.get(ip, []) if ts > window_start]
    if timestamps:
        _failed_auth[ip] = timestamps
    else:
        _failed_auth.pop(ip, None)
        return True, 0

    if len(timestamps) >= RATE_LIMIT_MAX_ATTEMPTS:
        oldest = timestamps[0]
        retry_after = int(oldest + RATE_LIMIT_WINDOW_SEC - now) + 1
        return False, max(retry_after, 1)
    return True, 0


def record_failed_auth(ip: str) -> None:
    """Записать неудачную попытку аутентификации с защитой от переполнения памяти."""
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SEC

    # Если в словаре скопилось много адресов (сканирование ботнетом), очищаем устаревшие
    if len(_failed_auth) >= _MAX_RATE_LIMIT_ENTRIES:
        stale_ips = [k for k, v in _failed_auth.items() if not v or v[-1] <= window_start]
        for k in stale_ips:
            _failed_auth.pop(k, None)
        # Если всё ещё превышает лимит, удаляем старейшие
        if len(_failed_auth) >= _MAX_RATE_LIMIT_ENTRIES:
            sorted_ips = sorted(_failed_auth.keys(), key=lambda k: _failed_auth[k][-1] if _failed_auth[k] else 0)
            for k in sorted_ips[: _MAX_RATE_LIMIT_ENTRIES // 2]:
                _failed_auth.pop(k, None)

    _failed_auth[ip].append(now)


def get_client_ip(request: Request) -> str:
    """Получить IP клиента с защитой от спуфинга.
    
    Если TRUST_PROXY выключен (0/false) — доверяем только прямому соединению (request.client.host).
    Если TRUST_PROXY включен (по умолчанию 1) — извлекаем IP из доверенных заголовков:
    CF-Connecting-IP (Cloudflare) или X-Forwarded-For.
    """
    trust_proxy = os.getenv("TRUST_PROXY", "1").strip().lower() not in ("0", "false", "no")
    if not trust_proxy:
        return request.client.host if request.client else "unknown"

    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    real_ip = request.headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            if os.getenv("RAILWAY_ENVIRONMENT"):
                return parts[-1]
            return parts[0]
    return request.client.host if request.client else "unknown"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware для rate limiting на админ-эндпоинты."""
    
    async def dispatch(self, request: Request, call_next):
        # Применяем только к /admin/*
        if request.url.path.startswith("/admin"):
            ip = get_client_ip(request)
            
            allowed, retry_after = check_rate_limit(ip)
            if not allowed:
                return JSONResponse(
                    status_code=429,
                    content={"error": "Слишком много неверных попыток, попробуйте позже"},
                    headers={"Retry-After": str(retry_after)},
                )
            
            response = await call_next(request)
            
            # Записываем неудачную попытку только если клиент пытался аутентифицироваться,
            # чтобы не блокировать браузер при обычном неавторизованном GET /admin/whoami.
            is_login_path = request.url.path in ("/admin/token", "/admin/login")
            has_auth = bool(
                request.headers.get(HEADER_ADMIN)
                or request.headers.get(HEADER_CLIENT)
                or request.headers.get("Authorization")
                or request.query_params.get("admin_token")
                or request.query_params.get("client_token")
            )
            auth_failed = getattr(request.state, "auth_failed", False)
            if auth_failed and (response.status_code in (401, 403) or is_login_path):
                record_failed_auth(ip)
            
            return response
        
        return await call_next(request)


class CSPMiddleware(BaseHTTPMiddleware):
    """Middleware для добавления CSP заголовков на /admin."""
    
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        
        if request.url.path.startswith("/admin"):
            # CSP: разрешаем только same-origin, inline scripts/styles для админки
            csp = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https:; "
                "font-src 'self'; "
                "connect-src 'self'; "
                "frame-ancestors 'none'; "
                "base-uri 'self'; "
                "form-action 'self'"
            )
            response.headers["Content-Security-Policy"] = csp
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        
        return response


def mask_secret(value: str) -> str:
    """Маска секрета для ответов API: "EAAY…ab12" (значение не раскрывается)."""
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) <= 12:
        return "••••"
    return f"{value[:4]}…{value[-4:]}"


def _client_yaml_path(clients_dir: Path, pid: str) -> Path | None:
    """Текущий файл клиента (<pid>.yaml; .yml тоже находим)."""
    for suffix in (".yaml", ".yml"):
        candidate = clients_dir / f"{pid}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _client_pids(clients_dir: Path) -> list[str]:
    """Ключи всех клиентов из папки clients/ (имя файла без расширения)."""
    try:
        names = sorted(os.listdir(clients_dir))
    except OSError:
        return []
    return [
        name.rsplit(".", 1)[0]
        for name in names
        if name.lower().endswith((".yaml", ".yml")) and not name.startswith("_")
    ]


def _read_cfg(path: Path) -> dict | None:
    """Содержимое yaml-файла как словарь; None — битый/нечитаемый."""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    return cfg if isinstance(cfg, dict) else None


def _management_token_of(clients_dir: Path, pid: str) -> str | None:
    """management_token из текущего yaml клиента (None — не задан/файла нет).

    Читаем файл напрямую, а не из реестра: так токен актуален сразу после
    правки конфига, без ожидания перечитки.
    Токен хранится в виде SHA-256 хэша. Здесь возвращаем хэш для сравнения.
    """
    path = _client_yaml_path(clients_dir, pid)
    if path is None:
        return None
    cfg = _read_cfg(path)
    value = str((cfg or {}).get("management_token") or "").strip()
    return value or None


def _client_provider(clients_dir: Path, pid: str) -> str:
    """Провайдер клиента из его yaml: "wa" (по умолчанию), "tg" или "zernio"."""
    path = _client_yaml_path(clients_dir, pid)
    cfg = _read_cfg(path) if path else None
    return str((cfg or {}).get("provider") or "wa").strip().lower() or "wa"


async def _setup_telegram_webhook(
    settings: Settings, bot_id: str, bot_token: str, secret: str,
) -> list[str]:
    """Привязывает вебхук Telegram-клиента (best-effort) и возвращает предупреждения.

    Сохранение конфига не должно падать из-за недоступного Telegram, поэтому
    все проблемы возвращаются строками-предупреждениями: панель покажет их
    рядом с «Готово». Без PUBLIC_BASE_URL привязать вебхук нечем — подсказываем
    готовый адрес для ручного setWebhook.
    """
    base = settings.public_base_url
    target = webhook_url(base, bot_id)
    if not base:
        return [
            "PUBLIC_BASE_URL не задан — вебхук Telegram не привязан автоматически. "
            f"Вызовите setWebhook вручную на адрес {target} "
            "(или задайте PUBLIC_BASE_URL и сохраните ещё раз)."
        ]

    notes: list[str] = []
    client = TelegramClient(bot_token)
    try:
        try:
            me = await client.get_me()
            token_bot_id = str(me.get("id") or "").strip()
            if token_bot_id and token_bot_id != bot_id:
                notes.append(
                    f"Токен принадлежит боту {token_bot_id}, а ключ клиента — {bot_id}. "
                    "Пересоздайте клиента с правильным токеном."
                )
        except (TelegramError, TelegramTimeout) as exc:
            notes.append(f"Не удалось проверить токен бота: {exc}")
        try:
            await client.set_webhook(target, secret)
            logger.info("Telegram вебхук привязан: %s", target)
        except (TelegramError, TelegramTimeout) as exc:
            notes.append(
                f"Не удалось привязать вебхук Telegram ({target}): {exc}. "
                "Проверьте, что адрес доступен из интернета по HTTPS."
            )
    finally:
        await client.close()
    return notes


def _authorize(settings: Settings, request: Request, clients_dir: Path,
               pid: str | None) -> tuple[str | None, int | None]:
    """(роль, статус ошибки): 'admin' | 'client' | (None, 401|403).

    401 — заголовков с токеном нет вовсе; 403 — токен был, но не подошёл
    (не раскрываем, какая именно из проверок провалилась).
    """
    bearer_token = ""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        bearer_token = auth[7:].strip()

    # Query-параметр ?token= допустим исключительно для скачивания медиафайлов
    # (HTML-теги <img> и <audio> в браузере не могут передавать заголовки)
    req_path = getattr(getattr(request, "url", None), "path", "") or ""
    media_token = request.query_params.get("token", "") if req_path.endswith("/media") else ""

    admin_header = (
        request.headers.get(HEADER_ADMIN, "")
        or bearer_token
        or request.query_params.get("admin_token", "")
        or media_token
    )
    client_header = (
        request.headers.get(HEADER_CLIENT, "")
        or bearer_token
        or request.query_params.get("client_token", "")
        or media_token
    )
    if settings.admin_token and tokens_equal(admin_header, settings.admin_token):
        if hasattr(request, "state"):
            request.state.auth_failed = False
        return "admin", None
    if pid and client_header:
        expected_hash = _management_token_of(clients_dir, pid)
        if expected_hash and verify_token_hash(client_header, expected_hash):
            if hasattr(request, "state"):
                request.state.auth_failed = False
            return "client", None
    if hasattr(request, "state"):
        request.state.auth_failed = True
    if admin_header or client_header:
        return None, 403
    return None, 401



def _atomic_write(path: Path, cfg: dict) -> None:
    """Запись конфига через временный файл — обрыв не оставит битый yaml."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def _backup(path: Path, clients_dir: Path, pid: str) -> Path | None:
    """Сохраняет предыдущую версию файла в clients/.history/<pid>/."""
    if not path.exists():
        return None
    dest_dir = clients_dir / HISTORY_DIR / pid
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    dest = dest_dir / f"{stamp}.yaml"
    try:
        shutil.copy2(path, dest)
        return dest
    except OSError:
        logger.exception("Не удалось сохранить бэкап %s", path)
        return None


def _audit(clients_dir: Path, actor: str, action: str, pid: str,
           old_cfg: dict | None, new_cfg: dict | None) -> None:
    """Строка в .audit.jsonl: кто/когда/какие поля. Значения не пишем —
    только имена полей, чтобы токены и бизнес-данные не оседали в логах."""
    old = old_cfg or {}
    fresh = new_cfg or {}
    changed = sorted(
        key for key in set(old) | set(fresh)
        if old.get(key) != fresh.get(key)
    )
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actor": actor,
        "action": action,
        "phone_number_id": pid,
        "changed": changed,
    }
    try:
        with open(clients_dir / AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        # Аудит не должен ломать саму операцию — но факт сбоя логируем.
        logger.exception("Не удалось дописать аудит-лог")


def _merge_incoming(old_cfg: dict, incoming: dict) -> dict:
    """Старый конфиг + присланное тело PUT -> итоговый dict.

    Секретные поля с пустым/замаскированным значением сохраняют прежнее
    содержимое — токен нельзя затереть по неосторожности, вернув из формы
    замаскированную маску. Вложенные словари объединяются рекурсивно,
    чтобы частичные обновления не затирали соседние поля (например, в llm, features, media).
    """
    def _deep_merge(old: dict, fresh: dict) -> dict:
        result = dict(old)
        for key, value in fresh.items():
            if key in SECRET_FIELDS:
                provided = str(value if value is not None else "").strip()
                old_value = str(old.get(key) or "")
                if provided and provided != mask_secret(old_value):
                    result[key] = provided
                # пустое/замаскированное значение = оставить прежнее
            elif isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = _deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    return _deep_merge(old_cfg, incoming)


def _incoming_differs(key: str, old_value, new_value) -> bool:
    """Прислано ли для закрытого поля по-настоящему новое значение.

    Равное текущему или его маске (форма возвращает маску как есть) и пустое
    значение изменением не считаются; всё остальное — попытка изменения.
    """
    if key in SECRET_FIELDS:
        provided = str(new_value if new_value is not None else "").strip()
        old_str = str(old_value or "")
        return provided not in ("", mask_secret(old_str), old_str)
    # llm
    return new_value not in (None, {}) and new_value != old_value


# --- профиль WhatsApp ---------------------------------------------------------


def _looks_like_email(value: str) -> bool:
    """Грубая проверка адреса: непустые части вокруг «@» и точка в домене."""
    local, _, domain = value.partition("@")
    return bool(local and domain and "." in domain and " " not in value)


def _validate_profile_fields(incoming: dict) -> tuple[dict, list[str]]:
    """Тело PATCH -> (поля для Meta, список проблем). Есть проблема — Meta не зовём.

    Пустое значение поля допустимо: так клиент очищает «о компании» или email,
    пустой список websites — убирает сайт. Смысл проверок — не дать Meta
    обрезать значение молча и показать в панели внятный русский текст.
    """
    payload: dict = {}
    problems: list[str] = []

    for key in ("about", "description", "address"):
        if key not in incoming:
            continue
        text = str(incoming[key] or "").strip()
        if key == "about" and len(text) > ABOUT_MAX_LENGTH:
            problems.append(
                f"«О компании» — максимум {ABOUT_MAX_LENGTH} символов, "
                f"сейчас {len(text)}"
            )
            continue
        if key == "description" and len(text) > DESCRIPTION_MAX_LENGTH:
            problems.append(
                f"Описание — максимум {DESCRIPTION_MAX_LENGTH} символов, "
                f"сейчас {len(text)}"
            )
            continue
        payload[key] = text

    if "vertical" in incoming:
        vertical = str(incoming["vertical"] or "").strip().upper()
        if vertical and vertical not in PROFILE_VERTICALS:
            problems.append(
                "категория бизнеса не из списка WhatsApp — выберите значение из списка"
            )
        else:
            payload["vertical"] = vertical

    if "email" in incoming:
        email = str(incoming["email"] or "").strip()
        if email and not _looks_like_email(email):
            problems.append("email выглядит неполным — проверьте адрес")
        else:
            payload["email"] = email

    if "websites" in incoming:
        raw = incoming["websites"]
        raw = [] if raw is None else raw
        if not isinstance(raw, (list, tuple)):
            problems.append("сайт — список ссылок (пустой список = убрать сайт)")
        else:
            sites = [str(site).strip() for site in raw if str(site or "").strip()]
            if len(sites) > PROFILE_WEBSITES_MAX:
                problems.append(
                    f"сайтов может быть не больше {PROFILE_WEBSITES_MAX} — "
                    "Meta принимает 1–2"
                )
            elif any(not site.startswith("https://") for site in sites):
                problems.append(
                    "ссылка на сайт должна начинаться с https:// — "
                    "например https://example.com"
                )
            else:
                payload["websites"] = sites

    return payload, problems


@asynccontextmanager
async def _profile_client(state, settings: Settings, clients_dir: Path, pid: str):
    """Клиент профиля под провайдера клиента: Meta или Zernio.

    У Meta профиль живёт в Graph API (токен WABA), у Zernio — в его API
    (accountId + общий ZERNIO_API_KEY). Оба клиента реализуют один интерфейс
    get/update_business_profile + upload_profile_photo, поэтому роуты панели
    о транспорте не знают. None — править нечем (нет токена/accountId).
    """
    if _client_provider(clients_dir, pid) == "zernio":
        async with _profile_zernio_client(state, settings, clients_dir, pid) as client:
            yield client
        return
    async with _profile_meta_client(state, settings, clients_dir, pid) as client:
        yield client


@asynccontextmanager
async def _profile_meta_client(state, settings: Settings, clients_dir: Path, pid: str):
    """Meta-клиент для правки профиля: бандл клиента или одноразовый.

    Обычный путь — sender из бандла: там уже его токен и открытые соединения.
    Бандла нет, когда конфиг клиента не прошёл валидацию, — тогда собираем
    одноразовый клиент и закрываем его на выходе. None — работать нечем:
    у номера нет токена.
    """
    await state.refresh_tenants()
    bundle = state.tenants.get(pid)
    if bundle is not None and isinstance(bundle.sender, MetaWhatsAppClient):
        yield bundle.sender
        return
    path = _client_yaml_path(clients_dir, pid)
    cfg = _read_cfg(path) if path else None
    token = str((cfg or {}).get("access_token") or "").strip() or settings.whatsapp_access_token
    if not token:
        yield None
        return
    client = _make_meta_client(state, settings, token, pid)
    try:
        yield client
    finally:
        await client.close()


@asynccontextmanager
async def _profile_zernio_client(state, settings: Settings, clients_dir: Path, pid: str):
    """Zernio-клиент для правки профиля: accountId из yaml + общий API-ключ.

    Ключ Zernio один на всю команду (ZERNIO_API_KEY), поэтому профиль правится
    даже там, где у клиента своего токена нет; править нечем только если в
    yaml не задан zernio_account_id.
    """
    path = _client_yaml_path(clients_dir, pid)
    cfg = _read_cfg(path) if path else None
    account_id = str((cfg or {}).get("zernio_account_id") or "").strip().lower()
    if not account_id:
        yield None
        return
    await state.refresh_tenants()
    bundle = state.tenants.get(account_id)
    if bundle is not None and isinstance(bundle.sender, ZernioWhatsAppClient):
        yield bundle.sender
        return
    factory = getattr(state, "sender_factory", None)
    if factory is not None:
        client = factory(replace(settings, zernio_account_id=account_id))
    else:
        client = ZernioWhatsAppClient(
            settings.zernio_api_key, account_id, base_url=settings.zernio_base_url,
        )
    try:
        yield client
    finally:
        await client.close()


def _make_meta_client(state, settings: Settings, access_token: str, pid: str) -> MetaWhatsAppClient:
    """Одноразовый Meta-клиент номера — через фабрику приложения, если она есть.

    Фабрика (state.sender_factory) уважает подмену транспорта в тестах; без
    неё (вне мультитенанта, где этот путь не встречается) — прямой клиент.
    """
    factory = getattr(state, "sender_factory", None)
    if factory is None:
        return MetaWhatsAppClient(access_token, pid, graph_version=settings.meta_graph_version)
    return factory(replace(
        settings,
        whatsapp_access_token=access_token,
        whatsapp_phone_number_id=pid,
    ))


def _profile_failure(exc: MessagingError | MessagingTimeout) -> JSONResponse:
    """Ошибка профиля наружу: 504 на таймаут, 502 на отказ API — с текстом.

    Текст показываем в панели как есть: провайдер пишет, чего именно не
    хватило (чаще всего — прав токена на whatsapp_business_management у Meta
    или accountId, недоступного этому API-ключу у Zernio).
    """
    if isinstance(exc, MessagingTimeout):
        return JSONResponse(status_code=504, content={"error": str(exc)})
    return JSONResponse(status_code=502, content={"error": str(exc)})


# --- Zernio: подключение аккаунта из панели -----------------------------------


def _zernio_api_client(state, settings: Settings) -> ZernioApiClient:
    """Клиент Zernio на уровне API-ключа (профили/аккаунты/ссылка/вебхук).

    Фабрика на state (zernio_api_factory) — точка подмены транспорта в тестах;
    без неё собираем клиент напрямую из настроек.
    """
    factory = getattr(state, "zernio_api_factory", None)
    if factory is not None:
        return factory(settings)
    return ZernioApiClient(settings.zernio_api_key, base_url=settings.zernio_base_url)



def _kb_has_question(knowledge_base: str, question: str) -> bool:
    """Есть ли уже такой вопрос в базе знаний.

    Один и тот же вопрос («Прайс») клиенты спрашивают снова и снова, и без
    проверки каждое «Ответить» дописывало бы в базу копию ответа.
    """
    needle = " ".join(str(question or "").strip().lower().split())
    if not needle:
        return False
    for line in str(knowledge_base or "").splitlines():
        if line.strip().lower().startswith("вопрос:"):
            existing = " ".join(line.strip()[len("вопрос:"):].strip().lower().split())
            if existing == needle:
                return True
    return False


def _append_answers_to_kb(knowledge_base: str, questions: list[dict], answer: str) -> str:
    """Дописывает «Вопрос: … Ответ: …» в базу знаний, минуя дубликаты.

    Уже отвеченный вопрос пропускается — иначе повторный ответ на «Прайс»
    дописывает то же самое второй раз.
    """
    text = str(knowledge_base or "").strip()
    blocks = []
    for question in questions:
        raw = str((question or {}).get("question") or "").strip()
        if not raw or _kb_has_question(text, raw):
            continue
        blocks.append("Вопрос: " + raw + "\nОтвет: " + answer)
    if not blocks:
        return text
    addition = "\n\n---\n\n".join(blocks)
    return (text + "\n\n---\n\n" + addition).strip() if text else addition


def _client_editable_cfg(clients_dir: Path, pid: str, cfg: dict,
                         fields: dict, actor: str, action: str, state) -> dict:
    """Дописывает поля в yaml клиента: бэкап -> атомарная запись -> аудит.

    Возвращает итоговый конфиг. Значения — только служебные id (без секретов),
    поэтому в аудит идут имена полей, как и везде.
    """
    path = _client_yaml_path(clients_dir, pid)
    if path is None:
        raise FileNotFoundError(pid)
    with _client_write_lock:
        latest = _read_cfg(path)
        base_dict = latest if latest is not None else cfg
        merged = dict(base_dict)
        merged.update(fields)
        _backup(path, clients_dir, pid)
        _atomic_write(path, merged)
        _audit(clients_dir, actor, action, pid, cfg, merged)
        return merged


def _validate_redirect_url(raw: str) -> str | None:
    """Абсолютный http(s) адрес возврата или None (проверяем до вызова Zernio)."""
    url = str(raw or "").strip()
    if not url.startswith(("https://", "http://")):
        return None
    return url


# --- роуты --------------------------------------------------------------------



def _parse_groups_json(raw: str) -> list | None:
    """Достаёт список групп из ответа модели.

    Модель может вернуть чистый JSON, JSON в блоке ``` или с текстом вокруг —
    все три варианта здесь приводим к списку. None — разобрать не удалось.
    """
    import json
    import re

    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.S)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if isinstance(parsed, dict):
        parsed = parsed.get("groups") or []
    if not isinstance(parsed, list):
        return None
    return parsed


def register_admin_api(app, settings: Settings, state) -> None:
    """Регистрирует админ-роуты и страницу /admin.

    Отключено (роуты не существуют -> 404), если нет ADMIN_TOKEN или сервис
    работает вне мультитенанта — админ-поверхность просто не появляется.
    """
    if not state.multitenant:
        if settings.admin_token:
            logger.warning(
                "ADMIN_TOKEN задан, но админ-API работает только в мультитенанте "
                "(clients/) — роуты отключены"
            )
        return
    if not settings.admin_token:
        logger.warning("ADMIN_TOKEN не задан — админ-API и панель /admin отключены")
        return
    if len(settings.admin_token) < ADMIN_TOKEN_MIN_LENGTH:
        logger.warning(
            "ADMIN_TOKEN короче %d символов — для продакшена сгенерируйте длиннее",
            ADMIN_TOKEN_MIN_LENGTH,
        )

    clients_dir = Path(settings.clients_dir)

    @app.get("/admin")
    async def admin_page():
        """Одна страница панели: админ видит всех, клиент — только себя."""
        try:
            html = _PAGE_PATH.read_text(encoding="utf-8")
        except OSError:
            logger.exception("Не найден admin/static/index.html")
            return PlainTextResponse("панель недоступна", status_code=503)
        return HTMLResponse(html)

    @app.get("/admin/whoami")
    async def admin_whoami(request: Request):
        """Роль токена: admin, либо pid клиента, чьим management_token он является."""
        if tokens_equal(request.headers.get(HEADER_ADMIN, ""), settings.admin_token):
            return {"role": "admin"}
        client_header = request.headers.get(HEADER_CLIENT, "")
        if client_header:
            try:
                names = sorted(os.listdir(clients_dir))
            except OSError:
                names = []
            for name in names:
                if not name.lower().endswith((".yaml", ".yml")) or name.startswith("_"):
                    continue
                # В yaml лежит SHA-256 хэш, а клиент присылает свой простой
                # пароль — сравнивать надо через verify_token_hash, иначе вход
                # по management_token не проходил никогда.
                token_hash = _management_token_of(clients_dir, Path(name).stem)
                if token_hash and verify_token_hash(client_header, token_hash):
                    return {"role": "client", "phone_number_id": Path(name).stem}
        return JSONResponse(status_code=401, content={"error": "токен не распознан"})

    @app.get("/admin/clients")
    async def admin_list(request: Request):
        """Список клиентов: валидные + пропущенные файлы с причинами (только админ)."""
        role, error = _authorize(settings, request, clients_dir, pid=None)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нужен админ-токен"})
        if role != "admin":
            return JSONResponse(status_code=403, content={"error": "только для администратора"})

        clients, skipped = [], []
        try:
            names = sorted(os.listdir(clients_dir))
        except OSError:
            names = []
        for name in names:
            if not name.lower().endswith((".yaml", ".yml")) or name.startswith("_"):
                continue
            pid = Path(name).stem
            if not is_valid_client_id(pid):
                skipped.append({"file": name, "problems": [
                    "имя файла — цифры (Meta phone_number_id / Telegram bot id) "
                    "или slug (Zernio)",
                ]})
                continue
            cfg = _read_cfg(clients_dir / name)
            if cfg is None:
                skipped.append({"file": name, "problems": ["yaml не читается или не является словарём"]})
                continue
            tenant, problems, _ = validate_tenant_config(cfg, settings, pid, name)
            if tenant is None:
                skipped.append({"file": name, "problems": problems})
                continue
            clients.append({
                "phone_number_id": pid,
                "business_name": str(cfg.get("business_name") or ""),
                "config_file": name,
                "provider": str(cfg.get("provider") or "wa").strip().lower() or "wa",
                "zernio_account_id": str(cfg.get("zernio_account_id") or "").strip(),
                "has_own_token": bool(str(cfg.get("access_token") or "").strip()),
                "has_management_token": bool(str(cfg.get("management_token") or "").strip()),
                "owner_phone": tenant.owner_phone or "",
                "owner_chat_id": tenant.owner_telegram_chat_id or "",
            })
        return {"clients": clients, "skipped": skipped}

    @app.get("/admin/clients/{pid}")
    async def get_client(pid: str, request: Request):
        """Конфиг клиента с замаскированными секретами (админ или сам клиент)."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        path = _client_yaml_path(clients_dir, pid)
        if path is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})
        cfg = _read_cfg(path)
        if cfg is None:
            return JSONResponse(
                status_code=409,
                content={"error": "yaml не читается — перепишите конфиг целиком через PUT"},
            )
        masked = {
            key: (mask_secret(value) if key in SECRET_FIELDS and isinstance(value, str) else value)
            for key, value in cfg.items()
        }
        # Часовой пояс клиента: панель показывает время в нём, а не в поясе
        # браузера (иначе менеджер видит чужое время в чате и аналитике).
        masked.setdefault("timezone", str(settings.timezone or "Asia/Almaty"))
        return {"phone_number_id": pid, "config_file": path.name, **masked}

    @app.put("/admin/clients/{pid}")
    async def put_client(pid: str, request: Request):
        """Создание/обновление конфига: валидация -> бэкап -> атомарная запись.

        Админ может всё; клиент — только бизнес-поля своего конфига. Пустое
        или замаскированное значение секрета сохраняет прежний токен.
        """
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if not is_valid_client_id(pid):
            return JSONResponse(
                status_code=400,
                content={"error": "ключ клиента — цифры (Meta/TG) или slug (Zernio)"},
            )
        raw_body = await request.body()
        if len(raw_body) > MAX_BODY_BYTES:
            return JSONResponse(status_code=413, content={"error": "тело запроса слишком большое"})
        try:
            incoming = json.loads(raw_body)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": "тело запроса должно быть JSON-объектом с полями конфига"},
            )
        if not isinstance(incoming, dict):
            return JSONResponse(status_code=400, content={"error": "ожидается JSON-объект с полями конфига"})
        incoming_pid = str(incoming.get("phone_number_id") or "").strip()
        if incoming_pid and incoming_pid != pid:
            return JSONResponse(
                status_code=400,
                content={"error": "phone_number_id в теле не совпадает с адресом запроса"},
            )

        path = _client_yaml_path(clients_dir, pid)
        old_cfg = _read_cfg(path) if path else None
        if role == "client":
            if path is None:
                return JSONResponse(status_code=404, content={"error": "клиент не найден"})
            denied = [
                key for key in RESTRICTED_FIELDS
                if key in incoming and _incoming_differs(key, (old_cfg or {}).get(key), incoming[key])
            ]
            if denied:
                return JSONResponse(
                    status_code=403,
                    content={"error": f"изменение полей {', '.join(denied)} доступно только администратору"},
                )
            # Защита лимитов медиа (daily_limit, max_audio_seconds и т.д. меняет только админ)
            if "media" in incoming and isinstance(incoming["media"], dict):
                old_media = (old_cfg or {}).get("media") or {}
                if not isinstance(old_media, dict):
                    old_media = {}
                denied_media = [
                    f"media.{k}" for k in (
                        "daily_limit", "max_audio_seconds", "max_image_mb",
                        "whisper_model", "vision_model", "transcribe_model", "describe_model",
                    )
                    if k in incoming["media"] and incoming["media"][k] != old_media.get(k)
                ]
                if denied_media:
                    return JSONResponse(
                        status_code=403,
                        content={"error": f"изменение полей {', '.join(denied_media)} доступно только администратору"},
                    )
            incoming = {key: value for key, value in incoming.items() if key in CLIENT_EDITABLE_FIELDS}

        merged = _merge_incoming(old_cfg or {}, incoming)

        # Хэшируем management_token перед сохранением (если он был передан и изменился)
        if "management_token" in merged:
            raw_token = str(merged["management_token"] or "").strip()
            if raw_token.startswith(TOKEN_HASH_PREFIX) or raw_token.startswith(TOKEN_PBKDF2_PREFIX):
                # Уже хэш: поле не меняли или прислали маску. Повторное
                # хэширование ломало бы ранее заданный токен — из-за этого
                # management_token нельзя было поменять.
                pass
            elif raw_token:
                # Любой простой пароль («nailsstudio») хэшируется в yaml с солью,
                # а вход в панели остаётся тем же, что ввёл менеджер.
                merged["management_token"] = hash_token(raw_token)
            else:
                # Пустой токен — удаляем поле
                merged.pop("management_token", None)

        # Провайдер задаёт только админ (в т.ч. при создании); у существующего
        # клиента он берётся из старого конфига. Смена транспорта = другой
        # клиент, поэтому провайдер фиксируется при создании.
        provider = str(
            incoming.get("provider") or (old_cfg or {}).get("provider") or "wa"
        ).strip().lower() or "wa"
        merged["provider"] = provider
        if provider == "tg" and not str(merged.get("telegram_webhook_secret") or "").strip():
            # Секрет вебхука генерируем сами: он нужен setWebhook и проверке
            # заголовка X-Telegram-Bot-Api-Secret-Token на входящих.
            merged["telegram_webhook_secret"] = secrets.token_urlsafe(32)

        tenant, problems, warnings = validate_tenant_config(merged, settings, pid, f"{pid}.yaml")
        if tenant is None:
            # Битый конфиг на диск не пишется — текущий файл остаётся рабочим.
            return JSONResponse(status_code=400, content={"ok": False, "problems": problems})

        if path is not None:
            _backup(path, clients_dir, pid)
        target = path or clients_dir / f"{pid}.yaml"
        _atomic_write(target, merged)
        actor = "admin" if role == "admin" else f"client:{pid}"
        _audit(clients_dir, actor, "put", pid, old_cfg, merged)
        await state.refresh_tenants()
        if provider == "tg":
            # Привязка вебхука — best-effort: её сбой не отменяет сохранение,
            # а возвращается панели предупреждением.
            warnings = list(warnings) + await _setup_telegram_webhook(
                settings, pid, tenant.telegram_bot_token, tenant.telegram_webhook_secret,
            )
        logger.info("Админ-API: конфиг %s записан (%s, провайдер=%s)", pid, role, provider)
        return {"ok": True, "phone_number_id": pid, "provider": provider, "warnings": warnings}

    @app.delete("/admin/clients/{pid}")
    async def delete_client(pid: str, request: Request):
        """Отключение клиента: бэкап + удаление yaml (только админ)."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if role != "admin":
            return JSONResponse(status_code=403, content={"error": "удаление доступно только администратору"})
        path = _client_yaml_path(clients_dir, pid)
        if path is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})
        old_cfg = _read_cfg(path)
        _backup(path, clients_dir, pid)
        path.unlink()
        _audit(clients_dir, "admin", "delete", pid, old_cfg, None)
        await state.refresh_tenants()
        return {"ok": True}

    @app.get("/admin/clients/{pid}/profile")
    async def get_client_profile(pid: str, request: Request):
        """Профиль WhatsApp-номера (Meta или Zernio) — что видно рядом с именем.

        Кэша нет: источник истины — провайдер, и панель зовёт его на каждый
        заход в редактор. Отказ провайдера уходит клиенту как 502 с его текстом.
        """
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})
        if _client_provider(clients_dir, pid) == "tg":
            return JSONResponse(
                status_code=409,
                content={"error": "профиль и аватар есть только у WhatsApp-клиентов (wa/zernio)"},
            )
        async with _profile_client(state, settings, clients_dir, pid) as profile_client:
            if profile_client is None:
                return JSONResponse(
                    status_code=409,
                    content={"error": "у клиента нет доступа для работы с профилем"},
                )
            try:
                profile = await profile_client.get_business_profile()
            except (MessagingError, MessagingTimeout) as exc:
                return _profile_failure(exc)
        return {
            key: profile.get(key, [] if key == "websites" else "")
            for key in PROFILE_RESPONSE_FIELDS
        }

    @app.patch("/admin/clients/{pid}/profile")
    async def patch_client_profile(pid: str, request: Request):
        """Текстовые поля профиля: валидация -> запись провайдеру -> аудит.

        Конфиг клиента при этом не меняется — в clients/*.yaml профиля нет,
        профиль живёт у провайдера (Meta/Zernio), и source of truth там.
        """
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})
        if _client_provider(clients_dir, pid) == "tg":
            return JSONResponse(
                status_code=409,
                content={"error": "профиль и аватар есть только у WhatsApp-клиентов (wa/zernio)"},
            )
        raw_body = await request.body()
        if len(raw_body) > MAX_BODY_BYTES:
            return JSONResponse(status_code=413, content={"error": "тело запроса слишком большое"})
        try:
            incoming = json.loads(raw_body)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": "тело запроса должно быть JSON-объектом с полями профиля"},
            )
        if not isinstance(incoming, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "ожидается JSON-объект с полями профиля"},
            )
        unknown = sorted(key for key in incoming if key not in PROFILE_WRITABLE_FIELDS)
        if unknown:
            return JSONResponse(
                status_code=400,
                content={"error": f"поля не меняются через API: {', '.join(unknown)}"},
            )
        payload, problems = _validate_profile_fields(incoming)
        if problems:
            return JSONResponse(
                status_code=400, content={"error": "проверьте поля", "problems": problems},
            )
        if not payload:
            return JSONResponse(
                status_code=400, content={"error": "нечего менять — пришлите хотя бы одно поле"},
            )
        async with _profile_client(state, settings, clients_dir, pid) as profile_client:
            if profile_client is None:
                return JSONResponse(
                    status_code=409,
                    content={"error": "у клиента нет доступа для работы с профилем"},
                )
            try:
                await profile_client.update_business_profile(payload)
            except (MessagingError, MessagingTimeout) as exc:
                return _profile_failure(exc)
        actor = "admin" if role == "admin" else f"client:{pid}"
        _audit(clients_dir, actor, "profile", pid, {}, payload)
        logger.info("Админ-API: профиль %s обновлён (%s): %s", pid, role, ", ".join(sorted(payload)))
        return {"ok": True, "changed": sorted(payload)}

    @app.post("/admin/clients/{pid}/profile/photo")
    async def upload_client_profile_photo(pid: str, request: Request,
                                          file: UploadFile = File(...)):
        """Аватар номера: файл уходит провайдеру и здесь нигде не остаётся.

        Расширение берём из content-type, а не из имени файла: в имени может
        быть что угодно, а провайдер ждёт имя с .jpg/.png/.webp.
        """
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})
        if _client_provider(clients_dir, pid) == "tg":
            return JSONResponse(
                status_code=409,
                content={"error": "профиль и аватар есть только у WhatsApp-клиентов (wa/zernio)"},
            )
        content_type = (file.content_type or "").lower()
        suffix = PROFILE_PHOTO_TYPES.get(content_type)
        if suffix is None:
            return JSONResponse(
                status_code=400,
                content={"error": "аватар — файл jpg, png или webp"},
            )
        content = await file.read(PROFILE_PHOTO_MAX_BYTES + 1)
        if len(content) > PROFILE_PHOTO_MAX_BYTES:
            return JSONResponse(
                status_code=413, content={"error": "файл больше 5 МБ — уменьшите размер"},
            )
        if not content:
            return JSONResponse(status_code=400, content={"error": "файл пустой"})
        async with _profile_client(state, settings, clients_dir, pid) as profile_client:
            if profile_client is None:
                return JSONResponse(
                    status_code=409,
                    content={"error": "у клиента нет доступа для работы с профилем"},
                )
            try:
                await profile_client.upload_profile_photo(
                    f"whatsapp-profile{suffix}", content, content_type,
                )
            except (MessagingError, MessagingTimeout) as exc:
                return _profile_failure(exc)
        actor = "admin" if role == "admin" else f"client:{pid}"
        _audit(clients_dir, actor, "profile_photo", pid, {}, {"photo": "avatar"})
        logger.info("Админ-API: аватар %s обновлён (%s)", pid, role)
        return {"ok": True}

    # --- Zernio: подключение аккаунта без скриптов --------------------------

    def _zernio_guard(pid: str, request: Request):
        """Общая проверка для Zernio-роутов клиента: (role, cfg, ошибка-ответ)."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return None, None, JSONResponse(status_code=error, content={"error": "нет доступа"})
        path = _client_yaml_path(clients_dir, pid)
        if path is None:
            return None, None, JSONResponse(status_code=404, content={"error": "клиент не найден"})
        if _client_provider(clients_dir, pid) != "zernio":
            return None, None, JSONResponse(
                status_code=409,
                content={"error": "подключение Zernio доступно только клиентам provider: zernio"},
            )
        if not settings.zernio_api_key:
            return None, None, JSONResponse(
                status_code=409,
                content={"error": "не задан ZERNIO_API_KEY в .env"},
            )
        return role, _read_cfg(path) or {}, None

    @app.post("/admin/clients/{pid}/zernio/connect-link")
    async def zernio_connect_link(pid: str, request: Request):
        """Ссылка Embedded Signup для клиента: профиль создаётся сам при нужде.

        Тело: {"redirect_url": "..."} — куда вернуть клиента (обычно наш
        /connect/done). Профиль Zernio клиента берём из yaml (zernio_profile_id),
        если его нет — создаём и сохраняем. Возвращаем authUrl: отправьте его
        клиенту; после подключения нажмите «Проверить подключение».
        """
        role, cfg, error = _zernio_guard(pid, request)
        if error is not None:
            return error
        try:
            incoming = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON"})
        if not isinstance(incoming, dict):
            return JSONResponse(status_code=400, content={"error": "ожидается JSON-объект"})
        redirect_url = _validate_redirect_url(incoming.get("redirect_url") or "")
        if not redirect_url:
            redirect_url = _validate_redirect_url(
                f"{settings.public_base_url}/connect/done" if settings.public_base_url else ""
            )
        if not redirect_url:
            return JSONResponse(status_code=400, content={"error": (
                "не задан redirect_url и PUBLIC_BASE_URL — некуда вернуть клиента "
                "после подключения")})

        client = _zernio_api_client(state, settings)
        try:
            profile_id = str(cfg.get("zernio_profile_id") or "").strip()
            if not profile_id:
                name = str(cfg.get("business_name") or "").strip() or pid
                profile = await client.create_profile(name)
                profile_id = str(profile.get("_id") or "").strip()
                if not profile_id:
                    return JSONResponse(status_code=502, content={
                        "error": "Zernio не вернул id созданного профиля"})
                actor = "admin" if role == "admin" else f"client:{pid}"
                cfg = _client_editable_cfg(
                    clients_dir, pid, cfg, {"zernio_profile_id": profile_id},
                    actor, "zernio_profile", state,
                )
                await state.refresh_tenants()
            auth_url = await client.whatsapp_connect_url(
                profile_id, redirect_url,
                onboarding="api",
                brand_name=str(cfg.get("business_name") or "").strip(),
            )
        except (MessagingError, MessagingTimeout) as exc:
            return _profile_failure(exc)
        finally:
            await client.close()
        if not auth_url:
            return JSONResponse(status_code=502, content={"error": "Zernio не вернул ссылку подключения"})
        return {"ok": True, "authUrl": auth_url, "profileId": profile_id,
                "redirectUrl": redirect_url}

    @app.post("/admin/clients/{pid}/zernio/sync-account")
    async def zernio_sync_account(pid: str, request: Request):
        """Сохраняет accountId клиента из Zernio после подключения по ссылке.

        Смотрит аккаунты профиля клиента (zernio_profile_id) и, если там ровно
        один WhatsApp-аккаунт, пишет его _id в yaml как zernio_account_id.
        """
        role, cfg, error = _zernio_guard(pid, request)
        if error is not None:
            return error
        profile_id = str(cfg.get("zernio_profile_id") or "").strip()
        if not profile_id:
            return JSONResponse(status_code=409, content={"error": (
                "у клиента нет zernio_profile_id — сначала сгенерируйте ссылку "
                "подключения")})
        client = _zernio_api_client(state, settings)
        try:
            accounts = await client.list_accounts(profile_id)
        except (MessagingError, MessagingTimeout) as exc:
            return _profile_failure(exc)
        finally:
            await client.close()
        whatsapp = [
            a for a in accounts
            if str(a.get("platform") or "").lower() == "whatsapp" and a.get("_id")
        ]
        if not whatsapp:
            return JSONResponse(status_code=409, content={"error": (
                "в этом профиле Zernio пока нет подключённого WhatsApp-номера — "
                "отправьте клиенту ссылку и повторите после подключения")})
        if len(whatsapp) > 1:
            return JSONResponse(status_code=409, content={"error": (
                "в профиле больше одного WhatsApp-аккаунта — оставьте один "
                "или впишите zernio_account_id вручную")})
        account_id = str(whatsapp[0]["_id"]).strip().lower()
        actor = "admin" if role == "admin" else f"client:{pid}"
        _client_editable_cfg(
            clients_dir, pid, cfg, {"zernio_account_id": account_id},
            actor, "zernio_account", state,
        )
        await state.refresh_tenants()
        logger.info("Админ-API: Zernio-аккаунт %s привязан к %s", account_id, pid)
        return {"ok": True, "accountId": account_id,
                "username": str(whatsapp[0].get("username") or "")}

    @app.get("/admin/clients/{pid}/zernio/number-info")
    async def zernio_number_info(pid: str, request: Request):
        """Живой статус номера из Meta (подключён / зарегистрирован).

        Помогает отличить «номера нет в аккаунте» от «номер ждёт регистрации»
        (status не CONNECTED, name_status PENDING_REVIEW и т.п.).
        """
        role, cfg, error = _zernio_guard(pid, request)
        if error is not None:
            return error
        if not str(cfg.get("zernio_account_id") or "").strip():
            return JSONResponse(status_code=409, content={"error": (
                "номер ещё не подключён — сначала сгенерируйте ссылку и нажмите "
                "«Проверить и сохранить»")})
        async with _profile_zernio_client(state, settings, clients_dir, pid) as client:
            if client is None:
                return JSONResponse(status_code=409, content={"error": "нет доступа к Zernio"})
            try:
                info = await client.get_number_info()
            except (MessagingError, MessagingTimeout) as exc:
                return _profile_failure(exc)
        phone = info.get("phone") if isinstance(info.get("phone"), dict) else {}
        waba = info.get("waba") if isinstance(info.get("waba"), dict) else {}
        return {
            "ok": True,
            "status": str(phone.get("status") or ""),
            "displayPhoneNumber": str(phone.get("display_phone_number") or ""),
            "verifiedName": str(phone.get("verified_name") or ""),
            "nameStatus": str(phone.get("name_status") or ""),
            "qualityRating": str(phone.get("quality_rating") or ""),
            "messagingLimitTier": str(phone.get("messaging_limit_tier") or ""),
            "platformType": str(phone.get("platform_type") or ""),
            "wabaName": str(waba.get("name") or "") if waba else "",
        }

    @app.post("/admin/clients/{pid}/zernio/register-number")
    async def zernio_register_number(pid: str, request: Request):
        """Регистрирует номер в Cloud API (шестизначный PIN).

        Нужно, если у номера свой two-step PIN: без этого он висит «На
        рассмотрении» и отправка падает с (#200) / error 133005. Тело:
        {"pin": "123456"}; пустой pin — дефолтная регистрация Zernio.
        """
        role, cfg, error = _zernio_guard(pid, request)
        if error is not None:
            return error
        if not str(cfg.get("zernio_account_id") or "").strip():
            return JSONResponse(status_code=409, content={"error": (
                "номер ещё не подключён — сначала сгенерируйте ссылку и нажмите "
                "«Проверить и сохранить»")})
        try:
            incoming = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON"})
        pin = str((incoming or {}).get("pin") or "").strip() if isinstance(incoming, dict) else ""
        if pin and not re.fullmatch(r"\d{6}", pin):
            return JSONResponse(status_code=400, content={"error": "PIN — ровно 6 цифр"})
        async with _profile_zernio_client(state, settings, clients_dir, pid) as client:
            if client is None:
                return JSONResponse(status_code=409, content={"error": "нет доступа к Zernio"})
            try:
                result = await client.register_number(pin)
            except (MessagingError, MessagingTimeout) as exc:
                return _profile_failure(exc)
        actor = "admin" if role == "admin" else f"client:{pid}"
        _audit(clients_dir, actor, "zernio_register", pid, {}, {"registered": True})
        logger.info("Админ-API: номер %s зарегистрирован в Cloud API", pid)
        return {"ok": True, "registered": bool(result.get("registered")),
                "phoneNumberId": str(result.get("phoneNumberId") or "")}

    @app.get("/admin/clients/{pid}/zernio/accounts")
    async def zernio_profile_accounts(pid: str, request: Request):
        """Диагностика: что реально лежит в профиле Zernio у этого клиента.

        Если в панели «номер не появился», здесь видно, подключён ли аккаунт и
        активен ли он. Пустой список — клиент не довёл Embedded Signup до конца.
        """
        role, cfg, error = _zernio_guard(pid, request)
        if error is not None:
            return error
        profile_id = str(cfg.get("zernio_profile_id") or "").strip()
        client = _zernio_api_client(state, settings)
        try:
            accounts = await client.list_accounts(profile_id) if profile_id else []
        except (MessagingError, MessagingTimeout) as exc:
            return _profile_failure(exc)
        finally:
            await client.close()
        return {"ok": True, "profileId": profile_id, "accounts": [
            {"accountId": str(a.get("_id") or ""), "platform": str(a.get("platform") or ""),
             "username": str(a.get("username") or ""), "isActive": bool(a.get("isActive"))}
            for a in accounts
        ]}

    @app.post("/admin/zernio/register-webhook")
    async def zernio_register_webhook(request: Request):
        """Регистрирует вебхук сервиса на /webhooks/zernio (только админ).

        Секрет берётся из ZERNIO_WEBHOOK_SECRET (env) — тот же, которым бот
        проверяет подпись. Нужен ZERNIO_WEBHOOK_SECRET и PUBLIC_BASE_URL.
        """
        role, error = _authorize(settings, request, clients_dir, pid=None)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нужен админ-токен"})
        if role != "admin":
            return JSONResponse(status_code=403, content={"error": "только для администратора"})
        if not settings.zernio_api_key:
            return JSONResponse(status_code=409, content={"error": "не задан ZERNIO_API_KEY в .env"})
        if not settings.zernio_webhook_secret:
            return JSONResponse(status_code=409, content={"error": (
                "не задан ZERNIO_WEBHOOK_SECRET в .env — задайте и перезапустите сервис")})
        if not settings.public_base_url:
            return JSONResponse(status_code=409, content={"error": (
                "не задан PUBLIC_BASE_URL в .env — вебхук должен указывать на "
                "публичный адрес сервиса")})
        target = f"{settings.public_base_url}/webhooks/zernio"
        client = _zernio_api_client(state, settings)
        try:
            webhook = await client.create_webhook(
                "whatsapp-bot", target, settings.zernio_webhook_secret,
                ["message.received", "message.sent", "message.failed"],
            )
        except (MessagingError, MessagingTimeout) as exc:
            return _profile_failure(exc)
        finally:
            await client.close()
        logger.info("Админ-API: вебхук Zernio зарегистрирован на %s", target)
        return {"ok": True, "url": target,
                "webhookId": str(webhook.get("_id") or "")}

    @app.get("/admin/clients/{pid}/conversations")
    async def get_conversations(pid: str, request: Request,
                                status: str | None = None,
                                q: str | None = None,
                                cursor: str | None = None,
                                since: str | None = None,
                                limit: int = 50):
        """Список диалогов клиента с фильтрами и пагинацией."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})

        # Фильтры
        if status and status not in ("bot", "manual"):
            return JSONResponse(status_code=400, content={"error": "status должен быть 'bot' или 'manual'"})
        if limit < 1 or limit > 100:
            limit = 50

        convs = list_conversations(_conversation_client_key(clients_dir, pid), status=status, q=q, cursor=cursor, since=since, limit=limit)
        return {"conversations": convs, "count": len(convs)}


    @app.get("/admin/clients/{pid}/conversations/{cid}/messages")
    async def get_conversation_messages(pid: str, cid: str, request: Request,
                                        before: str | None = None,
                                        limit: int = 50):
        """История сообщений диалога (новые -> старые)."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        conv = get_conversation(cid)
        if not _check_conv_ownership(conv, _conversation_client_key(clients_dir, pid)):
            return JSONResponse(status_code=404, content={"error": "диалог не найден"})

        if limit < 1 or limit > 200:
            limit = 50

        msgs = get_messages(cid, before=before, limit=limit)
        return {"messages": msgs, "count": len(msgs)}

    @app.get("/admin/clients/{pid}/conversations/{cid}/messages/{mid}/media")
    async def get_message_media(pid: str, cid: str, mid: int, request: Request):
        """Отдать медиафайл сообщения (аудио/фото)."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        conv = get_conversation(cid)
        if not _check_conv_ownership(conv, _conversation_client_key(clients_dir, pid)):
            return JSONResponse(status_code=404, content={"error": "диалог не найден"})

        msg = get_message(mid)
        if not msg or msg.get("conversation_id") != cid:
            return JSONResponse(status_code=404, content={"error": "сообщение не найдено"})

        media_path = msg.get("media_path")
        media_status = msg.get("media_status")
        if media_status == "expired" or not media_path:
            return JSONResponse(status_code=404, content={"error": "Файл удалён по истечении срока хранения"})

        if not os.path.exists(media_path):
            return JSONResponse(status_code=404, content={"error": "Файл не найден на сервере"})

        # Защита от Path Traversal: файл должен находиться внутри settings.media_dir (или временной папки в тестах)
        try:
            import tempfile
            resolved_file = Path(media_path).resolve()
            resolved_base = Path(settings.media_dir).resolve()
            resolved_tmp = Path(tempfile.gettempdir()).resolve()
            is_in_media = resolved_file.is_relative_to(resolved_base) if hasattr(resolved_file, "is_relative_to") else str(resolved_file).startswith(str(resolved_base))
            is_in_tmp = resolved_file.is_relative_to(resolved_tmp) if hasattr(resolved_file, "is_relative_to") else str(resolved_file).startswith(str(resolved_tmp))
            if not (is_in_media or is_in_tmp):
                logger.warning("Попытка Path Traversal через media_path: %s", media_path)
                return JSONResponse(status_code=403, content={"error": "нет доступа"})
        except Exception:
            return JSONResponse(status_code=403, content={"error": "нет доступа"})

        # MIME whitelist: только audio/* и image/*, опасные типы (html, svg, js) отдаются octet-stream
        mime = str(msg.get("media_mime") or "application/octet-stream").strip().lower()
        if mime == "image/svg+xml" or not (mime.startswith("audio/") or mime.startswith("image/")):
            mime = "application/octet-stream"

        return FileResponse(
            media_path,
            media_type=mime,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.post("/admin/clients/{pid}/conversations/{cid}/messages/{mid}/retry-media")
    async def retry_message_media(pid: str, cid: str, mid: int, request: Request):
        """Повторить расшифровку медиафайла при media_status == 'failed'."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        conv = get_conversation(cid)
        if not _check_conv_ownership(conv, _conversation_client_key(clients_dir, pid)):
            return JSONResponse(status_code=404, content={"error": "диалог не найден"})

        msg = get_message(mid)
        if not msg or msg.get("conversation_id") != cid:
            return JSONResponse(status_code=404, content={"error": "сообщение не найдено"})

        media_path = msg.get("media_path")
        if not media_path or not os.path.exists(media_path):
            return JSONResponse(status_code=404, content={"error": "Исходный файл не найден на сервере"})

        db_key = _conversation_client_key(clients_dir, pid)
        bundle = state.tenants.get(db_key) or state.tenants.get(pid)
        t_settings = bundle.settings if bundle else settings

        content_kind = msg.get("content_kind")
        from services.media import transcribe_audio, describe_image
        with open(media_path, "rb") as f:
            data = f.read()

        try:
            if content_kind in ("voice", "audio"):
                hint = f"{t_settings.business_name}. {t_settings.knowledge_base[:300]}"
                res = await transcribe_audio(
                    data=data,
                    mime=msg.get("media_mime") or "audio/ogg",
                    language=t_settings.language,
                    hint=hint,
                    settings=t_settings,
                    retry=False,
                )
                text = f"[Голосовое сообщение] {res.text.strip()}"
            elif content_kind in ("image", "photo"):
                res = await describe_image(
                    data=data,
                    mime=msg.get("media_mime") or "image/jpeg",
                    caption="",
                    business_name=t_settings.business_name,
                    settings=t_settings,
                    retry=False,
                )
                text = f"[Фото] Описание: {res.text.strip()}"
            else:
                return JSONResponse(status_code=400, content={"error": "Неподдерживаемый тип медиа для расшифровки"})

            from storage.db import execute
            execute(
                """
                UPDATE messages
                SET text = ?, media_status = 'ok', media_duration_s = ?, media_model = ?, media_cost = ?
                WHERE id = ?
                """,
                (text, res.duration_s, res.model, res.cost, mid),
            )
            return {"ok": True, "text": text}
        except Exception as exc:
            logger.exception("Ошибка повторной расшифровки медиа (%d)", mid)
            return JSONResponse(status_code=502, content={"error": f"Ошибка расшифровки: {exc}"})

    @app.post("/admin/clients/{pid}/conversations/{cid}/messages")
    async def send_message_in_conversation(pid: str, cid: str, request: Request):

        """Отправить сообщение от менеджера в диалог.
        Тело: {"text": "...", "idempotency_key": "..."}
        Возвращает 409 window_closed, если 24-часовое окно закрыто.
        """
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        conv = get_conversation(cid)
        if not _check_conv_ownership(conv, _conversation_client_key(clients_dir, pid)):
            return JSONResponse(status_code=404, content={"error": "диалог не найден"})

        # Проверка 24-часового окна (только для WhatsApp)
        provider = _client_provider(clients_dir, pid)
        if provider in ("wa", "zernio"):
            from storage import get_last_client_message_at
            last_client = get_last_client_message_at(cid)
            if last_client:
                from datetime import datetime, timedelta, timezone
                last_dt = datetime.fromisoformat(last_client.replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - last_dt > timedelta(hours=24):
                    return JSONResponse(
                        status_code=409,
                        content={"error": "window_closed", "message": "24-часовое окно закрыто, используйте шаблон"}
                    )

        try:
            incoming = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON"})
        if not isinstance(incoming, dict):
            return JSONResponse(status_code=400, content={"error": "ожидается JSON-объект"})
        text = str(incoming.get("text") or "").strip()
        if not text:
            return JSONResponse(status_code=400, content={"error": "текст сообщения обязателен"})
        idempotency_key = str(incoming.get("idempotency_key") or "").strip()

        sender = await _conversation_client(state, settings, clients_dir, pid)
        if sender is None:
            return JSONResponse(status_code=409, content={"error": "нет транспорта для отправки"})

        conversation_id = conv.get("zernio_conversation_id", "")
        if provider == "zernio" and not conversation_id:
            return JSONResponse(status_code=409, content={"error": "нет conversation_id для Zernio"})

        # Дедупликация по idempotency_key
        if idempotency_key:
            from storage import get_message_by_provider_id
            existing = get_message_by_provider_id(idempotency_key)
            if existing and existing.get("conversation_id") == cid:
                return {"ok": True, "delivered": True, "deduplicated": True, "message_id": existing.get("id")}

        # Отправляем сообщение
        try:
            if provider == "zernio":
                await sender.send_text(conversation_id, text, conversation_id=conversation_id)
            else:
                # Meta - отправка по номеру
                await sender.send_text(conv["contact_phone"], text)
        except MessagingError as exc:
            return JSONResponse(status_code=502, content={"error": str(exc)})

        # Сохраняем сообщение роли human в БД (сбой БД после успешной отправки не должен давать 500)
        try:
            add_message(
                conversation_id=cid,
                role="human",
                text=text,
                content_kind="text",
                provider_message_id=idempotency_key,
                delivery_status="sent",
            )

            # Обновляем last_message_at и last_human_message_at
            from storage import update_last_message_times
            update_last_message_times(cid, is_client=False, is_human=True)

            # Если был открытый handoff — отмечаем first_human_reply
            from storage import get_open_handoff, mark_first_human_reply
            open_handoff = get_open_handoff(cid)
            if open_handoff:
                mark_first_human_reply(open_handoff["id"])
        except Exception:
            logger.exception("Сообщение доставлено клиенту (%s), но произошла ошибка сохранения в БД", cid)

        return {"ok": True, "delivered": True}


    @app.post("/admin/clients/{pid}/conversations/{cid}/mode")
    async def set_conversation_mode(pid: str, cid: str, request: Request):
        """Переключить режим диалога: 'bot' или 'manual'."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        conv = get_conversation(cid)
        if not _check_conv_ownership(conv, _conversation_client_key(clients_dir, pid)):
            return JSONResponse(status_code=404, content={"error": "диалог не найден"})

        try:
            incoming = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON"})
        if not isinstance(incoming, dict):
            return JSONResponse(status_code=400, content={"error": "ожидается JSON-объект"})
        mode = str(incoming.get("mode") or "").strip()
        if mode not in ("bot", "manual"):
            return JSONResponse(status_code=400, content={"error": "mode должен быть 'bot' или 'manual'"})

        update_conversation_status(cid, mode)

        # Если переключаем на bot — сбрасываем unread_count и закрываем висящие handoffs
        if mode == "bot":
            from storage import mark_read, resolve_conversation_handoffs
            mark_read(cid)
            resolve_conversation_handoffs(cid)

        return {"ok": True, "conversation_id": cid, "mode": mode}


    @app.post("/admin/clients/{pid}/conversations/{cid}/read")
    async def mark_conversation_read(pid: str, cid: str, request: Request):
        """Сбросить счётчик непрочитанных сообщений."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        conv = get_conversation(cid)
        if not _check_conv_ownership(conv, _conversation_client_key(clients_dir, pid)):
            return JSONResponse(status_code=404, content={"error": "диалог не найден"})
        from storage import mark_read
        mark_read(cid)
        return {"ok": True}


    # --- Telegram-привязка для уведомлений -------------------------------------------


    @app.post("/admin/clients/{pid}/telegram/link-code")
    async def tg_link_code(pid: str, request: Request):
        """Создать одноразовый код привязки Telegram (на 15 минут)."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})

        from storage import MAX_BINDINGS_PER_CLIENT, count_bindings, create_link_code
        db_key = _conversation_client_key(clients_dir, pid)
        bot_username = os.getenv("TELEGRAM_OWNER_BOT_USERNAME", "").strip().lstrip("@")
        link = create_link_code(db_key, bot_username=bot_username)
        return {
            "ok": True,
            "code": link["code"],
            "url": link["url"],
            "expires_at": link["expires_at"],
            "bot_username": bot_username,
            "bound": count_bindings(db_key),
            "max_bindings": MAX_BINDINGS_PER_CLIENT,
        }


    @app.get("/admin/clients/{pid}/telegram")
    async def tg_bindings_list(pid: str, request: Request):
        """Список привязанных Telegram-чатов."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        from storage import MAX_BINDINGS_PER_CLIENT, get_tg_bindings
        db_key = _conversation_client_key(clients_dir, pid)
        return {
            "bindings": get_tg_bindings(db_key),
            "max_bindings": MAX_BINDINGS_PER_CLIENT,
            "owner_bot_configured": bool(settings.telegram_owner_bot_token),
        }


    @app.delete("/admin/clients/{pid}/telegram/{binding_id}")
    async def tg_binding_remove(pid: str, binding_id: int, request: Request):
        """Отвязать Telegram-чат."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        from storage import remove_tg_binding
        if not remove_tg_binding(_conversation_client_key(clients_dir, pid), binding_id):
            return JSONResponse(status_code=404, content={"error": "привязка не найдена"})
        return {"ok": True}


    # --- Вопросы без ответа -----------------------------------------------------------


    @app.get("/admin/clients/{pid}/unanswered/{group_id}/questions")
    async def unanswered_group_questions(pid: str, group_id: int, request: Request):
        """Тексты вопросов внутри группы — их показывает панель перед ответом."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        from storage import get_questions_for_group, get_unanswered_group
        db_key = _conversation_client_key(clients_dir, pid)
        group = get_unanswered_group(group_id)
        if not group or group.get("client_key") != db_key:
            return JSONResponse(status_code=404, content={"error": "группа не найдена"})
        questions = get_questions_for_group(group_id)
        return {
            "questions": [{
                "id": q["id"],
                "question": q["question"],
                "status": q["status"],
                "answer_text": q.get("answer_text") or "",
                "created_at": q.get("created_at") or "",
            } for q in questions]
        }


    @app.get("/admin/conversations/{cid}/client")
    async def conversation_client(request: Request, cid: str, pid: str | None = None):
        """К какому клиенту относится диалог — нужно для перехода по ссылке.

        Ссылка из уведомления Telegram приходит как /admin#chat=<id> без указания
        клиента, поэтому панель спрашивает, чей это диалог, и открывает нужную
        карточку сама.
        """
        from storage import get_conversation
        conv = get_conversation(cid)
        if not conv:
            return JSONResponse(status_code=404, content={"error": "диалог не найден"})
        db_key = str(conv.get("client_key") or "")

        target_pid = pid
        if not target_pid:
            for stem in _client_pids(clients_dir):
                if _conversation_client_key(clients_dir, stem) == db_key:
                    target_pid = stem
                    break

        role, error = _authorize(settings, request, clients_dir, target_pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})

        if not target_pid:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})

        # Если вошёл клиент по своему токену, проверяем, что диалог принадлежит ему
        if role != "admin":
            expected_key = _conversation_client_key(clients_dir, target_pid)
            if db_key != expected_key:
                return JSONResponse(status_code=404, content={"error": "диалог не найден"})

        return {"pid": target_pid, "conversation_id": cid}


    @app.get("/admin/clients/{pid}/unanswered")
    async def get_unanswered(pid: str, request: Request, status: str | None = None):
        """Список групп вопросов без ответа."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        from storage import list_unanswered_groups, list_ungrouped_questions
        db_key = _conversation_client_key(clients_dir, pid)
        groups = list_unanswered_groups(db_key, status=status)
        return {
            "groups": groups,
            # Вопросы без группы видны сразу — их можно закрыть, не запуская
            # LLM-группировку.
            "ungrouped": list_ungrouped_questions(db_key, status=status or None),
        }


    @app.post("/admin/clients/{pid}/unanswered/question/{question_id}/answer")
    async def answer_unanswered_question(pid: str, question_id: int, request: Request):
        """Ответ на один несгруппированный вопрос: дописывает его в базу знаний."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        try:
            incoming = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON"})
        if not isinstance(incoming, dict):
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON-объектом"})
        answer = str(incoming.get("answer") or "").strip()
        if not answer:
            return JSONResponse(status_code=400, content={"error": "ответ не может быть пустым"})
        if len(answer) > 20000:
            return JSONResponse(status_code=400, content={"error": "ответ слишком длинный (максимум 20 000 символов)"})

        from storage import get_unanswered_question, mark_question_answered
        db_key = _conversation_client_key(clients_dir, pid)
        question = get_unanswered_question(question_id)
        if not question or question.get("client_key") != db_key:
            return JSONResponse(status_code=404, content={"error": "вопрос не найден"})

        path = _client_yaml_path(clients_dir, pid)
        cfg = _read_cfg(path)
        if cfg is None:
            return JSONResponse(status_code=409, content={"error": "не удалось прочитать конфигурацию клиента"})
        old_kb = str(cfg.get("knowledge_base") or "").strip()
        new_kb = _append_answers_to_kb(old_kb, [question], answer)
        _client_editable_cfg(clients_dir, pid, cfg, {"knowledge_base": new_kb},
                             "admin" if role == "admin" else f"client:{pid}",
                             "unanswered_answer", state)
        mark_question_answered(question_id, answer)
        await state.refresh_tenants()
        return {"ok": True, "updated_kb": True}


    @app.post("/admin/clients/{pid}/unanswered/{group_id}/answer")
    async def answer_unanswered(pid: str, group_id: int, request: Request):
        """Добавить ответ в базу знаний и закрыть группу вопросов."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        try:
            incoming = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON"})
        if not isinstance(incoming, dict):
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON-объектом"})
        answer = str(incoming.get("answer") or "").strip()
        if not answer:
            return JSONResponse(status_code=400, content={"error": "ответ не может быть пустым"})
        if len(answer) > 20000:
            return JSONResponse(status_code=400, content={"error": "ответ слишком длинный (максимум 20 000 символов)"})

        from storage import get_unanswered_group, get_questions_for_group, mark_question_answered, update_group_status, create_conversation
        group = get_unanswered_group(group_id)
        if not group or group.get("client_key") != _conversation_client_key(clients_dir, pid):
            return JSONResponse(status_code=404, content={"error": "группа не найдена"})

        # Дописываем «Вопрос: … Ответ: …» в knowledge_base
        path = _client_yaml_path(clients_dir, pid)
        cfg = _read_cfg(path)
        if cfg is None:
            return JSONResponse(status_code=409, content={"error": "не удалось прочитать конфигурацию клиента"})
        old_kb = str(cfg.get("knowledge_base") or "").strip()
        questions = get_questions_for_group(group_id)
        new_kb = _append_answers_to_kb(old_kb, questions, answer)

        _client_editable_cfg(clients_dir, pid, cfg, {"knowledge_base": new_kb},
                             "admin" if role == "admin" else f"client:{pid}", "unanswered_answer", state)

        # Отмечаем вопросы отвеченными
        for q in questions:
            mark_question_answered(q["id"], answer)
        update_group_status(group_id, "answered")

        await state.refresh_tenants()
        return {"ok": True, "updated_kb": True}


    @app.post("/admin/clients/{pid}/unanswered/question/{question_id}/ignore")
    async def ignore_unanswered_question(pid: str, question_id: int, request: Request):
        """Скрыть (игнорировать) один несгруппированный вопрос."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        from storage import get_unanswered_question, mark_question_ignored
        db_key = _conversation_client_key(clients_dir, pid)
        question = get_unanswered_question(question_id)
        if not question or question.get("client_key") != db_key:
            return JSONResponse(status_code=404, content={"error": "вопрос не найден"})
        mark_question_ignored(question_id)
        return {"ok": True}


    @app.post("/admin/clients/{pid}/unanswered/{group_id}/ignore")
    async def ignore_unanswered(pid: str, group_id: int, request: Request):
        """Скрыть группу вопросов."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        from storage import get_unanswered_group, update_group_status
        from storage.db import execute
        db_key = _conversation_client_key(clients_dir, pid)
        group = get_unanswered_group(group_id)
        if not group or group.get("client_key") != db_key:
            return JSONResponse(status_code=404, content={"error": "группа не найдена"})
        update_group_status(group_id, "ignored")
        execute("UPDATE unanswered_questions SET status = 'ignored' WHERE group_id = ? AND status = 'new'", (group_id,))
        return {"ok": True}


    @app.post("/admin/clients/{pid}/unanswered/regroup")
    async def regroup_unanswered(pid: str, request: Request):
        """Пересчитать группы похожих вопросов (LLM)."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})

        # Получаем последние необработанные вопросы
        from storage import get_recent_unanswered_for_regroup, get_last_regroup_time, create_unanswered_group
        db_key = _conversation_client_key(clients_dir, pid)

        # Ограничение частоты перегруппировки для не-админов (cooldown 10 минут)
        if role != "admin":
            last_time = get_last_regroup_time(db_key)
            if last_time:
                from datetime import datetime, timezone
                try:
                    dt_last = datetime.fromisoformat(last_time.replace("Z", "+00:00"))
                    if dt_last.tzinfo is None:
                        dt_last = dt_last.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - dt_last).total_seconds()
                    if elapsed < 600:
                        remaining = int(600 - elapsed)
                        return JSONResponse(
                            status_code=429,
                            content={"error": f"Группировка уже выполнялась недавно. Повторите через {remaining} сек."},
                        )
                except Exception:
                    pass

        questions = get_recent_unanswered_for_regroup(db_key, limit=200)
        if len(questions) < 2:
            return {"ok": True, "groups_created": 0, "message": "недостаточно вопросов для группировки"}

        # Промпт для LLM группировки
        system_prompt = (
            "Ты — помощник для группировки похожих вопросов клиентов. "
            "Дан список вопросов. Сгруппируй их по смыслу: вопросы об одной и той же теме "
            "(например, цена, время работы, запись на маникюр) должны быть в одной группе. "
            "Верни JSON: список групп, где каждая группа — объект с полями "
            "'name' (краткое название темы) и 'question_ids' (массив id вопросов). "
            "Не придумывай вопросы, используй только те, что даны. Вопросы, которые не "
            "подходят ни к какой группе, не включай."
        )
        user_prompt = "Вопросы:\n" + "\n".join(f"{q['id']}: {q['question']}" for q in questions)

        # Используем LLM клиента для группировки. Параметры — настоящий
        # LLMParams: самодельный объект без reasoning_effort ронял клиент.
        from config.settings import LLMParams
        from services.llm_client import LLMClient

        client_settings = _read_cfg(_client_yaml_path(clients_dir, pid)) or {}
        llm_params = client_settings.get("llm") or {}
        llm = LLMClient(
            settings.llm_api_url,
            settings.llm_api_key,
            LLMParams(
                model=str(llm_params.get("model") or settings.llm.model),
                temperature=float(llm_params.get("temperature", 0.3)),
                max_tokens=int(llm_params.get("max_tokens", 2000)),
                timeout_seconds=int(llm_params.get("timeout_seconds", 30)),
                reasoning_effort=llm_params.get("reasoning_effort"),
            ),
        )

        try:
            response = await llm.chat(system_prompt, user_prompt, history=[])
        except Exception as exc:
            logger.exception("Группировка вопросов: LLM не ответил")
            return JSONResponse(status_code=502, content={"error": f"ошибка LLM: {exc}"})
        finally:
            await llm.close()

        groups = _parse_groups_json(response)
        if groups is None:
            return JSONResponse(
                status_code=502,
                content={"error": "LLM вернул нечитаемый ответ вместо списка групп"},
            )

        # Создаём группы в БД
        created = 0
        for g in groups:
            if not isinstance(g, dict):
                continue
            name = str(g.get("name") or "").strip()
            raw_ids = g.get("question_ids")
            if not isinstance(raw_ids, list):
                continue
            ids = [int(x) for x in raw_ids if str(x).isdigit()]
            if name and ids:
                create_unanswered_group(db_key, name, ids)
                created += 1

        return {"ok": True, "groups_created": created, "groups": groups}


    # --- Аналитика -------------------------------------------------------------------


    @app.get("/admin/clients/{pid}/stats")
    async def get_client_stats(pid: str, request: Request,
                               from_date: str | None = None,
                               to_date: str | None = None):
        """Показатели клиента за период."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})

        from storage import parse_date_range, get_client_stats
        from_date, to_date = parse_date_range(from_date, to_date)
        client_tz = str((_read_cfg(_client_yaml_path(clients_dir, pid)) or {}).get("timezone")
                        or settings.timezone or "Asia/Almaty")
        stats = get_client_stats(_conversation_client_key(clients_dir, pid),
                                 from_date, to_date, timezone=client_tz)
        return stats


    @app.get("/admin/clients/{pid}/stats/export.csv")
    async def export_client_stats(pid: str, request: Request,
                                  from_date: str | None = None,
                                  to_date: str | None = None):
        """Экспорт статистики в CSV (UTF-8 с BOM для Excel)."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        from storage import parse_date_range, export_stats_csv
        from_date, to_date = parse_date_range(from_date, to_date)
        csv_data = export_stats_csv(_conversation_client_key(clients_dir, pid), from_date, to_date)
        return PlainTextResponse(
            csv_data,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="stats-{pid}-{from_date[:10]}-{to_date[:10]}.csv"'}
        )


    @app.get("/admin/stats/overview")
    async def admin_stats_overview(request: Request,
                                   from_date: str | None = None,
                                   to_date: str | None = None):
        """Сводка по всем клиентам и расходы (только админ)."""
        role, error = _authorize(settings, request, clients_dir, pid=None)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нужен админ-токен"})
        if role != "admin":
            return JSONResponse(status_code=403, content={"error": "только для администратора"})

        from storage import parse_date_range, get_admin_overview_stats
        from_date, to_date = parse_date_range(from_date, to_date)
        client_map = {
            stem: _conversation_client_key(clients_dir, stem)
            for stem in _client_pids(clients_dir)
        }
        stats = get_admin_overview_stats(from_date, to_date, client_map)
        return stats



# --- Живой чат: диалоги и сообщения ---------------------------------------------


def _conversation_client_key(clients_dir: Path, pid: str) -> str:
    """Ключ диалогов клиента в БД.

    Обработчик пишет диалоги с client_key = whatsapp_phone_number_id тенанта:
    у wa/tg это ключ из имени файла, у zernio — accountId из yaml. Панель
    знает клиента по имени файла (pid), поэтому для zernio ключ надо
    разрешить в accountId — иначе диалоги в живом чате не видны.
    """
    if _client_provider(clients_dir, pid) == "zernio":
        cfg = _read_cfg(_client_yaml_path(clients_dir, pid)) or {}
        return str(cfg.get("zernio_account_id") or "").strip() or pid
    return pid


async def _conversation_client(state, settings: Settings, clients_dir: Path, pid: str):
    """Sender клиента для отправки сообщений в диалог (wa / zernio / tg)."""
    key = _conversation_client_key(clients_dir, pid)
    if not key:
        return None
    await state.refresh_tenants()
    bundle = state.tenants.get(key)
    return bundle.sender if bundle else None


def _check_conv_ownership(conv, db_key: str) -> bool:
    """Проверка, что диалог принадлежит клиенту (по ключу диалогов в БД)."""
    return conv and conv.get("client_key") == db_key


