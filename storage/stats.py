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

    # Диалоги за период (где было хотя бы одно сообщение клиента)
    dialogues_count = conv_stats.get("conversations_with_client_msg", 0)
    new_contacts = conv_stats.get("new_conversations", 0)

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

    bot_msg_count = msg_stats.get("total_bot_messages", 0)
    total_messages = client_msg_count + bot_msg_count

    # Закрыто ботом: диалогов без handoff / всего диалогов с сообщениями клиента
    total_handoffs = sum(handoff_stats.values())
    closed_by_bot = dialogues_count - total_handoffs if dialogues_count > 0 else 0
    closed_by_bot_pct = round(closed_by_bot / dialogues_count * 100, 1) if dialogues_count > 0 else 0

    # Время ответа бота
    avg_latency = msg_stats.get("avg_latency_ms") or 0
    # p95 считаем в Python если нужно (нужны сырые данные)

    # Топ вопросов без ответа
    top_unanswered = get_top_unanswered_questions(client_key, from_date, to_date, limit=10)

    # Часы пик (конвертируем UTC -> timezone)
    hourly_tz = convert_hourly_to_timezone(hourly, timezone)

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
            # "p95_ms": ... нужно сырые данные
        },
        "manager_reaction": response_times,
        "hourly_peak": hourly_tz,
        "top_unanswered": top_unanswered,
        "tokens": {
            "in": msg_stats.get("total_tokens_in", 0),
            "out": msg_stats.get("total_tokens_out", 0),
        },
        "estimated_time_saved_minutes": closed_by_bot * 2,  # minutes_per_reply = 2 по умолчанию
    }


def get_top_unanswered_questions(client_key: str, from_date: str, to_date: str, limit: int = 10) -> list[dict]:
    """Топ вопросов без ответа по частоте (группы)."""
    rows = fetchall(
        """
        SELECT ug.name, COUNT(uq.id) as count, MAX(uq.created_at) as last_asked
        FROM unanswered_questions uq
        JOIN unanswered_groups ug ON uq.group_id = ug.id
        WHERE uq.client_key = ?
          AND uq.status = 'answered'
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


def get_admin_overview_stats(from_date: str, to_date: str) -> dict:
    """Сводка по всем клиентам для админа."""
    # Список клиентов с их статистикой
    rows = fetchall(
        """
        SELECT DISTINCT client_key FROM conversations
        WHERE created_at <= ?
        """,
        (to_date,),
    )
    client_keys = [row["client_key"] for row in rows]

    clients_stats = {}
    total_tokens_in = 0
    total_tokens_out = 0
    total_dialogues = 0
    total_handoffs = 0

    for ck in client_keys:
        stats = get_client_stats(ck, from_date, to_date)
        clients_stats[ck] = {
            "dialogues": stats["dialogues"],
            "handoffs": stats["handoffs"]["total"],
            "closed_by_bot_pct": stats["closed_by_bot"]["percentage"],
            "tokens_in": stats["tokens"]["in"],
            "tokens_out": stats["tokens"]["out"],
        }
        total_tokens_in += stats["tokens"]["in"]
        total_tokens_out += stats["tokens"]["out"]
        total_dialogues += stats["dialogues"]
        total_handoffs += stats["handoffs"]["total"]

    # Стоимость (примерные цены за 1M токенов, можно вынести в конфиг)
    # Пример: gpt-4o-mini $0.15/1M in, $0.60/1M out
    cost_usd = (total_tokens_in / 1_000_000 * 0.15) + (total_tokens_out / 1_000_000 * 0.60)

    return {
        "period": {"from": from_date, "to": to_date},
        "total_clients": len(client_keys),
        "total_dialogues": total_dialogues,
        "total_handoffs": total_handoffs,
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "estimated_cost_usd": round(cost_usd, 4),
        "clients": clients_stats,
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