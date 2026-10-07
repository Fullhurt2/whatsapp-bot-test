"""Общая модель входящего сообщения для всех провайдеров.

Вебхуки Meta, Zernio и Telegram разбираются в один и тот же InboundMessage —
хендлеры и память диалога работают только с ним.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class InboundMessage:
    """Нормализованное входящее сообщение клиента."""

    phone: str          # номер клиента (E.164) — ключ диалога
    display_name: str   # имя в WhatsApp (может быть пустым)
    text: str           # текст сообщения; "" для нетекстового контента
    content_kind: str   # text / interactive / image / ... / unknown
    message_id: str     # id сообщения провайдера (wam_... / wamid....) — для логов
    # Ключ маршрутизации клиента: у Meta — ID бизнес-номера (value.metadata),
    # у Zernio — accountId подключённого аккаунта; "" для Telegram.
    phone_number_id: str = ""
    # Zernio: id диалога, в который уходит ответ (POST .../conversations/{id}/
    # messages). Без него Zernio не может отправить свободный текст.
    conversation_id: str = ""
    # Zernio: accountId аккаунта-получателя (обязателен в теле запросов).
    account_id: str = ""
    # Медиа-вложения (если есть)
    media_url: str = ""
    media_mime: str = ""
    media_caption: str = ""
    media_duration_s: float = 0.0
