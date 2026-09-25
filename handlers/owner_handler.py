"""Уведомления владельцу бизнеса о вопросах, требующих живого человека.

Владелец получает WhatsApp-сообщение от бизнес-номера (через Bird) на номер
owner_whatsapp_phone из конфига клиента (или OWNER_WHATSAPP_NUMBER из .env).
"""

import logging

from config.settings import Settings
from whatsapp.bird_client import BirdError, BirdWhatsAppClient

logger = logging.getLogger(__name__)


async def notify_owner(
    bird: BirdWhatsAppClient,
    settings: Settings,
    client_phone: str,
    display_name: str,
    message_text: str,
    reason: str,
) -> bool:
    """Пересылает владельцу сообщение клиента и его данные.

    Возвращает True, если уведомление доставлено.
    """
    if not settings.owner_phone:
        logger.warning(
            "Уведомление владельцу пропущено: owner_whatsapp_phone не задан "
            "(в client_config.yaml или OWNER_WHATSAPP_NUMBER в .env)"
        )
        return False

    who = f"{display_name or 'клиент'} ({client_phone})"
    text = (
        f"🔔 Вопрос вне базы знаний ({reason})\n"
        f"От: {who}\n"
        f"Сообщение: {message_text}"
    )
    try:
        await bird.send_text(settings.owner_phone, text)
        return True
    except BirdError:
        # Падение уведомления не должно ломать диалог с клиентом — логируем.
        logger.exception("Не удалось отправить уведомление владельцу (%s)", settings.owner_phone)
        return False
