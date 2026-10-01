"""Уведомления владельцу бизнеса о вопросах, требующих живого человека.

Владелец получает сообщение тем же транспортом, что и клиенты: у WhatsApp-
клиента — на owner_whatsapp_phone, у Telegram-клиента — в чат
owner_telegram_chat_id (см. Settings.owner_notify_target).
"""

import logging

from config.settings import Settings
from whatsapp.errors import MessagingError

logger = logging.getLogger(__name__)


async def notify_owner(
    sender,
    settings: Settings,
    client_phone: str,
    display_name: str,
    message_text: str,
    reason: str,
) -> bool:
    """Пересылает владельцу сообщение клиента и его данные.

    Возвращает True, если уведомление доставлено.
    """
    target = settings.owner_notify_target()
    if not target:
        logger.warning(
            "Уведомление владельцу пропущено: не задан получатель "
            "(owner_telegram_chat_id для Telegram или owner_whatsapp_phone "
            "для WhatsApp — в конфиге клиента)"
        )
        return False

    who = f"{display_name or 'клиент'} ({client_phone})"
    text = (
        f"🔔 Вопрос вне базы знаний ({reason})\n"
        f"От: {who}\n"
        f"Сообщение: {message_text}"
    )
    try:
        await sender.send_text(target, text)
        return True
    except MessagingError:
        # Падение уведомления не должно ломать диалог с клиентом — логируем.
        logger.exception("Не удалось отправить уведомление владельцу (%s)", target)
        return False
