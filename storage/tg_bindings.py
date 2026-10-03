"""storage/tg_bindings.py — привязки Telegram и коды для линковки."""

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from storage.db import fetchone, fetchall, execute, transaction


def generate_link_code() -> tuple[str, str]:
    """
    Сгенерировать код привязки и его хэш.
    Возвращает (code, code_hash).
    Код: 8 символов base32 (без padding), легко вводить в Telegram.
    """
    code = secrets.token_bytes(5).hex()[:8].upper()  # 8 hex chars
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    return code, code_hash


def create_link_code(client_key: str, expires_minutes: int = 15) -> dict:
    """
    Создать одноразовый код для привязки Telegram.
    Возвращает dict с code, code_hash, expires_at, url.
    """
    code, code_hash = generate_link_code()
    expires_at = (datetime.utcnow() + timedelta(minutes=expires_minutes)).isoformat()

    execute(
        """
        INSERT INTO tg_link_codes (code_hash, client_key, expires_at)
        VALUES (?, ?, ?)
        """,
        (code_hash, client_key, expires_at),
    )

    bot_username = "JauapBot"  # будет подставляться из настроек
    url = f"https://t.me/{bot_username}?start={code}"

    return {
        "code": code,
        "code_hash": code_hash,
        "expires_at": expires_at,
        "url": url,
    }


def verify_link_code(code: str) -> Optional[dict]:
    """
    Проверить код привязки. Если валиден — вернуть {client_key, code_hash} и пометить использованным.
    """
    code_hash = hashlib.sha256(code.encode()).hexdigest()

    row = fetchone(
        "SELECT client_key, code_hash, chat_id, expires_at, used_at FROM tg_link_codes WHERE code_hash = ?",
        (code_hash,),
    )
    if not row:
        return None

    if row["used_at"]:
        return None  # уже использован

    if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
        return None  # истёк

    return {"client_key": row["client_key"], "code_hash": code_hash}


def complete_link_code(code_hash: str, chat_id: str) -> bool:
    """Завершить привязку: записать chat_id и used_at."""
    result = execute(
        "UPDATE tg_link_codes SET chat_id = ?, used_at = datetime('now') WHERE code_hash = ?",
        (chat_id, code_hash),
    )
    return result.rowcount > 0


def add_tg_binding(client_key: str, chat_id: str) -> bool:
    """Добавить привязку чата к клиенту (после успешной верификации кода)."""
    try:
        execute(
            "INSERT INTO tg_bindings (client_key, chat_id) VALUES (?, ?)",
            (client_key, chat_id),
        )
        return True
    except Exception:
        # Уже существует (UNIQUE constraint)
        return False


def get_tg_bindings(client_key: str) -> list[dict]:
    """Список привязанных чатов для клиента."""
    rows = fetchall(
        "SELECT * FROM tg_bindings WHERE client_key = ? ORDER BY created_at",
        (client_key,),
    )
    return [dict(row) for row in rows]


def remove_tg_binding(client_key: str, binding_id: int) -> bool:
    """Удалить привязку (отвязать чат)."""
    result = execute(
        "DELETE FROM tg_bindings WHERE id = ? AND client_key = ?",
        (binding_id, client_key),
    )
    return result.rowcount > 0


def get_tg_bindings_for_notify(client_key: str) -> list[str]:
    """Получить список chat_id для отправки уведомлений."""
    rows = fetchall(
        "SELECT chat_id FROM tg_bindings WHERE client_key = ?",
        (client_key,),
    )
    return [row["chat_id"] for row in rows]


def cleanup_expired_link_codes() -> int:
    """Удалить истёкшие коды привязки (старше 1 часа от expiry для чистоты)."""
    result = execute(
        "DELETE FROM tg_link_codes WHERE expires_at < datetime('now', '-1 hour')"
    )
    return result.rowcount