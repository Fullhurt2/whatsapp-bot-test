"""Разбор входящих событий вебхука Meta Cloud API.

Meta POST'ит на наш вебхук payload вида:

    {
      "object": "whatsapp_business_account",
      "entry": [{
        "changes": [{
          "field": "messages",
          "value": {
            "metadata": {"phone_number_id": "..."},
            "contacts": [{"profile": {"name": "Аня"}, "wa_id": "77770000001"}],
            "messages": [{
              "from": "77770000001",
              "id": "wamid.HBgLM...",
              "type": "text",
              "text": {"body": "Привет"}
            }]
          }
        }]
      }]
    }

Один POST может содержать несколько entry/changes и несколько сообщений —
возвращаем список. Блок value.statuses (статусы доставки) — не наше
событие, игнорируется. Текст достаём из type=text, а также из нажатий
кнопок (button/interactive) — их обрабатываем как обычное сообщение.
Здесь только разбор, без бизнес-логики (см. tests/test_meta_payload.py).
"""

import logging
import re

from config.settings import normalize_phone
from whatsapp.inbound import InboundMessage

logger = logging.getLogger(__name__)

# Объект вебхука Meta и поле, которое бот обрабатывает.
OBJECT_WABA = "whatsapp_business_account"
FIELD_MESSAGES = "messages"


def parse_meta_events(payload: dict) -> list[InboundMessage]:
    """Извлекает входящие WhatsApp-сообщения из payload вебхука Meta.

    В одном POST может быть несколько entry/changes и несколько сообщений —
    возвращаем все осмысленные. Для служебных событий (статусы доставки) и
    битых payload'ов возвращает [] — вебхук их просто подтверждает 200.
    """
    if not isinstance(payload, dict) or payload.get("object") != OBJECT_WABA:
        return []

    inbound: list[InboundMessage] = []
    for entry in _iter_dicts(payload.get("entry")):
        for change in _iter_dicts(entry.get("changes")):
            if change.get("field") != FIELD_MESSAGES:
                continue
            value = change.get("value")
            if isinstance(value, dict):
                inbound.extend(_parse_value(value))
    return inbound


def _iter_dicts(value) -> list[dict]:
    """Элементы списка-значения, являющиеся словарями."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _parse_value(value: dict) -> list[InboundMessage]:
    """value одного change -> список InboundMessage (может быть пустым)."""
    contacts: dict[str, str] = {}
    for contact in _iter_dicts(value.get("contacts")):
        wa_id = str(contact.get("wa_id") or "")
        profile = contact.get("profile")
        name = profile.get("name") if isinstance(profile, dict) else None
        contacts[wa_id] = str(name or "").strip()

    result: list[InboundMessage] = []
    for message in _iter_dicts(value.get("messages")):
        from_raw = str(message.get("from") or "").strip()
        phone = normalize_phone("+" + re.sub(r"[^\d]", "", from_raw))
        if not re.fullmatch(r"\+\d{6,15}", phone):
            logger.warning("Событие messages с некорректным номером: keys=%s", list(message))
            continue
        result.append(InboundMessage(
            phone=phone,
            display_name=contacts.get(from_raw, ""),
            text=_extract_text(message),
            content_kind=str(message.get("type") or "unknown"),
            message_id=str(message.get("id") or ""),
        ))
    return result


def _extract_text(message: dict) -> str:
    """Текст сообщения: body у текста, label у кнопок/списков."""
    mtype = str(message.get("type") or "")
    if mtype == "text":
        return str((message.get("text") or {}).get("body") or "").strip()
    if mtype == "button":
        return str((message.get("button") or {}).get("text") or "").strip()
    if mtype == "interactive":
        interactive = message.get("interactive") or {}
        for key in ("button_reply", "list_reply"):
            block = interactive.get(key)
            if isinstance(block, dict):
                title = str(block.get("title") or "").strip()
                if title:
                    return title
    return ""
