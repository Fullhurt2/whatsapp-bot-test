"""Уведомления владельцу бизнеса о вопросах, требующих живого человека.

Владелец получает сообщение через настроенные каналы:
- Telegram (если привязан и включен в notify_channels)
- WhatsApp (если включен в notify_channels)

Особенность Zernio: свободный текст вне 24-часового окна WhatsApp запрещён,
поэтому уведомление уходит approved-шаблоном (owner_template_name/
owner_template_language из yaml клиента, две переменные тела: отправитель
и сообщение). Если шаблон не задан или у sender нет send_template — пробуем
обычный send_text (в диалоге, где окно открыто, это сработает).

Для NO_ANSWER: silent уведомление (disable_notification в Telegram).
Троттлинг: не чаще 1 уведомления на диалог за 10 минут.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from config.settings import Settings
from whatsapp.errors import MessagingError
from storage import get_open_handoff

logger = logging.getLogger(__name__)

# In-memory троттлинг: (conversation_id, reason) -> last_notified_at
_notification_throttle: dict[tuple[str, str], datetime] = {}
THROTTLE_MINUTES = 10


async def notify_owner(
    sender,
    settings: Settings,
    client_phone: str,
    display_name: str,
    message_text: str,
    reason: str,
    conversation_id: str = "",
) -> bool:
    """Пересылает владельцу сообщение клиента и его данные.

    Параметры:
    - sender: клиент отправки (Meta/Zernio/Telegram)
    - settings: Settings клиента
    - client_phone: номер/ID клиента
    - display_name: имя клиента
    - message_text: текст для владельца (сводка или исходное сообщение)
    - reason: причина уведомления (booking, complaint, human_requested, no_answer, llm_error, llm_timeout, keyword:...)
    - conversation_id: ID диалога (для троттлинга)

    Возвращает True, если уведомление доставлено хотя бы в один канал.
    """
    # Определяем каналы уведомлений с fallback на старую логику
    notify_channels = getattr(settings, "notify_channels", None)
    
    # Собираем доступные контакты
    has_telegram = bool(getattr(settings, "owner_telegram_chat_id", ""))
    has_whatsapp = bool(getattr(settings, "owner_phone", ""))
    
    if notify_channels is None:
        # Старая логика: Telegram если есть owner_telegram_chat_id, иначе WhatsApp
        if has_telegram:
            notify_channels = ["telegram"]
        elif has_whatsapp:
            notify_channels = ["whatsapp"]
        else:
            notify_channels = []
    elif isinstance(notify_channels, str):
        notify_channels = [notify_channels]
    
    # Фильтруем каналы: оставляем только те, для которых есть контакты
    notify_channels = [ch for ch in notify_channels 
                       if (ch == "telegram" and has_telegram) or (ch == "whatsapp" and has_whatsapp)]
    
    # Если после фильтрации каналов нет — пробуем старую логику как fallback
    if not notify_channels:
        if has_telegram:
            notify_channels = ["telegram"]
        elif has_whatsapp:
            notify_channels = ["whatsapp"]
        else:
            notify_channels = []

    # Для NO_ANSWER проверяем режим уведомления
    if reason == "no_answer":
        notify_on_no_answer = getattr(settings, "notify_on_no_answer", "silent")
        if notify_on_no_answer == "off":
            return False

    if not notify_channels:
        logger.warning("Уведомление пропущено: нет каналов доставки (owner_phone/owner_telegram_chat_id)")
        return False

    # Троттлинг: не чаще 1 уведомления на диалог за 10 минут
    if conversation_id:
        throttle_key = (conversation_id, reason)
        now = datetime.utcnow()
        last = _notification_throttle.get(throttle_key)
        if last and now - last < timedelta(minutes=THROTTLE_MINUTES):
            logger.debug("Уведомление заторможено: %s", throttle_key)
            return False
        _notification_throttle[throttle_key] = now

    who = f"{display_name or 'клиент'} ({client_phone})"
    text = (
        f"🔔 Вопрос вне базы знаний ({reason})\n"
        f"От: {who}\n"
        f"Сообщение: {message_text}"
    )

    delivered_any = False

    # Отправка в Telegram
    if "telegram" in notify_channels:
        chat_ids = getattr(settings, "owner_telegram_chat_id", "") or ""
        if chat_ids:
            for chat_id in [c.strip() for c in chat_ids.split(",") if c.strip()]:
                try:
                    disable_notification = (reason == "no_answer" and
                                            getattr(settings, "notify_on_no_answer", "silent") == "silent")
                    if hasattr(sender, "send_text"):
                        await _send_telegram_with_options(sender, chat_id, text, disable_notification)
                    else:
                        await sender.send_text(chat_id, text)
                    delivered_any = True
                except MessagingError:
                    logger.exception("Не удалось отправить уведомление в Telegram (%s)", chat_id)

    # Отправка в WhatsApp (Meta/Zernio)
    if "whatsapp" in notify_channels:
        phone = getattr(settings, "owner_phone", "") or ""
        if phone:
            try:
                template_name = settings.owner_template_name
                if template_name and hasattr(sender, "send_template"):
                    await sender.send_template(
                        to=phone,
                        template_name=template_name,
                        language=settings.owner_template_language or "ru",
                        params=[who, message_text],
                    )
                    delivered_any = True
                elif hasattr(sender, "send_template"):
                    # Zernio: свободный текст без conversation_id невозможен —
                    # попытка send_text гарантированно упадёт. Нужен шаблон.
                    logger.error(
                        "Уведомление владельцу не отправлено: у Zernio свободный текст "
                        "вне 24-часового окна запрещён. Создайте approved-шаблон в Zernio "
                        "(переменные: {{1}} — отправитель, {{2}} — сообщение) и задайте "
                        "owner_template_name/owner_template_language в «Служебном» клиента"
                    )
                else:
                    await sender.send_text(phone, text)
                    delivered_any = True
            except MessagingError:
                logger.exception("Не удалось отправить уведомление в WhatsApp (%s)", phone)

    return delivered_any


async def _send_telegram_with_options(sender, chat_id: str, text: str, disable_notification: bool = False) -> None:
    """Отправка в Telegram с опцией disable_notification (silent message).
    Если sender не поддерживает этот параметр — fallback на send_text.
    """
    try:
        # Пробуем вызвать с disable_notification
        await sender.send_text(chat_id, text, disable_notification=disable_notification)
    except TypeError:
        # Старый sender без этого параметра
        await sender.send_text(chat_id, text)
