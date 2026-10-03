"""storage/unanswered.py — вопросы без ответа и их группировка."""

import json
from datetime import datetime
from typing import Optional

from storage.db import fetchone, fetchall, execute, transaction


def add_unanswered_question(
    client_key: str,
    conversation_id: str,
    question: str,
) -> int:
    """
    Добавить вопрос без ответа. Возвращает question_id.
    """
    now = datetime.utcnow().isoformat()
    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO unanswered_questions (client_key, conversation_id, question, status, created_at)
            VALUES (?, ?, ?, 'new', ?)
            """,
            (client_key, conversation_id, question, now),
        )
    return cursor.lastrowid


def get_unanswered_question(question_id: int) -> Optional[dict]:
    row = fetchone("SELECT * FROM unanswered_questions WHERE id = ?", (question_id,))
    return dict(row) if row else None


def list_unanswered_questions(
    client_key: str,
    status: Optional[str] = None,  # 'new' | 'answered' | 'ignored'
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """Список вопросов без ответа."""
    sql = "SELECT * FROM unanswered_questions WHERE client_key = ?"
    params = [client_key]

    if status:
        sql += " AND status = ?"
        params.append(status)

    sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = fetchall(sql, tuple(params))
    return [dict(row) for row in rows]


def mark_question_answered(question_id: int, answer_text: str) -> bool:
    """Отметить вопрос как отвеченный, сохранить текст ответа."""
    result = execute(
        "UPDATE unanswered_questions SET status = 'answered', answer_text = ? WHERE id = ? AND status = 'new'",
        (answer_text, question_id),
    )
    return result.rowcount > 0


def mark_question_ignored(question_id: int) -> bool:
    """Скрыть вопрос (игнорировать)."""
    result = execute(
        "UPDATE unanswered_questions SET status = 'ignored' WHERE id = ? AND status = 'new'",
        (question_id,),
    )
    return result.rowcount > 0


# --- Группы вопросов ---

def create_unanswered_group(
    client_key: str,
    name: str,
    question_ids: list[int],
) -> int:
    """Создать группу похожих вопросов. Возвращает group_id."""
    now = datetime.utcnow().isoformat()
    with transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO unanswered_groups (client_key, name, question_ids, status, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?)
            """,
            (client_key, name, json.dumps(question_ids), now, now),
        )
        group_id = cursor.lastrowid

        # Обновить вопросы: привязать к группе
        if question_ids:
            placeholders = ",".join("?" * len(question_ids))
            conn.execute(
                f"UPDATE unanswered_questions SET group_id = ? WHERE id IN ({placeholders})",
                [group_id] + question_ids,
            )
    return group_id


def get_unanswered_group(group_id: int) -> Optional[dict]:
    row = fetchone("SELECT * FROM unanswered_groups WHERE id = ?", (group_id,))
    if row:
        d = dict(row)
        d["question_ids"] = json.loads(d["question_ids"])
        return d
    return None


def list_unanswered_groups(
    client_key: str,
    status: Optional[str] = None,  # 'active' | 'answered' | 'ignored'
) -> list[dict]:
    """Список групп вопросов."""
    sql = "SELECT * FROM unanswered_groups WHERE client_key = ?"
    params = [client_key]

    if status:
        sql += " AND status = ?"
        params.append(status)

    sql += " ORDER BY updated_at DESC"

    rows = fetchall(sql, tuple(params))
    result = []
    for row in rows:
        d = dict(row)
        d["question_ids"] = json.loads(d["question_ids"])
        result.append(d)
    return result


def update_group_status(group_id: int, status: str) -> bool:
    """Обновить статус группы (answered/ignored)."""
    result = execute(
        "UPDATE unanswered_groups SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, group_id),
    )
    return result.rowcount > 0


def get_questions_for_group(group_id: int) -> list[dict]:
    """Получить все вопросы в группе."""
    rows = fetchall(
        "SELECT * FROM unanswered_questions WHERE group_id = ? ORDER BY created_at",
        (group_id,),
    )
    return [dict(row) for row in rows]


def get_unanswered_stats(client_key: str, from_date: str, to_date: str) -> dict:
    """Статистика вопросов без ответа за период."""
    row = fetchone(
        """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN status = 'new' THEN 1 ELSE 0 END) as new_count,
            SUM(CASE WHEN status = 'answered' THEN 1 ELSE 0 END) as answered_count,
            SUM(CASE WHEN status = 'ignored' THEN 1 ELSE 0 END) as ignored_count
        FROM unanswered_questions
        WHERE client_key = ?
          AND created_at >= ?
          AND created_at <= ?
        """,
        (client_key, from_date, to_date),
    )
    return dict(row) if row else {}


def get_recent_unanswered_for_regroup(client_key: str, limit: int = 200) -> list[dict]:
    """Получить последние необработанные вопросы для группировки LLM."""
    rows = fetchall(
        """
        SELECT id, question, created_at
        FROM unanswered_questions
        WHERE client_key = ? AND status = 'new' AND group_id IS NULL
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (client_key, limit),
    )
    return [dict(row) for row in rows]


def get_last_regroup_time(client_key: str) -> Optional[str]:
    """Время последней группировки (updated_at самой свежей группы)."""
    row = fetchone(
        "SELECT updated_at FROM unanswered_groups WHERE client_key = ? ORDER BY updated_at DESC LIMIT 1",
        (client_key,),
    )
    return row["updated_at"] if row else None