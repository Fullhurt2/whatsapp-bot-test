"""Общие исключения транспорта WhatsApp.

Один тип ошибок для всех провайдеров (Zernio, Meta Cloud API, Telegram):
хендлеры ловят MessagingError и не зависят от того, какой провайдер включён
в .env.
"""

from __future__ import annotations


class MessagingError(Exception):
    """Любая ошибка отправки через активный WhatsApp-провайдер."""


class MessagingTimeout(MessagingError):
    """Провайдер не ответил за отведённое время."""
