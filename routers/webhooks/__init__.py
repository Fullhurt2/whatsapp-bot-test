"""routers.webhooks package"""
from routers.webhooks.webhooks_meta import register_meta_webhook
from routers.webhooks.webhooks_zernio import register_zernio_webhook
from routers.webhooks.webhooks_telegram import (
    register_telegram_webhook,
    register_telegram_owner_webhook,
)

__all__ = [
    "register_meta_webhook",
    "register_zernio_webhook",
    "register_telegram_webhook",
    "register_telegram_owner_webhook",
]
