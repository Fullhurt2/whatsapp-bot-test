"""routers/webhooks/webhooks_zernio.py — Вебхуки Zernio WhatsApp API."""

import json
import logging
import re
import time

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from config.settings import Settings
from services.cache import TTLCache
from storage import (
    add_message,
    check_and_add,
    get_message_by_provider_id,
    get_open_handoff,
    mark_first_human_reply,
    update_delivery_status,
    update_last_message_times,
)
from storage.db import execute, fetchone
from whatsapp.zernio_payload import parse_zernio_events
from whatsapp.zernio_security import (
    HEADER_SIGNATURE as ZERNIO_HEADER_SIGNATURE,
    HEADER_SIGNATURE_LEGACY as ZERNIO_HEADER_SIGNATURE_LEGACY,
    verify_zernio_signature,
)

logger = logging.getLogger(__name__)

# Кэш бизнес-номеров по accountId (для фильтрации message.sent от бизнеса)
_business_phone_cache: TTLCache[str] = TTLCache(maxsize=1000, ttl=900.0)


async def _get_business_phone(account_id: str, zernio_client) -> str:
    """Бизнес-номер из кэша или Zernio API."""
    cached = _business_phone_cache.get(account_id)
    if cached is not None:
        return cached

    phone = ""
    try:
        info = await zernio_client.get_number_info()
        phone_obj = info.get("phone") if isinstance(info.get("phone"), dict) else {}
        phone = str(
            phone_obj.get("display_phone_number")
            or info.get("phoneNumber")
            or info.get("username")
            or ""
        ).strip()
    except Exception:
        logger.debug("Не удалось получить бизнес-номер из Zernio", exc_info=True)
        return ""

    if phone:
        _business_phone_cache.set(account_id, phone)
    return phone


async def _handle_zernio_inbound(state, event, processor) -> None:
    """Фоновая обработка входящего Zernio: фильтр «от бизнеса» + сценарий бота."""
    try:
        business_phone = await _get_business_phone(event.account_id, processor.sender)
        sender_digits = re.sub(r"\D", "", event.sender_phone or "")
        business_digits = re.sub(r"\D", "", business_phone or "")
        if business_digits and sender_digits == business_digits:
            logger.debug("Zernio: message.received от бизнеса пропущено | sender=%s", event.sender_phone)
            return
        await state.handle_event(event.inbound, processor)
    except Exception:
        logger.exception("Ошибка обработки входящего Zernio (account=%s)", event.account_id)


def _process_zernio_sent(event) -> None:
    """Синхронная обработка отправленного сообщения Zernio (вызывается в фоне)."""
    try:
        if event.provider_message_id:
            update_delivery_status(event.provider_message_id, "sent")

        if event.conversation_id:
            row = fetchone(
                "SELECT id FROM conversations WHERE zernio_conversation_id = ? AND client_key = ?",
                (event.conversation_id, event.account_id),
            )
            if row:
                conv_id = row["id"]
                update_last_message_times(conv_id, is_client=False)

                raw_msg = (event.raw_payload.get("message") or {}) if isinstance(event.raw_payload, dict) else {}
                event_text = str(raw_msg.get("text") or "").strip()

                is_bot_message = False
                if event.provider_message_id:
                    existing = get_message_by_provider_id(event.provider_message_id)
                    if existing and existing.get("role") == "bot":
                        is_bot_message = True

                matched_bot_msg = None
                if not is_bot_message and event_text:
                    matched_bot_msg = fetchone(
                        "SELECT id, role, text, provider_message_id FROM messages WHERE conversation_id = ? AND role = 'bot' AND text = ? ORDER BY id DESC LIMIT 1",
                        (conv_id, event_text),
                    )
                    if matched_bot_msg:
                        is_bot_message = True
                        if event.provider_message_id and not matched_bot_msg["provider_message_id"]:
                            execute(
                                "UPDATE messages SET provider_message_id = ?, delivery_status = 'sent' WHERE id = ?",
                                (event.provider_message_id, matched_bot_msg["id"]),
                            )

                last_msg = fetchone(
                    "SELECT id, role, text, provider_message_id FROM messages WHERE conversation_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                    (conv_id,),
                )

                if not is_bot_message:
                    existing_human = None
                    if event.provider_message_id:
                        existing_human = get_message_by_provider_id(event.provider_message_id)

                    if not existing_human and last_msg and last_msg["role"] == "human":
                        if event_text and last_msg["text"] == event_text:
                            existing_human = last_msg
                            if event.provider_message_id:
                                execute(
                                    "UPDATE messages SET provider_message_id = ?, delivery_status = 'sent' WHERE id = ?",
                                    (event.provider_message_id, last_msg["id"]),
                                )

                    if not existing_human and event_text:
                        add_message(
                            conversation_id=conv_id,
                            role="human",
                            text=event_text,
                            content_kind="text",
                            provider_message_id=event.provider_message_id,
                            delivery_status="sent",
                        )

                    open_handoff = get_open_handoff(conv_id)
                    if open_handoff:
                        mark_first_human_reply(open_handoff["id"])
    except Exception:
        logger.exception("Ошибка обработки message_sent в Zernio (account=%s)", event.account_id)


def register_zernio_webhook(app: FastAPI, settings: Settings, state) -> None:
    @app.post("/webhooks/zernio")
    async def zernio_webhook(request: Request, background_tasks: BackgroundTasks):
        """Приём вебхука Zernio: проверка X-Zernio-Signature -> ack 200 -> фон."""
        raw_body = await request.body()
        received_at = time.monotonic()
        signature = (
            request.headers.get(ZERNIO_HEADER_SIGNATURE, "")
            or request.headers.get(ZERNIO_HEADER_SIGNATURE_LEGACY, "")
        )
        if not verify_zernio_signature(settings.zernio_webhook_secret, raw_body, signature):
            logger.warning("Вебхук Zernio отклонён: подпись не прошла проверку")
            return JSONResponse(status_code=401, content={"error": "invalid signature"})

        try:
            payload = json.loads(raw_body)
        except ValueError:
            logger.warning("Вебхук Zernio с невалидным JSON: %d байт", len(raw_body))
            return {"ok": True}

        events = parse_zernio_events(payload)
        logger.info(
            "Вебхук Zernio разобран | событий=%d | %.3f с",
            len(events), time.monotonic() - received_at,
        )
        if not events:
            return {"ok": True}

        for event in events:
            raw_id = event.provider_message_id or event.timestamp or ""
            event_id = f"zernio:{event.event_type}:{raw_id}" if raw_id else ""
            if event_id and not check_and_add(event_id):
                logger.debug("Дубликат вебхука Zernio проигнорирован: event_id=%s", event_id)
                continue

            class MockInbound:
                phone_number_id = event.account_id
                account_id = event.account_id

            processor = await state.processor_for(MockInbound())
            if processor is None:
                logger.warning(
                    "Событие для незарегистрированного аккаунта Zernio: "
                    "account_id=%s — подтверждено без обработки",
                    event.account_id,
                )
                continue

            if event.event_type == "message_received":
                if event.inbound is None:
                    continue
                background_tasks.add_task(
                    _handle_zernio_inbound, state, event, processor
                )

            elif event.event_type == "message_sent":
                background_tasks.add_task(_process_zernio_sent, event)

            elif event.event_type == "message_failed":
                if event.provider_message_id:
                    background_tasks.add_task(update_delivery_status, event.provider_message_id, "failed")

            elif event.event_type == "message_delivered":
                if event.provider_message_id:
                    background_tasks.add_task(update_delivery_status, event.provider_message_id, "delivered")

            elif event.event_type == "message_read":
                if event.provider_message_id:
                    background_tasks.add_task(update_delivery_status, event.provider_message_id, "read")

        logger.info(
            "Вебхук Zernio: ack через %.3f с | событий=%d",
            time.monotonic() - received_at, len(events),
        )
        return {"ok": True}
