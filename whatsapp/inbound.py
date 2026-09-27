"""Общая модель входящего сообщения для обоих провайдеров.

Оба вебхука (Bird и Meta) разбираются в один и тот же InboundMessage —
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
    # ID бизнес-номера, на который пришло сообщение (Meta: value.metadata).
    # В мультитенанте — ключ маршрутизации к конфигу клиента; "" для Bird.
    phone_number_id: str = ""
