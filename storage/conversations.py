"""storage/conversations.py — CRUD для диалогов."""

import sqlite3
import uuid
from datetime import datetime
from typing import Optional

from storage.db import fetchone, fetchall, execute, transaction


def create_conversation(
    client_key: str,
    channel: str,
    contact_phone: str,
    contact_name: str = "",
    zernio_conversation_id: str = "",
) -> dict:
    """
    Создать новый диалог или вернуть существующий.
    Уникальность по (client_key, contact_phone).
    Возвращает dict диалога с ключом 'id'.
    """
    # Сначала пробуем найти существующий
    existing = fetchone(
        "SELECT * FROM conversations WHERE client_key = ? AND contact_phone = ?",
        (client_key, contact_phone),
    )
    if existing:
        conv = dict(existing)
        # Обновить имя и zernio_conversation_id если изменились
        execute(
            """
            UPDATE conversations
            SET contact_name = COALESCE(NULLIF(?, ''), contact_name),
                zernio_conversation_id = COALESCE(NULLIF(?, ''), zernio_conversation_id),
                last_message_at = datetime('now')
            WHERE id = ?
            """,
            (contact_name, zernio_conversation_id, conv["id"]),
        )
        return conv

    # Создать новый диалог атомарно, избегая race condition при параллельных вебхуках
    conv_id = str(uuid.uuid4())
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        execute(
            """
            INSERT INTO conversations (id, client_key, channel, contact_phone, contact_name, zernio_conversation_id, created_at, last_message_at, last_client_message_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_key, contact_phone) DO NOTHING
            """,
            (conv_id, client_key, channel, contact_phone, contact_name, zernio_conversation_id, now, now, now),
        )
    except sqlite3.IntegrityError:
        pass

    # Повторный поиск после безопасной вставки
    fresh = fetchone(
        "SELECT * FROM conversations WHERE client_key = ? AND contact_phone = ?",
        (client_key, contact_phone),
    )
    if fresh:
        return dict(fresh)

    return {"id": conv_id, "client_key": client_key, "channel": channel, "contact_phone": contact_phone,
            "contact_name": contact_name, "zernio_conversation_id": zernio_conversation_id,
            "status": "bot", "created_at": now, "last_message_at": now, "last_client_message_at": now,
            "unread_count": 0}


def get_conversation(conv_id: str) -> Optional[dict]:
    """Получить диалог по ID."""
    row = fetchone("SELECT * FROM conversations WHERE id = ?", (conv_id,))
    return dict(row) if row else None


def get_conversation_by_client_and_phone(client_key: str, contact_phone: str) -> Optional[dict]:
    """Получить диалог по клиенту и телефону."""
    row = fetchone(
        "SELECT * FROM conversations WHERE client_key = ? AND contact_phone = ?",
        (client_key, contact_phone),
    )
    return dict(row) if row else None


def list_conversations(
    client_key: str,
    status: Optional[str] = None,
    q: Optional[str] = None,          # поиск по имени/номеру
    cursor: Optional[str] = None,     # пагинация: last_message_at ISO8601
    since: Optional[str] = None,      # только с last_message_at > since
    limit: int = 50,
) -> list[dict]:
    """
    Список диалогов клиента с фильтрами и пагинацией.
    Сортировка: по last_message_at DESC.
    """
    sql = """
        SELECT c.*,
               m.text AS last_message_text,
               m.content_kind AS last_message_content_kind,
               m.media_duration_s AS last_message_duration_s
        FROM conversations c
        LEFT JOIN messages m ON m.id = (
            SELECT id FROM messages WHERE conversation_id = c.id ORDER BY created_at DESC LIMIT 1
        )
        WHERE c.client_key = ?
    """
    params = [client_key]

    if status:
        sql += " AND c.status = ?"
        params.append(status)

    if q:
        sql += " AND (c.contact_name LIKE ? OR c.contact_phone LIKE ?)"
        like_q = f"%{q}%"
        params.extend([like_q, like_q])

    if since:
        sql += " AND c.last_message_at > ?"
        params.append(since)

    if cursor:
        sql += " AND c.last_message_at < ?"
        params.append(cursor)

    sql += " ORDER BY c.last_message_at DESC LIMIT ?"

    params.append(limit)

    rows = fetchall(sql, tuple(params))
    return [dict(row) for row in rows]


def update_conversation_status(conv_id: str, status: str) -> bool:
    """
    Обновить статус диалога: 'bot' или 'manual'.
    При переходе в manual — зафиксировать manual_since.
    """
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if status == "manual":
        result = execute(
            "UPDATE conversations SET status = ?, manual_since = ?, last_message_at = ? WHERE id = ? AND status != ?",
            (status, now, now, conv_id, status),
        )
    else:
        result = execute(
            "UPDATE conversations SET status = ?, manual_since = NULL, last_message_at = ? WHERE id = ? AND status != ?",
            (status, now, conv_id, status),
        )
    return result.rowcount > 0


def increment_unread(conv_id: str) -> None:
    """Увеличить счётчик непрочитанных (для входящих сообщений клиента в manual)."""
    execute(
        "UPDATE conversations SET unread_count = unread_count + 1, last_message_at = datetime('now') WHERE id = ?",
        (conv_id,),
    )


def mark_read(conv_id: str) -> None:
    """Сбросить счётчик непрочитанных (менеджер открыл диалог)."""
    execute(
        "UPDATE conversations SET unread_count = 0 WHERE id = ?",
        (conv_id,),
    )


def update_last_message_times(conv_id: str, is_client: bool = False) -> None:
    """Обновить last_message_at и (опционально) last_client_message_at."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if is_client:
        execute(
            "UPDATE conversations SET last_message_at = ?, last_client_message_at = ? WHERE id = ?",
            (now, now, conv_id),
        )
    else:
        execute(
            "UPDATE conversations SET last_message_at = ? WHERE id = ?",
            (now, conv_id),
        )


def get_conversations_needing_timeout_check(
    timeout_hours: int,
    client_key: Optional[str] = None,
) -> list[dict]:
    """
    Найти диалоги в manual, где нет сообщений от человека > timeout_hours.
    Для фоновой задачи автовозврата к боту.

    client_key — ограничить одним клиентом (в мультитенанте у каждого свой
    таймаут, поэтому задача обходит тенантов по одному).
    """
    sql = """
        SELECT * FROM conversations
        WHERE status = 'manual'
          AND datetime(COALESCE(last_message_at, manual_since), ? || ' hours') < datetime('now')
    """
    params: tuple = (f"+{int(timeout_hours)}",)
    if client_key:
        sql += " AND client_key = ?"
        params += (client_key,)
    rows = fetchall(sql, params)
    return [dict(row) for row in rows]


def delete_conversation(conv_id: str) -> bool:
    """Удалить диалог (каскадно удалятся messages через FK)."""
    result = execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    return result.rowcount > 0


def cleanup_old_conversations(retention_days: int = 365) -> int:
    """Удалить диалоги старше retention_days (ГДПР / политика хранения)."""
    result = execute(
        "DELETE FROM conversations WHERE datetime(created_at) < datetime('now', ?)",
        (f"-{retention_days} days",),
    )
    return result.rowcount