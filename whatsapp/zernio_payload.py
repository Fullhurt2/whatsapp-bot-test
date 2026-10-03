"""Разбор входящих событий вебхука Zernio.

Поддерживаемые события:
- message.received: входящее сообщение от клиента (direction: incoming)
- message.sent: исходящее сообщение от бизнеса/оператора (direction: outgoing)
- message.failed: сообщение не доставлено
- message.delivered: сообщение доставлено (опционально)
- message.read: сообщение прочитано (опционально)

Для message.received: фильтруем сообщения от бизнеса (sender.phoneNumber == бизнес-номер),
чтобы бот не отвечал оператору.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional

from config.settings import normalize_phone
from whatsapp.inbound import InboundMessage

logger = logging.getLogger(__name__)

EVENT_MESSAGE_RECEIVED = "message.received"
EVENT_MESSAGE_SENT = "message.sent"
EVENT_MESSAGE_FAILED = "message.failed"
EVENT_MESSAGE_DELIVERED = "message.delivered"
EVENT_MESSAGE_READ = "message.read"

PLATFORM_WHATSAPP = "whatsapp"
DIRECTION_INCOMING = "incoming"
DIRECTION_OUTGOING = "outgoing"

_PHONE_RE = re.compile(r"\+\d{6,15}")


@dataclass
class ZernioEvent:
    """Распарсенное событие Zernio."""
    event_type: str                    # 'message_received', 'message_sent', 'message_failed', 'message_delivered', 'message_read'
    inbound: Optional[InboundMessage]  # заполнен только для message_received
    conversation_id: str               # conversationId из сообщения
    account_id: str                    # accountId из аккаунта
    provider_message_id: str           # platformMessageId (wamid...) или message.id
    sender_phone: str                  # номер отправителя (для message_sent - номер бизнеса)
    is_from_business: bool             # True если сообщение от бизнеса/оператора
    timestamp: str                     # timestamp события
    raw_payload: dict                  # оригинальный payload для логирования


def parse_zernio_events(payload: dict) -> list[ZernioEvent]:
    """Парсит вебхук Zernio и возвращает список событий.

    Обрабатывает message.received, message.sent, message.failed, message.delivered, message.read.
    Для message.received от клиента возвращает inbound сообщение.
    Для message.sent от бизнеса — помечает is_from_business=True.
    """
    if not isinstance(payload, dict):
        return []

    event = payload.get("event")
    if not event:
        return []

    message = payload.get("message")
    if not isinstance(message, dict):
        return []

    # Только WhatsApp
    if str(message.get("platform") or "").lower() != PLATFORM_WHATSAPP:
        return []

    direction = str(message.get("direction") or "").lower()

    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    account_id = str(account.get("accountId") or account.get("id") or "").strip()

    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    conversation_id = str(
        message.get("conversationId") or conversation.get("id") or ""
    ).strip()

    sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
    sender_phone = _sender_key(sender)

    provider_message_id = str(message.get("platformMessageId") or message.get("id") or "")
    timestamp = str(payload.get("timestamp") or "")

    base = ZernioEvent(
        event_type=event.replace(".", "_"),
        inbound=None,
        conversation_id=conversation_id,
        account_id=account_id,
        provider_message_id=provider_message_id,
        sender_phone=sender_phone,
        is_from_business=False,
        timestamp=timestamp,
        raw_payload=payload,
    )

    if event == EVENT_MESSAGE_RECEIVED:
        return [_parse_received(base, message, sender, payload)]
    elif event == EVENT_MESSAGE_SENT:
        return [_parse_sent(base, message, sender, payload)]
    elif event == EVENT_MESSAGE_FAILED:
        return [_parse_failed(base, message, payload)]
    elif event == EVENT_MESSAGE_DELIVERED:
        return [_parse_delivered(base, message, payload)]
    elif event == EVENT_MESSAGE_READ:
        return [_parse_read(base, message, payload)]

    return []


def _parse_received(base: ZernioEvent, message: dict, sender: dict, payload: dict) -> ZernioEvent:
    """Парсит message.received — входящее от клиента."""
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    if metadata.get("standby") is True:
        logger.info("Zernio: message.received в режиме standby пропущено")
        base.is_from_business = True  # чтобы пропустить в вебхуке
        return base

    # Определить, от бизнеса ли сообщение (бизнес-номер будет проверяться в main.py)
    # Пока ставим флаг, основная проверка — в webhook handler
    text = str(message.get("text") or "").strip()
    content_kind = _content_kind(message, text)

    base.inbound = InboundMessage(
        phone=_sender_key(sender),
        display_name=str(sender.get("name") or sender.get("username") or "").strip(),
        text=text,
        content_kind=content_kind,
        message_id=str(message.get("platformMessageId") or message.get("id") or ""),
        phone_number_id=base.account_id,
        conversation_id=base.conversation_id,
        account_id=base.account_id,
    )
    return base


def _parse_sent(base: ZernioEvent, message: dict, sender: dict, payload: dict) -> ZernioEvent:
    """Парсит message.sent — исходящее от бизнеса/оператора."""
    # Это сообщение от бизнеса (оператор в Inbox, API, или приложение WhatsApp Business)
    base.is_from_business = True
    base.inbound = None  # не обрабатываем как входящее от клиента
    logger.debug("Zernio: message.sent от бизнеса | conv=%s | sender=%s", base.conversation_id, base.sender_phone)
    return base


def _parse_failed(base: ZernioEvent, message: dict, payload: dict) -> ZernioEvent:
    """Парсит message.failed — сообщение не доставлено."""
    base.inbound = None
    logger.warning("Zernio: message.failed | msg_id=%s | conv=%s", base.provider_message_id, base.conversation_id)
    return base


def _parse_delivered(base: ZernioEvent, message: dict, payload: dict) -> ZernioEvent:
    """Парсит message.delivered — сообщение доставлено."""
    base.inbound = None
    logger.debug("Zernio: message.delivered | msg_id=%s | conv=%s", base.provider_message_id, base.conversation_id)
    return base


def _parse_read(base: ZernioEvent, message: dict, payload: dict) -> ZernioEvent:
    """Парсит message.read — сообщение прочитано."""
    base.inbound = None
    logger.debug("Zernio: message.read | msg_id=%s | conv=%s", base.provider_message_id, base.conversation_id)
    return base


def _sender_key(sender: dict) -> str:
    """Ключ диалога: номер (E.164) или BSUID/ID отправителя."""
    phone_number = str(sender.get("phoneNumber") or "").strip()
    if phone_number:
        normalized = normalize_phone(phone_number)
        return normalized if _PHONE_RE.fullmatch(normalized) else phone_number

    sender_id = str(sender.get("id") or "").strip()
    if sender_id and re.fullmatch(r"\d{6,15}", sender_id):
        return "+" + sender_id

    bsuid = str(sender.get("businessScopedUserId") or "").strip()
    return bsuid or sender_id


def _content_kind(message: dict, text: str) -> str:
    """Тип контента: text для текста, иначе тип вложения или unknown."""
    if text:
        return "text"
    attachments = message.get("attachments")
    if isinstance(attachments, list):
        for attachment in attachments:
            if isinstance(attachment, dict) and attachment.get("type"):
                return str(attachment["type"]).strip().lower() or "unknown"
    return "unknown"