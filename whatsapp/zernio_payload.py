"""Разбор входящих событий вебхука Zernio (событие message.received).

Zernio POST'ит на наш вебхук конверт вида:

    {
      "id": "3f0c1c2e-...",                  # id события (дедуп)
      "event": "message.received",
      "message": {
        "id": "...", "conversationId": "...", "platform": "whatsapp",
        "platformMessageId": "wamid...", "direction": "incoming",
        "text": "Привет",
        "attachments": [{"type": "image", "url": "..."}],
        "sender": {"id": "77770000001", "phoneNumber": "+77770000001",
                   "name": "Аня", "businessScopedUserId": "..."}
      },
      "conversation": {"id": "..."},
      "account": {"accountId": "66b2e19d8c3f5a7e9d0b1c2d", "profileId": "..."},
      "metadata": {...},
      "timestamp": "2027-01-04T14:00:03Z"
    }

Обрабатываем только входящие текстовые/медиа-сообщения WhatsApp. Ключ
маршрутизации в мультитенанте — account.accountId (он же кладётся в
phone_number_id — общий слот «ключ клиента» из InboundMessage). Для ответа
нужен message.conversationId, поэтому он едет в InboundMessage.conversation_id.

Здесь только разбор, без бизнес-логики (см. tests/test_zernio_payload.py).
"""

import logging
import re

from config.settings import normalize_phone
from whatsapp.inbound import InboundMessage

logger = logging.getLogger(__name__)

EVENT_MESSAGE_RECEIVED = "message.received"
PLATFORM_WHATSAPP = "whatsapp"
DIRECTION_INCOMING = "incoming"

_PHONE_RE = re.compile(r"\+\d{6,15}")


def parse_zernio_events(payload: dict) -> list[InboundMessage]:
    """Извлекает входящее WhatsApp-сообщение из конверта вебхука Zernio.

    Возвращает список из 0 или 1 элемента: событие message.received несёт
    одно сообщение. Служебные события, не-WhatsApp платформы, исходящие
    сообщения и «standby» (ответ ведёт Meta Business Agent) игнорируются —
    вебхук их молча подтверждает 200.
    """
    if not isinstance(payload, dict):
        return []
    if payload.get("event") != EVENT_MESSAGE_RECEIVED:
        return []

    message = payload.get("message")
    if not isinstance(message, dict):
        return []
    if str(message.get("platform") or "").lower() != PLATFORM_WHATSAPP:
        return []
    if str(message.get("direction") or "").lower() != DIRECTION_INCOMING:
        return []

    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    if metadata.get("standby") is True:
        # Ответ клиенту ведёт агент Meta — наш бот в этот диалог не вмешивается.
        logger.info("Zernio: входящее в режиме standby пропущено")
        return []

    account = payload.get("account") if isinstance(payload.get("account"), dict) else {}
    account_id = str(account.get("accountId") or account.get("id") or "").strip()

    conversation = payload.get("conversation") if isinstance(payload.get("conversation"), dict) else {}
    conversation_id = str(
        message.get("conversationId") or conversation.get("id") or ""
    ).strip()

    sender = message.get("sender") if isinstance(message.get("sender"), dict) else {}
    phone = _sender_key(sender)
    if not phone:
        logger.warning("Zernio: сообщение без идентификатора отправителя — пропущено")
        return []

    text = str(message.get("text") or "").strip()
    content_kind = _content_kind(message, text)

    return [InboundMessage(
        phone=phone,
        display_name=str(sender.get("name") or sender.get("username") or "").strip(),
        text=text,
        content_kind=content_kind,
        message_id=str(message.get("platformMessageId") or message.get("id") or ""),
        phone_number_id=account_id,   # ключ маршрутизации клиента (accountId Zernio)
        conversation_id=conversation_id,
        account_id=account_id,
    )]


def _sender_key(sender: dict) -> str:
    """Ключ диалога клиента: номер (E.164) или BSUID, если номера нет.

    С апреля 2026 Meta может не отдавать номер (пользователи с username) —
    тогда стабильный якорь это businessScopedUserId; для диалога подходит
    любая стабильная строка, т.к. ответ уходит по conversationId, а не по
    номеру.
    """
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
