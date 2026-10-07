"""Разбор входящих событий вебхука Telegram Bot API.

Telegram POST'ит на наш вебхук update вида:

    {
      "update_id": 123456789,
      "message": {
        "message_id": 42,
        "from": {"id": 777, "is_bot": false, "first_name": "Аня", "last_name": "Қ"},
        "chat": {"id": 777, "type": "private", "first_name": "Аня"},
        "date": 1700000000,
        "text": "Привет"
      }
    }

Разбираем только message (edited_message, callback_query и прочие типы
игнорируем — мы подписаны на allowed_updates=["message"]). Ключ диалога —
chat_id: у личных чатов он совпадает с id пользователя, у групп отрицательный.
Ключ маршрутизации к конфигу клиента — id бота (tenant_key из URL вебхука),
он кладётся в phone_number_id.

Здесь только разбор, без бизнес-логики (см. tests/test_telegram_payload.py).
"""

import logging

from whatsapp.inbound import InboundMessage

logger = logging.getLogger(__name__)

# Типы медиа-контента, которые распознаём как «нетекстовое» (для вежливого
# «напишите текстом»). Всё остальное без текста — "unknown".
_MEDIA_KEYS = (
    "photo", "voice", "audio", "video", "document", "sticker",
    "animation", "video_note", "location", "contact", "poll", "venue",
)


def parse_telegram_update(payload: dict, tenant_key: str) -> list[InboundMessage]:
    """Одно событие вебхука -> список InboundMessage (0 или 1 элемент).

    tenant_key — id бота из адреса вебхука; попадает в phone_number_id для
    маршрутизации к конфигу клиента. Не-наше событие (не message) -> [].
    """
    if not isinstance(payload, dict):
        return []
    message = payload.get("message")
    if not isinstance(message, dict):
        return []

    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat_id = chat.get("id", sender.get("id"))
    if chat_id is None:
        logger.warning("Telegram update без chat/from id: keys=%s", list(message))
        return []

    media_duration_s = 0.0
    for key in ("voice", "audio"):
        item = message.get(key)
        if isinstance(item, dict):
            try:
                media_duration_s = float(item.get("duration") or 0.0)
            except (ValueError, TypeError):
                media_duration_s = 0.0
            break

    caption = str(message.get("caption") or "").strip()

    return [InboundMessage(
        phone=str(chat_id),
        display_name=_display_name(message, chat, sender),
        text=_extract_text(message),
        content_kind=_content_kind(message),
        message_id=str(payload.get("update_id") or message.get("message_id") or ""),
        phone_number_id=tenant_key,
        media_caption=caption,
        media_duration_s=media_duration_s,
    )]


def _display_name(message: dict, chat: dict, sender: dict) -> str:
    """Имя собеседника: для личных чатов — имя пользователя, для групп — title."""
    who = sender or chat
    parts = [str(who.get("first_name") or "").strip(), str(who.get("last_name") or "").strip()]
    name = " ".join(part for part in parts if part).strip()
    if name:
        return name
    title = str(chat.get("title") or "").strip()
    if title:
        return title
    return str(who.get("username") or "").strip()


def _extract_text(message: dict) -> str:
    """Текст сообщения: text обычного сообщения или caption у медиа."""
    for key in ("text", "caption"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _content_kind(message: dict) -> str:
    """Тип контента: text, имя медиа-ветки (photo/voice/…) или unknown."""
    if _extract_text(message):
        return "text"
    for key in _MEDIA_KEYS:
        if message.get(key) is not None:
            return key
    return "unknown"
