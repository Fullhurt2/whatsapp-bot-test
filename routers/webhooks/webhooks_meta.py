"""routers/webhooks/webhooks_meta.py — Вебхуки Meta WhatsApp Cloud API."""

import json
import logging

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from config.settings import Settings
from storage import check_and_add
from whatsapp.meta_payload import parse_meta_events
from whatsapp.meta_security import (
    META_HEADER_SIGNATURE,
    QUERY_CHALLENGE,
    QUERY_MODE,
    QUERY_VERIFY_TOKEN,
    verify_meta_signature,
    verify_subscription,
)

logger = logging.getLogger(__name__)


def register_meta_webhook(app: FastAPI, settings: Settings, state) -> None:
    """Роуты вебхука Meta: GET-верификация подписки + POST событий."""

    @app.get("/webhooks/meta")
    async def meta_verify(request: Request):
        """Привязка вебхука в дашборде Meta: echo challenge при верном токене."""
        params = request.query_params
        if not verify_subscription(
            hub_mode=params.get(QUERY_MODE, ""),
            verify_token=params.get(QUERY_VERIFY_TOKEN, ""),
            expected_token=settings.meta_verify_token,
        ):
            logger.warning("Привязка вебхука Meta отклонена: verify_token не совпал")
            return PlainTextResponse("forbidden", status_code=403)
        return PlainTextResponse(params.get(QUERY_CHALLENGE, ""))

    @app.post("/webhooks/meta")
    async def meta_webhook(request: Request, background_tasks: BackgroundTasks):
        """Приём вебхука Meta: проверка X-Hub-Signature-256 -> ack 200 -> фон."""
        raw_body = await request.body()
        signature = request.headers.get(META_HEADER_SIGNATURE, "")
        if not verify_meta_signature(settings.meta_app_secret, raw_body, signature):
            logger.warning("Вебхук Meta отклонён: подпись не прошла проверку")
            return JSONResponse(status_code=401, content={"error": "invalid signature"})

        try:
            payload = json.loads(raw_body)
        except ValueError:
            logger.warning("Вебхук Meta с невалидным JSON: %d байт", len(raw_body))
            return {"ok": True}

        inbound_messages = parse_meta_events(payload)
        if not inbound_messages:
            return {"ok": True}

        for inbound in inbound_messages:
            event_id = getattr(inbound, "message_id", None) or ""
            if not check_and_add(event_id):
                logger.debug("Дубликат вебхука Meta проигнорирован: message_id=%s", event_id)
                continue

            processor = await state.processor_for(inbound)
            if processor is None:
                logger.warning(
                    "Событие для незарегистрированного номера: phone_number_id=%s "
                    "— подтверждено без обработки (клиента нет в clients/)",
                    inbound.phone_number_id or "-",
                )
                continue
            background_tasks.add_task(state.handle_event, inbound, processor)
        return {"ok": True}
