"""storage/stats.py — агрегации для аналитики."""

import json
from datetime import datetime, timedelta
from typing import Optional

from storage.db import fetchone, fetchall, execute
from storage.messages import (
    get_bot_messages_stats,
    get_conversation_stats,
    get_hourly_distribution,
)
from storage.handoffs import get_handoff_stats, get_manager_response_times
from storage.unanswered import get_unanswered_stats


def get_client_stats(client_key: str, from_date: str, to_date: str, timezone: str = "Asia/Almaty") -> dict:
    """
    Полная статистика клиента за период.
    from_date, to_date — ISO8601 (UTC).
    """
    # Базовые метрики
    conv_stats = get_conversation_stats(client_key, from_date, to_date)
    msg_stats = get_bot_messages_stats(client_key, from_date, to_date)
    handoff_stats = get_handoff_stats(client_key, from_date, to_date)
    unanswered_stats = get_unanswered_stats(client_key, from_date, to_date)
    response_times = get_manager_response_times(client_key, from_date, to_date)
    hourly = get_hourly_distribution(client_key, from_date, to_date, timezone)

    # Диалоги за период (где было хотя бы одно сообщение клиента в этом периоде)
    dialogues_count = conv_stats.get("conversations_with_client_msg") or 0
    new_contacts = conv_stats.get("new_conversations") or 0

    # Сообщения: клиенты + бот
    client_msg_count = 0
    row = fetchone(
        """
        SELECT COUNT(*) as cnt FROM messages m
        JOIN conversations c ON m.conversation_id = c.id
        WHERE c.client_key = ? AND m.role = 'client' AND m.created_at >= ? AND m.created_at <= ?
        """,
        (client_key, from_date, to_date),
    )
    if row:
        client_msg_count = row["cnt"]

    bot_msg_count = msg_stats.get("total_bot_messages") or 0
    total_messages = client_msg_count + bot_msg_count

    # Закрыто ботом: диалоги периода без единой передачи менеджеру.
    # Обе величины — счётчики диалогов, поэтому доля не уходит в минус.
    total_handoffs = sum(handoff_stats.values())
    closed_by_bot = conv_stats.get("conversations_closed_by_bot") or 0
    closed_by_bot_pct = round(closed_by_bot / dialogues_count * 100, 1) if dialogues_count > 0 else 0

    # Время ответа бота
    avg_latency = msg_stats.get("avg_latency_ms") or 0
    p95_latency = get_bot_latency_p95(client_key, from_date, to_date)

    # Топ вопросов без ответа
    top_unanswered = get_top_unanswered_questions(client_key, from_date, to_date, limit=10)

    # Часы пик (конвертируем UTC -> timezone)
    hourly_tz = convert_hourly_to_timezone(hourly, timezone)

    # Медиа-статистика
    media_stats = get_media_stats(client_key, from_date, to_date)

    return {
        "period": {"from": from_date, "to": to_date},
        "dialogues": dialogues_count,
        "new_contacts": new_contacts,
        "messages": {
            "total": total_messages,
            "client": client_msg_count,
            "bot": bot_msg_count,
        },
        "closed_by_bot": {
            "count": closed_by_bot,
            "percentage": closed_by_bot_pct,
        },
        "handoffs": {
            "total": total_handoffs,
            "by_reason": handoff_stats,
        },
        "unanswered": unanswered_stats,
        "response_time": {
            "avg_ms": round(avg_latency, 1),
            "p95_ms": round(p95_latency, 1),
        },
        "manager_reaction": response_times,
        "hourly_peak": hourly_tz,
        "top_unanswered": top_unanswered,
        "tokens": {
            "in": msg_stats.get("total_tokens_in") or 0,
            "out": msg_stats.get("total_tokens_out") or 0,
        },
        "media": media_stats,
        "estimated_time_saved_minutes": closed_by_bot * SAVED_MINUTES_PER_DIALOGUE,
    }



# Сколько минут работы менеджера экономит один диалог, закрытый ботом.
# Ориентир — 2 минуты на ответ; при желании вынести в конфиг клиента.
SAVED_MINUTES_PER_DIALOGUE = 2


def get_bot_latency_p95(client_key: str, from_date: str, to_date: str) -> float:
    """95-й перцентиль времени ответа бота за период (мс)."""
    rows = fetchall(
        """
        SELECT m.latency_ms
        FROM messages m
        JOIN conversations c ON m.conversation_id = c.id
        WHERE c.client_key = ?
          AND m.role = 'bot'
          AND m.latency_ms IS NOT NULL
          AND m.created_at >= ?
          AND m.created_at <= ?
        ORDER BY m.latency_ms
        """,
        (client_key, from_date, to_date),
    )
    if not rows:
        return 0.0
    index = min(int(round(0.95 * (len(rows) - 1))), len(rows) - 1)
    return float(rows[index]["latency_ms"] or 0)


def get_top_unanswered_questions(client_key: str, from_date: str, to_date: str, limit: int = 10) -> list[dict]:
    """Топ вопросов без ответа по частоте (по группам вопросов).

    Берём вопросы, на которые ответа ещё нет (new/ignored); answered — это
    уже закрытые вопросы, они в «без ответа» не должны попадать.
    """
    rows = fetchall(
        """
        SELECT ug.name, COUNT(uq.id) as count, MAX(uq.created_at) as last_asked
        FROM unanswered_questions uq
        JOIN unanswered_groups ug ON uq.group_id = ug.id
        WHERE uq.client_key = ?
          AND uq.status != 'answered'
          AND uq.created_at >= ?
          AND uq.created_at <= ?
        GROUP BY ug.id
        ORDER BY count DESC
        LIMIT ?
        """,
        (client_key, from_date, to_date, limit),
    )
    return [dict(row) for row in rows]


def convert_hourly_to_timezone(hourly_utc: list[dict], timezone: str) -> list[dict]:
    """
    Конвертировать часы UTC в часовой пояс клиента.
    Простая реализация:Asia/Almaty = UTC+5 (без DST).
    Для полноты можно использовать zoneinfo (Python 3.9+).
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(timezone)
        # Находим смещение для текущей даты (середина периода)
        now = datetime.now(tz)
        offset_hours = int(now.utcoffset().total_seconds() / 3600)
    except Exception:
        # Fallback: Asia/Almaty = UTC+5
        offset_hours = 5

    result = []
    for item in hourly_utc:
        hour_utc = item["hour_utc"]
        hour_local = (hour_utc + offset_hours) % 24
        result.append({"hour": hour_local, "count": item["count"]})

    # Сортируем по локальному часу
    result.sort(key=lambda x: x["hour"])
    return result


def get_admin_overview_stats(
    from_date: str,
    to_date: str,
    client_map: Optional[dict] = None,
) -> dict:
    """Сводка по всем клиентам для админа.

    client_map — сопоставление «ключ панели (имя файла) -> ключ в БД».
    Для zernio-клиентов они различаются (имя файла против accountId), поэтому
    без этой карты в сводке вместо клиентов были бы их accountId. Если карта не
    передана, берём ключи клиентов из самой БД.
    """
    if client_map is None:
        rows = fetchall(
            "SELECT DISTINCT client_key FROM conversations WHERE created_at <= ?",
            (to_date,),
        )
        pairs = [(row["client_key"], row["client_key"]) for row in rows]
    else:
        pairs = [(pid, db_key) for pid, db_key in client_map.items() if db_key]

    clients_stats = {}
    total_tokens_in = 0
    total_tokens_out = 0
    total_dialogues = 0
    total_handoffs = 0

    for pid, db_key in pairs:
        stats = get_client_stats(db_key, from_date, to_date)
        tokens_in = stats["tokens"]["in"] or 0
        tokens_out = stats["tokens"]["out"] or 0
        clients_stats[pid] = {
            "dialogues": stats["dialogues"],
            "handoffs": stats["handoffs"]["total"],
            "closed_by_bot_pct": stats["closed_by_bot"]["percentage"],
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }
        total_tokens_in += tokens_in
        total_tokens_out += tokens_out
        total_dialogues += stats["dialogues"]
        total_handoffs += stats["handoffs"]["total"]

    # Стоимость (примерные цены за 1M токенов, можно вынести в конфиг)
    # Пример: gpt-4o-mini $0.15/1M in, $0.60/1M out
    cost_usd = (total_tokens_in / 1_000_000 * 0.15) + (total_tokens_out / 1_000_000 * 0.60)

    return {
        "period": {"from": from_date, "to": to_date},
        "total_clients": len(pairs),
        "total_dialogues": total_dialogues,
        "total_handoffs": total_handoffs,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "estimated_cost_usd": round(cost_usd, 4),
        "clients": clients_stats,
    }


def get_media_stats(client_key: str, from_date: str, to_date: str) -> dict:
    """Агрегация по обработанным медиафайлам."""
    row = fetchone(
        """
        SELECT
            COUNT(CASE WHEN m.content_kind = 'voice' THEN 1 END) as voice_count,
            COUNT(CASE WHEN m.content_kind = 'image' THEN 1 END) as image_count,
            COUNT(CASE WHEN m.content_kind = 'video' THEN 1 END) as video_count,
            COUNT(CASE WHEN m.content_kind IN ('document', 'file') THEN 1 END) as document_count,
            COUNT(CASE WHEN m.media_status = 'failed' THEN 1 END) as failed_count,
            COUNT(CASE WHEN m.media_status = 'skipped' THEN 1 END) as skipped_count,
            COALESCE(SUM(m.media_duration_s), 0) as total_duration_s,
            COALESCE(SUM(m.media_cost), 0.0) as total_cost
        FROM messages m
        JOIN conversations c ON m.conversation_id = c.id
        WHERE c.client_key = ?
          AND m.created_at >= ?
          AND m.created_at <= ?
        """,
        (client_key, from_date, to_date),
    )
    if not row:
        return {
            "voice": 0, "image": 0, "video": 0, "document": 0,
            "failed": 0, "skipped": 0, "duration_s": 0.0, "cost": 0.0,
        }
    return {
        "voice": int(row["voice_count"] or 0),
        "image": int(row["image_count"] or 0),
        "video": int(row["video_count"] or 0),
        "document": int(row["document_count"] or 0),
        "failed": int(row["failed_count"] or 0),
        "skipped": int(row["skipped_count"] or 0),
        "duration_s": round(float(row["total_duration_s"] or 0.0), 1),
        "cost": round(float(row["total_cost"] or 0.0), 4),
    }


def export_stats_csv(client_key: str, from_date: str, to_date: str) -> str:
    """Экспорт статистики в CSV (UTF-8 с BOM для Excel)."""
    stats = get_client_stats(client_key, from_date, to_date)

    lines = []
    lines.append("Метрика,Значение")
    lines.append(f"Период с,{from_date}")
    lines.append(f"Период по,{to_date}")
    lines.append(f"Диалогов,{stats['dialogues']}")
    lines.append(f"Новых контактов,{stats['new_contacts']}")
    lines.append(f"Всего сообщений,{stats['messages']['total']}")
    lines.append(f"Сообщений клиентов,{stats['messages']['client']}")
    lines.append(f"Сообщений бота,{stats['messages']['bot']}")
    lines.append(f"Закрыто ботом (шт.),{stats['closed_by_bot']['count']}")
    lines.append(f"Закрыто ботом (%),{stats['closed_by_bot']['percentage']}")
    lines.append(f"Передач всего,{stats['handoffs']['total']}")
    for reason, cnt in stats['handoffs']['by_reason'].items():
        lines.append(f"Передач: {reason},{cnt}")
    lines.append(f"Вопросов без ответа всего,{stats['unanswered']['total']}")
    lines.append(f"Вопросов без ответа (новых),{stats['unanswered']['new_count']}")
    lines.append(f"Вопросов без ответа (отвечено),{stats['unanswered']['answered_count']}")
    lines.append(f"Вопросов без ответа (игнорировано),{stats['unanswered']['ignored_count']}")
    lines.append(f"Среднее время ответа бота (мс),{stats['response_time']['avg_ms']}")
    lines.append(f"Реакция менеджера (среднее сек.),{stats['manager_reaction']['avg_seconds']}")
    lines.append(f"Реакция менеджера (p95 сек.),{stats['manager_reaction']['p95_seconds']}")
    lines.append(f"Токенов в,{stats['tokens']['in']}")
    lines.append(f"Токенов аут,{stats['tokens']['out']}")
    if "media" in stats:
        lines.append(f"Медиа: голосовых,{stats['media']['voice']}")
        lines.append(f"Медиа: фото,{stats['media']['image']}")
        lines.append(f"Медиа: секунд аудио,{stats['media']['duration_s']}")
        lines.append(f"Медиа: расход ($),{stats['media']['cost']}")
    lines.append(f"Оценка сэкономленного времени (мин.),{stats['estimated_time_saved_minutes']}")

    # BOM для Excel
    return "\ufeff" + "\n".join(lines)



def parse_date_range(from_str: Optional[str], to_str: Optional[str]) -> tuple[str, str]:
    """Парсинг диапазона дат из query параметров. По умолчанию — последние 30 дней."""
    now = datetime.utcnow()
    if to_str:
        to_dt = datetime.fromisoformat(to_str.replace("Z", "+00:00"))
    else:
        to_dt = now

    if from_str:
        from_dt = datetime.fromisoformat(from_str.replace("Z", "+00:00"))
    else:
        from_dt = to_dt - timedelta(days=30)

    return from_dt.isoformat(), to_dt.isoformat()