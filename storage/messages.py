"""storage/messages.py — CRUD для сообщений и контекст для LLM."""

import json
from datetime import datetime
from typing import Optional

from storage.db import fetchone, fetchall, execute, transaction


def add_message(
    conversation_id: str,
    role: str,                      # 'client' | 'bot' | 'human'
    text: str,
    content_kind: str = "text",
    provider_message_id: str = "",
    delivery_status: str = "",
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    latency_ms: Optional[int] = None,
    answer_kind: Optional[str] = None,  # 'kb' | 'handoff' | 'no_answer'
    media_path: Optional[str] = None,
    media_mime: Optional[str] = None,
    media_duration_s: Optional[float] = None,
    media_size: Optional[int] = None,
    media_status: Optional[str] = None,  # 'ok' | 'failed' | 'skipped' | 'expired'
    media_model: Optional[str] = None,
    media_cost: Optional[float] = None,
) -> int:
    """
    Добавить сообщение в диалог. Возвращает message_id (autoincrement).
    Обновляет last_message_at в conversations.
    """
    now = datetime.utcnow().isoformat()
    is_client = (role == "client")

    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO messages (conversation_id, role, text, content_kind, provider_message_id,
                                  delivery_status, tokens_in, tokens_out, latency_ms, answer_kind,
                                  media_path, media_mime, media_duration_s, media_size,
                                  media_status, media_model, media_cost, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (conversation_id, role, text, content_kind, provider_message_id,
             delivery_status, tokens_in, tokens_out, latency_ms, answer_kind,
             media_path, media_mime, media_duration_s, media_size,
             media_status, media_model, media_cost, now),
        )
        msg_id = cursor.lastrowid

        # Обновить временные метки диалога
        conn.execute(
            "UPDATE conversations SET last_message_at = ? WHERE id = ?",
            (now, conversation_id),
        )
        if is_client:
            conn.execute(
                "UPDATE conversations SET last_client_message_at = ?, unread_count = unread_count + 1 WHERE id = ?",
                (now, conversation_id),
            )

    return msg_id


def get_message(msg_id: int) -> Optional[dict]:
    row = fetchone("SELECT * FROM messages WHERE id = ?", (msg_id,))
    return dict(row) if row else None


def get_messages(
    conversation_id: str,
    before: Optional[str] = None,    # ISO8601, получить сообщения старше этого времени
    limit: int = 50,
    role: Optional[str] = None,
) -> list[dict]:
    """
    Получить сообщения диалога (новые -> старые).
    Если before задан — пагинация назад во времени.
    """
    sql = "SELECT * FROM messages WHERE conversation_id = ?"
    params = [conversation_id]

    if role:
        sql += " AND role = ?"
        params.append(role)

    if before:
        sql += " AND created_at < ?"
        params.append(before)

    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    rows = fetchall(sql, tuple(params))
    return [dict(row) for row in rows]


def get_context_for_llm(
    conversation_id: str,
    max_messages: int = 16,
    max_chars_per_msg: int = 700,
    total_char_budget: int = 8000,
) -> list[dict]:
    """
    Получить контекст для LLM: последние N сообщений с обрезкой.
    Роль 'human' идёт как 'assistant' (бот учитывает ответы человека как свои).
    Возвращает список в хронологическом порядке (старые -> новые).
    """
    rows = fetchall(
        """
        SELECT role, text, created_at
        FROM messages
        WHERE conversation_id = ?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (conversation_id, max_messages),
    )

    if not rows:
        return []

    # Реверс для хронологического порядка
    messages = []
    total_chars = 0

    for row in reversed(rows):
        role = row["role"]
        text = row["text"] or ""

        # Роли БД -> роли LLM: клиент — user; ответы бота и менеджера —
        # assistant (бот учитывает их как свои).
        if role in ("bot", "human"):
            role = "assistant"
        elif role == "client":
            role = "user"

        # Обрезка длинного сообщения
        if len(text) > max_chars_per_msg:
            text = text[:max_chars_per_msg] + "…"

        # Бюджет символов: переполнение пропускаем, а не обрываем — иначе
        # вместе с лишним текстом выпали бы самые новые сообщения диалога.
        if total_chars + len(text) > total_char_budget:
            continue

        messages.append({"role": role, "content": text})
        total_chars += len(text)

    return messages


def get_last_client_message_at(conversation_id: str) -> Optional[str]:
    """Время последнего сообщения от клиента (role='client')."""
    row = fetchone(
        "SELECT created_at FROM messages WHERE conversation_id = ? AND role = 'client' ORDER BY created_at DESC LIMIT 1",
        (conversation_id,),
    )
    return row["created_at"] if row else None


def update_delivery_status(provider_message_id: str, status: str) -> bool:
    """
    Обновить статус доставки по provider_message_id.
    Используется при получении message.sent / message.failed / message.delivered.
    """
    result = execute(
        "UPDATE messages SET delivery_status = ? WHERE provider_message_id = ?",
        (status, provider_message_id),
    )
    return result.rowcount > 0


def get_message_by_provider_id(provider_message_id: str) -> Optional[dict]:
    """Найти сообщение по ID провайдера (для дедупликации исходящих)."""
    row = fetchone(
        "SELECT * FROM messages WHERE provider_message_id = ?",
        (provider_message_id,),
    )
    return dict(row) if row else None


def count_messages_since(conversation_id: str, since: str, role: Optional[str] = None) -> int:
    """Количество сообщений в диалоге с заданного времени."""
    sql = "SELECT COUNT(*) as cnt FROM messages WHERE conversation_id = ? AND created_at > ?"
    params = [conversation_id, since]
    if role:
        sql += " AND role = ?"
        params.append(role)
    row = fetchone(sql, tuple(params))
    return row["cnt"] if row else 0


def get_bot_messages_stats(client_key: str, from_date: str, to_date: str) -> dict:
    """
    Статистика сообщений бота за период для аналитики.
    """
    row = fetchone(
        """
        SELECT
            COUNT(*) as total_bot_messages,
            COALESCE(AVG(latency_ms), 0) as avg_latency_ms,
            COALESCE(SUM(tokens_in), 0) as total_tokens_in,
            COALESCE(SUM(tokens_out), 0) as total_tokens_out,
            COALESCE(SUM(CASE WHEN answer_kind = 'handoff' THEN 1 ELSE 0 END), 0) as handoff_count,
            COALESCE(SUM(CASE WHEN answer_kind = 'no_answer' THEN 1 ELSE 0 END), 0) as no_answer_count,
            COALESCE(SUM(CASE WHEN answer_kind = 'kb' THEN 1 ELSE 0 END), 0) as kb_count
        FROM messages m
        JOIN conversations c ON m.conversation_id = c.id
        WHERE c.client_key = ?
          AND m.role = 'bot'
          AND m.created_at >= ?
          AND m.created_at <= ?
        """,
        (client_key, from_date, to_date),
    )
    return dict(row) if row else {}


def get_conversation_stats(client_key: str, from_date: str, to_date: str) -> dict:
    """
    Статистика диалогов за период.

    conversations_with_client_msg — диалоги, где клиент писал ИМЕННО в периоде
    (без фильтра по датам метрика считала бы все диалоги за всё время).
    conversations_closed_by_bot — те же диалоги, но без единой передачи
    менеджеру: их закрыл бот сам. Считается в тех же единицах, что и
    conversations_with_client_msg, поэтому доля не уходит в минус.
    """
    row = fetchone(
        """
        SELECT
            COUNT(DISTINCT c.id) as total_conversations,
            COUNT(DISTINCT CASE WHEN c.created_at >= ? AND c.created_at <= ? THEN c.id END) as new_conversations,
            COUNT(DISTINCT CASE WHEN EXISTS (
                SELECT 1 FROM messages m
                WHERE m.conversation_id = c.id AND m.role = 'client'
                  AND m.created_at >= ? AND m.created_at <= ?
            ) THEN c.id END) as conversations_with_client_msg,
            COUNT(DISTINCT CASE WHEN EXISTS (
                SELECT 1 FROM messages m
                WHERE m.conversation_id = c.id AND m.role = 'client'
                  AND m.created_at >= ? AND m.created_at <= ?
            ) AND NOT EXISTS (
                SELECT 1 FROM handoffs h WHERE h.conversation_id = c.id
            ) THEN c.id END) as conversations_closed_by_bot
        FROM conversations c
        WHERE c.client_key = ?
        """,
        (from_date, to_date, from_date, to_date, from_date, to_date, client_key),
    )
    return dict(row) if row else {}


def get_handoff_stats(client_key: str, from_date: str, to_date: str) -> dict:
    """Статистика передач по причинам."""
    rows = fetchall(
        """
        SELECT h.reason, COUNT(*) as cnt
        FROM handoffs h
        JOIN conversations c ON h.conversation_id = c.id
        WHERE c.client_key = ?
          AND h.created_at >= ?
          AND h.created_at <= ?
        GROUP BY h.reason
        """,
        (client_key, from_date, to_date),
    )
    return {row["reason"]: row["cnt"] for row in rows}


def get_hourly_distribution(client_key: str, from_date: str, to_date: str, timezone: str = "Asia/Almaty") -> list[dict]:
    """
    Распределение сообщений по часам (в часовом поясе клиента).
    Возвращает список {hour: 0-23, count: N}.
    """
    # SQLite не умеет таймзоны нативно, делаем в Python
    # Здесь просто возвращаем UTC, конвертацию делаем в API
    rows = fetchall(
        """
        SELECT strftime('%H', m.created_at) as hour_utc, COUNT(*) as cnt
        FROM messages m
        JOIN conversations c ON m.conversation_id = c.id
        WHERE c.client_key = ?
          AND m.role = 'client'
          AND m.created_at >= ?
          AND m.created_at <= ?
        GROUP BY hour_utc
        """,
        (client_key, from_date, to_date),
    )
    return [{"hour_utc": int(row["hour_utc"]), "count": row["cnt"]} for row in rows]


def cleanup_old_seen_events(days: int = 7) -> int:
    """Очистка seen_events старше N дней."""
    result = execute(
        "DELETE FROM seen_events WHERE created_at < datetime('now', ?)",
        (f"-{days} days",),
    )
    return result.rowcount


def cleanup_expired_media(days: int = 30) -> int:
    """
    Удаляет физические медиафайлы старше N дней и обновляет media_status на 'expired'.
    Текст расшифровки сохраняется.
    Возвращает количество очищенных записей.
    """
    from pathlib import Path
    rows = fetchall(
        """
        SELECT id, media_path FROM messages
        WHERE media_path IS NOT NULL
          AND media_status != 'expired'
          AND created_at < datetime('now', ?)
        """,
        (f"-{days} days",),
    )
    count = 0
    for row in rows:
        path_str = row["media_path"]
        if path_str:
            try:
                p = Path(path_str)
                if p.is_file():
                    p.unlink(missing_ok=True)
            except OSError:
                pass
        execute("UPDATE messages SET media_status = 'expired' WHERE id = ?", (row["id"],))
        count += 1
    return count