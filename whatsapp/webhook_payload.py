"""Разбор входящих событий вебхука Bird (whatsapp.received).

Bird POST'ит на наш вебхук JSON вида:

    {
      "type": "whatsapp.received",
      "timestamp": "...",
      "data": {
        "whatsapp_id": "wam_...",
        "direction": "inbound",
        "from": {"phone_number": "+14155550100", "display_name": "Alex"},
        "to": {"phone_number": "+13124495648"},
        "text": {"body": "Сколько стоит капучино?"},
        ...
      }
    }

Ровно одна «ветка контента» заполнена: text / image / video / audio /
sticker / document / location / contact_cards / interactive_reply /
unsupported. Бот работает с текстом; остальное отвечаем вежливой просьбой
написать текстом (см. handlers/message_handler.py -> handle_non_text).

Здесь только разбор — никакой бизнес-логики: тестируется отдельно
от сервера (tests/test_webhook_payload.py).
"""

import logging
import re

from config.settings import normalize_phone
from whatsapp.inbound import InboundMessage  # noqa: F401 (переэкспорт для совместимости)

logger = logging.getLogger(__name__)

# Событие входящего WhatsApp-сообщения (единственное, что бот обрабатывает).
EVENT_RECEIVED = "whatsapp.received"

# Ветки контента входящего сообщения в порядке приоритета: первая непустая
# становится content_kind; для text и interactive_reply текст достаём.
CONTENT_ARMS = (
    "text",
    "interactive_reply",
    "image",
    "video",
    "audio",
    "sticker",
    "document",
    "location",
    "contact_cards",
    "unsupported",
)


def parse_incoming_event(payload: dict) -> InboundMessage | None:
    """Извлекает входящее WhatsApp-сообщение из события вебхука.

    Возвращает InboundMessage для whatsapp.received с валидным номером
    отправителя, иначе None (чужие события и события без номера —
    игнорируются с логом).
    """
    if not isinstance(payload, dict) or payload.get("type") != EVENT_RECEIVED:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    sender = data.get("from")
    if not isinstance(sender, dict):
        return None
    phone = normalize_phone(str(sender.get("phone_number") or ""))
    if not re.fullmatch(r"\+\d{6,15}", phone):
        logger.warning("Вебхук whatsapp.received с некорректным номером отправителя: keys=%s", list(data))
        return None

    raw_name = sender.get("display_name")
    display_name = raw_name.strip() if isinstance(raw_name, str) else ""

    return InboundMessage(
        phone=phone,
        display_name=display_name,
        text=_extract_text(data),
        content_kind=_content_kind(data),
        message_id=str(data.get("whatsapp_id") or ""),
    )


def _extract_text(data: dict) -> str:
    """Текст сообщения: тело text или нажатая кнопка interactive_reply."""
    text_block = data.get("text")
    if isinstance(text_block, dict):
        body = str(text_block.get("body") or "").strip()
        if body:
            return body
    interactive = data.get("interactive_reply")
    if isinstance(interactive, dict):
        button = interactive.get("button")
        if isinstance(button, dict):
            label = str(button.get("text") or "").strip()
            if label:
                return label
        label = str(interactive.get("text") or "").strip()
        if label:
            return label
    return ""


def _content_kind(data: dict) -> str:
    """Какая ветка контента заполнена: text / image / ... / unknown."""
    for arm in CONTENT_ARMS:
        if data.get(arm):
            return arm
    return "unknown"
