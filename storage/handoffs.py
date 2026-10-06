"""storage/handoffs.py — CRUD для передач менеджеру."""

from datetime import datetime
from typing import Optional

from storage.db import fetchone, fetchall, execute, transaction


def create_handoff(
    conversation_id: str,
    reason: str,
    summary: str = "",
) -> int:
    """
    Создать запись о передаче. Возвращает handoff_id.
    reason: 'booking' | 'complaint' | 'human_requested' | 'no_answer' | 'llm_error' | 'llm_timeout' | 'keyword:<trigger>'
    """
    now = datetime.utcnow().isoformat()
    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO handoffs (conversation_id, reason, summary, created_at, notified_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (conversation_id, reason, summary or None, now, now),
        )
        handoff_id = cursor.lastrowid

        # Обновить статус диалога на manual если причина требует паузы
        # (логика pause_on проверяется в message_handler)
        # Здесь просто фиксируем handoff

    return handoff_id


def get_handoff(handoff_id: int) -> Optional[dict]:
    row = fetchone("SELECT * FROM handoffs WHERE id = ?", (handoff_id,))
    return dict(row) if row else None


def get_handoffs_for_conversation(conversation_id: str) -> list[dict]:
    """Все передачи для диалога (обычно 0 или 1 открытая)."""
    rows = fetchall(
        "SELECT * FROM handoffs WHERE conversation_id = ? ORDER BY created_at DESC",
        (conversation_id,),
    )
    return [dict(row) for row in rows]


def get_open_handoff(conversation_id: str) -> Optional[dict]:
    """Открытая передача (без resolved_at)."""
    row = fetchone(
        "SELECT * FROM handoffs WHERE conversation_id = ? AND resolved_at IS NULL ORDER BY created_at DESC LIMIT 1",
        (conversation_id,),
    )
    return dict(row) if row else None


def mark_notified(handoff_id: int) -> bool:
    """Отметить, что менеджер уведомлён."""
    result = execute(
        "UPDATE handoffs SET notified_at = datetime('now') WHERE id = ? AND notified_at IS NULL",
        (handoff_id,),
    )
    return result.rowcount > 0


def mark_first_human_reply(handoff_id: int) -> bool:
    """Отметить первое сообщение от человека."""
    result = execute(
        "UPDATE handoffs SET first_human_reply_at = datetime('now') WHERE id = ? AND first_human_reply_at IS NULL",
        (handoff_id,),
    )
    return result.rowcount > 0


def mark_reminded(handoff_id: int) -> bool:
    """Отметить, что напоминание менеджеру отправлено (reminded_at)."""
    result = execute(
        "UPDATE handoffs SET reminded_at = datetime('now') WHERE id = ? AND reminded_at IS NULL",
        (handoff_id,),
    )
    return result.rowcount > 0


def get_handoffs_needing_reminder(hours: int = 2) -> list[dict]:
    """Передачи, висящие без ответа человека дольше hours часов, где напоминание ещё не отправлялось.

    Возвращает handoff с полями диалога (conversation_id, client_key, contact_phone, contact_name).
    """
    rows = fetchall(
        f"""
        SELECT h.id as handoff_id, h.conversation_id, h.reason, h.summary, h.created_at,
               c.client_key, c.contact_phone, c.contact_name
        FROM handoffs h
        JOIN conversations c ON h.conversation_id = c.id
        WHERE h.first_human_reply_at IS NULL
          AND h.resolved_at IS NULL
          AND h.reminded_at IS NULL
          AND h.notified_at IS NOT NULL
          AND h.notified_at <= datetime('now', '-{int(hours)} hours')
        """,
    )
    return [dict(row) for row in rows]


def resolve_handoff(handoff_id: int) -> bool:
    """Закрыть передачу (диалог вернулся к боту или закрыт)."""
    result = execute(
        "UPDATE handoffs SET resolved_at = datetime('now') WHERE id = ? AND resolved_at IS NULL",
        (handoff_id,),
    )
    return result.rowcount > 0


def get_handoff_stats(client_key: str, from_date: str, to_date: str) -> dict:
    """Статистика передач по причинам за период."""
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


def get_manager_response_times(client_key: str, from_date: str, to_date: str) -> dict:
    """
    Время реакции менеджера: от created_at handoff до first_human_reply_at.
    Возвращает avg и p95 в секундах.
    """
    rows = fetchall(
        """
        SELECT
            h.created_at,
            h.first_human_reply_at
        FROM handoffs h
        JOIN conversations c ON h.conversation_id = c.id
        WHERE c.client_key = ?
          AND h.created_at >= ?
          AND h.created_at <= ?
          AND h.first_human_reply_at IS NOT NULL
        """,
        (client_key, from_date, to_date),
    )

    if not rows:
        return {"avg_seconds": 0, "p95_seconds": 0, "count": 0}

    import statistics
    diffs = []
    for row in rows:
        created = datetime.fromisoformat(row["created_at"])
        replied = datetime.fromisoformat(row["first_human_reply_at"])
        diffs.append((replied - created).total_seconds())

    diffs.sort()
    n = len(diffs)
    avg = sum(diffs) / n
    p95_idx = int(n * 0.95)
    p95 = diffs[p95_idx] if p95_idx < n else diffs[-1]

    return {"avg_seconds": avg, "p95_seconds": p95, "count": n}