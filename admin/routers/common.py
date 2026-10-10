"""Вспомогательные утилиты, авторизация и общие константы админ-API."""

import hashlib
import hmac
import json
import logging
import os
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
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from config.clients import is_valid_client_id, validate_tenant_config
from config.settings import Settings
from whatsapp.errors import MessagingError, MessagingTimeout
from whatsapp.meta_client import ABOUT_MAX_LENGTH, MetaWhatsAppClient
from whatsapp.telegram_client import TelegramClient, TelegramError, TelegramTimeout, webhook_url
from whatsapp.zernio_client import ZernioApiClient, ZernioWhatsAppClient

logger = logging.getLogger(__name__)

HEADER_ADMIN = "X-Admin-Token"
HEADER_CLIENT = "X-Client-Token"
ADMIN_TOKEN_MIN_LENGTH = 32

SECRET_FIELDS = (
    "access_token",
    "management_token",
    "telegram_bot_token",
    "telegram_webhook_secret",
)

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
MAX_BODY_BYTES = 256 * 1024

PROFILE_RESPONSE_FIELDS = ("about", "description", "email", "websites", "address",
                           "vertical", "photo_url")
PROFILE_WRITABLE_FIELDS = ("about", "description", "email", "websites", "address",
                           "vertical")
DESCRIPTION_MAX_LENGTH = 512
PROFILE_WEBSITES_MAX = 2
PROFILE_VERTICALS = (
    "UNDEFINED", "OTHER", "AUTO", "BEAUTY", "APPAREL", "EDU", "ENTERTAIN",
    "EVENT_PLAN", "FINANCE", "GOVT", "GROCERY", "HEALTH", "HOTEL", "NONPROFIT",
    "ONLINE_GAMBLING", "OTC_DRUGS", "PHYSICAL_GAMBLING", "PROF_SERVICES",
    "RESTAURANT", "RETAIL", "TRAVEL", "ALCOHOL",
)
PROFILE_PHOTO_MAX_BYTES = 5 * 1024 * 1024
PROFILE_PHOTO_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

PAGE_PATH = Path(__file__).resolve().parent.parent / "static" / "index.html"
client_write_lock = threading.Lock()

TOKEN_HASH_PREFIX = "sha256:"
TOKEN_PBKDF2_PREFIX = "pbkdf2:sha256:"


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


def hash_token(token: str) -> str:
    """PBKDF2-HMAC-SHA256 хэш токена с солью для хранения в YAML."""
    salt = secrets.token_hex(16)
    iterations = 100_000
    h = hashlib.pbkdf2_hmac("sha256", token.strip().encode("utf-8"), salt.encode("utf-8"), iterations).hex()
    return f"{TOKEN_PBKDF2_PREFIX}{iterations}${salt}${h}"


def verify_token_hash(provided: str, stored_hash: str) -> bool:
    """Constant-time проверка токена против хранящегося значения."""
    if not provided or not stored_hash:
        return False
    
    stored = stored_hash.strip()
    provided = provided.strip()
    
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

    if stored.startswith(TOKEN_HASH_PREFIX):
        provided_hash = TOKEN_HASH_PREFIX + hashlib.sha256(provided.encode("utf-8")).hexdigest()
        return hmac.compare_digest(provided_hash.encode("utf-8"), stored.encode("utf-8"))
    
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
RATE_LIMIT_WINDOW_SEC = 600
RATE_LIMIT_MAX_ATTEMPTS = 10
_MAX_RATE_LIMIT_ENTRIES = 2000


def check_rate_limit(ip: str) -> tuple[bool, int]:
    """Проверка rate limit для IP. Возвращает (allowed, retry_after_seconds)."""
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SEC
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

    if len(_failed_auth) >= _MAX_RATE_LIMIT_ENTRIES:
        stale_ips = [k for k, v in _failed_auth.items() if not v or v[-1] <= window_start]
        for k in stale_ips:
            _failed_auth.pop(k, None)
        if len(_failed_auth) >= _MAX_RATE_LIMIT_ENTRIES:
            sorted_ips = sorted(_failed_auth.keys(), key=lambda k: _failed_auth[k][-1] if _failed_auth[k] else 0)
            for k in sorted_ips[: _MAX_RATE_LIMIT_ENTRIES // 2]:
                _failed_auth.pop(k, None)

    _failed_auth[ip].append(now)


def get_client_ip(request: Request) -> str:
    """Получить IP клиента с защитой от спуфинга."""
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
            
            is_login_path = request.url.path in ("/admin/token", "/admin/login")
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


def _client_yaml_path(clients_dir: Path, pid: str) -> Path | None:
    """Текущий файл клиента (<pid>.yaml; .yml тоже находим)."""
    pid_str = str(pid or "").strip()
    if not pid_str or any(sep in pid_str for sep in ("/", "\\", "..")):
        return None
    clean_pid = Path(pid_str).name
    if clean_pid != pid_str:
        return None
    for suffix in (".yaml", ".yml"):
        candidate = clients_dir / f"{clean_pid}{suffix}"
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
    """management_token из текущего yaml клиента (None — не задан/файла нет)."""
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


def _authorize(settings: Settings, request: Request, clients_dir: Path,
               pid: str | None) -> tuple[str | None, int | None]:
    """(роль, статус ошибки): 'admin' | 'client' | (None, 401|403)."""
    bearer_token = ""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        bearer_token = auth[7:].strip()

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
    """Запись конфига через временный файл."""
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
    """Строка в .audit.jsonl: кто/когда/какие поля."""
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
        logger.exception("Не удалось дописать аудит-лог")


def _merge_incoming(old_cfg: dict, incoming: dict) -> dict:
    """Старый конфиг + присланное тело PUT -> итоговый dict."""
    def _deep_merge(old: dict, fresh: dict) -> dict:
        result = dict(old)
        for key, value in fresh.items():
            if key in SECRET_FIELDS:
                provided = str(value if value is not None else "").strip()
                old_value = str(old.get(key) or "")
                if provided and provided != mask_secret(old_value):
                    result[key] = provided
            elif isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = _deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    return _deep_merge(old_cfg, incoming)


def _incoming_differs(key: str, old_value, new_value) -> bool:
    """Прислано ли для закрытого поля по-настоящему новое значение."""
    if key in SECRET_FIELDS:
        provided = str(new_value if new_value is not None else "").strip()
        old_str = str(old_value or "")
        return provided not in ("", mask_secret(old_str), old_str)
    return new_value not in (None, {}) and new_value != old_value


def _client_editable_cfg(clients_dir: Path, pid: str, cfg: dict,
                         fields: dict, actor: str, action: str, state) -> dict:
    """Дописывает поля в yaml клиента: бэкап -> атомарная запись -> аудит."""
    path = _client_yaml_path(clients_dir, pid)
    if path is None:
        raise FileNotFoundError(pid)
    with client_write_lock:
        latest = _read_cfg(path)
        base_dict = latest if latest is not None else cfg
        merged = dict(base_dict)
        merged.update(fields)
        _backup(path, clients_dir, pid)
        _atomic_write(path, merged)
        _audit(clients_dir, actor, action, pid, cfg, merged)
        return merged


def _conversation_client_key(clients_dir: Path, pid: str) -> str:
    """Ключ диалогов клиента в БД."""
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
