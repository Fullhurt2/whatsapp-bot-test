"""storage/tg_bindings.py — привязки Telegram и коды для линковки.

Менеджер привязывает свой чат по одноразовой ссылке: панель просит код, бот
JAUAP получает `/start <код>` и запоминает чат. Дальше уведомления о вопросах
идут во все привязанные чаты этого клиента (см. handlers/owner_handler.py).
"""

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional

from storage.db import fetchone, fetchall, execute, transaction

# Сколько чатов менеджер может привязать к одному клиенту.
MAX_BINDINGS_PER_CLIENT = 5

# Сколько живёт код привязки.
LINK_CODE_TTL_MINUTES = 15


def generate_link_code() -> tuple[str, str]:
    """
    Сгенерировать код привязки и его хэш.
    Возвращает (code, code_hash).
    Код: 12 hex-символов с достаточной энтропией против перебора.
    """
    code = secrets.token_hex(6).upper()  # 12 hex chars
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    return code, code_hash


def create_link_code(
    client_key: str,
    expires_minutes: int = LINK_CODE_TTL_MINUTES,
    bot_username: str = "",
) -> dict:
    """
    Создать одноразовый код для привязки Telegram.
    Возвращает dict с code, expires_at и url (ссылка для менеджера).

    bot_username — @username общего бота JAUAP из env: без него ссылка собирается
    только из кода, её придётся вводить вручную.
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

    url = f"https://t.me/{bot_username}?start={code}" if bot_username else ""

    return {
        "code": code,
        "code_hash": code_hash,
        "expires_at": expires_at,
        "url": url,
    }


def verify_link_code(code: str) -> Optional[dict]:
    """
    Проверить код привязки: вернуть {client_key, code_hash}, если код найден,
    не использован и не истёк.
    """
    code_hash = hashlib.sha256(code.strip().encode()).hexdigest()

    row = fetchone(
        "SELECT client_key, code_hash, chat_id, expires_at, used_at FROM tg_link_codes WHERE code_hash = ?",
        (code_hash,),
    )
    if not row:
        return None

    if row["used_at"]:
        return None  # уже использован

    try:
        from datetime import timezone
        exp_dt = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        if exp_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=None)
        if exp_dt < now_dt:
            return None  # истёк
    except Exception:
        return None

    return {"client_key": row["client_key"], "code_hash": code_hash}


def complete_link_code(code_hash: str, chat_id: str) -> bool:
    """Завершить привязку: записать chat_id и used_at.

    Условие `used_at IS NULL` в UPDATE делает код действительно одноразовым:
    два одновременных `/start <код>` не оба получат привязку — второй
    получит False, потому что строка уже занята первым.
    """
    result = execute(
        """
        UPDATE tg_link_codes SET chat_id = ?, used_at = datetime('now')
        WHERE code_hash = ? AND used_at IS NULL
        """,
        (chat_id, code_hash),
    )
    return result.rowcount > 0


def count_bindings(client_key: str) -> int:
    """Сколько чатов уже привязано к клиенту."""
    row = fetchone(
        "SELECT COUNT(*) as cnt FROM tg_bindings WHERE client_key = ?",
        (client_key,),
    )
    return int(row["cnt"]) if row else 0


def add_tg_binding(client_key: str, chat_id: str) -> bool:
    """Добавить привязку чата к клиенту.

    False — чат уже привязан либо у клиента уже MAX_BINDINGS_PER_CLIENT чатов.
    """
    if count_bindings(client_key) >= MAX_BINDINGS_PER_CLIENT:
        return False
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
    """Удалить истёкшие и использованные коды привязки (старше часа).

    Сравнение идёт в формате SQLite datetime('now'): строки с ISO-разделителем
    «T» всегда считались бы «больше» строки с пробелом и не удалялись.
    """
    result = execute(
        """
        DELETE FROM tg_link_codes
        WHERE datetime(replace(expires_at, 'T', ' ')) < datetime('now', '-1 hour')
           OR (used_at IS NOT NULL AND used_at < datetime('now', '-1 hour'))
        """
    )
    return result.rowcount