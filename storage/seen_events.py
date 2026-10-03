"""storage/seen_events.py — дедупликация вебхуков."""

import time
from typing import Optional

from storage.db import fetchone, execute, transaction


def check_and_add(event_id: str) -> bool:
    """
    Проверить, видели ли мы это событие.
    Если нет — добавить и вернуть True (событие новое).
    Если да — вернуть False (дубль).
    Атомарно через INSERT OR IGNORE.
    """
    if not event_id:
        return True  # Без ID не дедуплицируем, пропускаем

    with transaction() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO seen_events (event_id, created_at) VALUES (?, datetime('now'))",
            (event_id,),
        )
        return cursor.rowcount > 0  # 1 = вставили (новое), 0 = уже было (дубль)


def is_seen(event_id: str) -> bool:
    """Проверить, есть ли событие в базе (без добавления)."""
    if not event_id:
        return False
    row = fetchone("SELECT 1 FROM seen_events WHERE event_id = ?", (event_id,))
    return row is not None


def add_event(event_id: str) -> None:
    """Просто добавить событие (для ручного добавления)."""
    execute(
        "INSERT OR IGNORE INTO seen_events (event_id, created_at) VALUES (?, datetime('now'))",
        (event_id,),
    )


def cleanup_old(days: int = 7) -> int:
    """Удалить события старше N дней."""
    result = execute(
        "DELETE FROM seen_events WHERE created_at < datetime('now', ?)",
        (f"-{days} days",),
    )
    return result.rowcount


def get_count() -> int:
    """Количество отслеживаемых событий."""
    row = fetchone("SELECT COUNT(*) as cnt FROM seen_events")
    return row["cnt"] if row else 0