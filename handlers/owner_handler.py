"""Уведомления владельцу бизнеса о вопросах, требующих живого человека.

Владелец получает сообщение через настроенные каналы:
- Telegram (чаты, привязанные в панели, + owner_telegram_chat_id из yaml);
- WhatsApp (если включён в notify_channels).

Telegram-уведомления уходят общим ботом JAUAP (telegram_owner_bot_token) —
он видит всех клиентов; если он не задан, откат на sender клиента. К уведомлению
прикрепляется кнопка «Открыть диалог» — ссылка прямо на нужный диалог в панели
(нужен PUBLIC_BASE_URL).

Особенность Zernio: свободный текст вне 24-часового окна WhatsApp запрещён,
поэтому уведомление уходит approved-шаблоном (owner_template_name/
owner_template_language из yaml клиента, две переменные тела: отправитель
и сообщение). Если шаблон не задан — сообщаем в лог, что его нужно создать,
и уведомление не доставляем.

Для NO_ANSWER: silent уведомление (disable_notification в Telegram).
Троттлинг: не чаще 1 уведомления на диалог и причину за 10 минут.
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


# Текст кнопки в уведомлении менеджеру
OPEN_CHAT_BUTTON_TEXT = "💬 Открыть диалог"


def _notify_recipients(settings: Settings) -> list[str]:
    """Чаты для Telegram-уведомлений: привязки из панели + ручной chat_id из yaml.

    Привязки хранятся в БД по ключу клиента; для zernio это accountId — тот же,
    под которым лежат диалоги, поэтому уведомление найдёт нужный диалог.
    """
    chat_ids: list[str] = []
    client_key = (
        getattr(settings, "whatsapp_phone_number_id", "")
        or getattr(settings, "zernio_account_id", "")
        or ""
    )
    if client_key:
        try:
            from storage import get_tg_bindings_for_notify
            chat_ids.extend(get_tg_bindings_for_notify(client_key))
        except Exception:
            logger.exception("Не удалось прочитать привязки Telegram (%s)", client_key)

    manual = str(getattr(settings, "owner_telegram_chat_id", "") or "")
    for chat_id in [c.strip() for c in manual.split(",") if c.strip()]:
        if chat_id not in chat_ids:
            chat_ids.append(chat_id)
    return chat_ids


def _dialog_button(settings: Settings, conversation_id: str) -> dict | None:
    """Inline-кнопка «Открыть диалог» — ведёт в панель на нужный диалог.

    Без PUBLIC_BASE_URL адрес панели неизвестен, поэтому кнопку не вешаем.
    """
    base_url = str(getattr(settings, "public_base_url", "") or "").strip().rstrip("/")
    if not base_url or not conversation_id:
        return None
    return {
        "inline_keyboard": [[{
            "text": OPEN_CHAT_BUTTON_TEXT,
            "url": f"{base_url}/admin#chat={conversation_id}",
        }]],
    }


async def _send_telegram_notification(
    sender,
    settings: Settings,
    chat_id: str,
    text: str,
    reply_markup: dict | None,
    disable_notification: bool,
) -> None:
    """Отправка уведомления в Telegram.

    Приоритет у общего бота JAUAP (telegram_owner_bot_token): он видит все
    клиенты. Если он не задан — откат на sender клиента (и тогда это должен
    быть Telegram-клиент; у WhatsApp-клиента чат_id в отправителе бессмыслен).
    """
    owner_token = str(getattr(settings, "telegram_owner_bot_token", "") or "").strip()
    if owner_token:
        from whatsapp.telegram_client import TelegramClient
        bot = TelegramClient(owner_token)
        try:
            await bot.send_text(
                chat_id,
                text,
                reply_markup=reply_markup,
                disable_notification=disable_notification,
            )
            return
        finally:
            await bot.close()

    if hasattr(sender, "send_text"):
        try:
            await sender.send_text(chat_id, text, disable_notification=disable_notification)
        except TypeError:
            # Старый клиент без параметра тихой доставки
            await sender.send_text(chat_id, text)
        return
    await sender.send_text(chat_id, text)


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
    
    # Собираем доступные контакты. Для Telegram это привязки из панели плюс
    # ручной chat_id из yaml — поэтому канал считаем доступным, если есть хоть один.
    telegram_chats = _notify_recipients(settings)
    has_telegram = bool(telegram_chats)
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

    # Отправка в Telegram: привязанные менеджерам чаты + ручной chat_id
    if "telegram" in notify_channels and telegram_chats:
        reply_markup = _dialog_button(settings, conversation_id)
        silent = (
            reason == "no_answer"
            and getattr(settings, "notify_on_no_answer", "silent") == "silent"
        )
        for chat_id in telegram_chats:
            try:
                await _send_telegram_notification(
                    sender, settings, chat_id, text, reply_markup, silent,
                )
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
