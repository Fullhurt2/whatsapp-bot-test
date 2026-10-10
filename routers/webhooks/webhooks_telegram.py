"""routers/webhooks/webhooks_telegram.py — Вебхуки Telegram (клиентские боты и бот владельца JAUAP)."""

import hmac
import json
import logging

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from config.settings import Settings
from storage import (
    MAX_BINDINGS_PER_CLIENT,
    add_tg_binding,
    check_and_add,
    complete_link_code,
    count_bindings,
    remove_tg_binding,
    verify_link_code,
)
from storage.db import fetchall
from whatsapp.telegram_client import TelegramClient
from whatsapp.telegram_payload import parse_telegram_update

logger = logging.getLogger(__name__)

# Заголовок с секретом вебхука Telegram (задаётся при setWebhook)
TELEGRAM_HEADER_SECRET = "X-Telegram-Bot-Api-Secret-Token"


def register_telegram_webhook(app: FastAPI, settings: Settings, state) -> None:
    """Роут вебхука Telegram: POST /webhooks/telegram/{bot_id}."""

    @app.post("/webhooks/telegram/{bot_id}")
    async def telegram_webhook(bot_id: str, request: Request, background_tasks: BackgroundTasks):
        """Приём апдейта Telegram: проверка секрета -> ack 200 -> обработка в фоне."""
        await state.refresh_tenants()
        bundle = state.tenants.get(bot_id)
        if bundle is None:
            logger.warning("Telegram вебхук: неизвестный бот %s", bot_id)
            return JSONResponse(status_code=404, content={"ok": False})
        expected_secret = bundle.settings.telegram_webhook_secret

        if expected_secret:
            provided = request.headers.get(TELEGRAM_HEADER_SECRET, "")
            if not hmac.compare_digest(provided, expected_secret):
                logger.warning("Telegram вебхук отклонён: секрет не совпал (bot_id=%s)", bot_id)
                return JSONResponse(status_code=401, content={"ok": False})
        else:
            logger.warning(
                "Telegram вебхук принят без проверки секретного токена: TELEGRAM_WEBHOOK_SECRET не настроен (bot_id=%s)",
                bot_id,
            )

        raw_body = await request.body()
        try:
            payload = json.loads(raw_body)
        except ValueError:
            logger.warning("Telegram вебхук с невалидным JSON: %d байт", len(raw_body))
            return {"ok": True}

        inbound_messages = parse_telegram_update(payload, bot_id)
        if not inbound_messages:
            return {"ok": True}

        for inbound in inbound_messages:
            raw_id = getattr(inbound, "message_id", None) or ""
            event_id = f"tg:{bot_id}:{raw_id}" if raw_id else ""
            if not check_and_add(event_id):
                logger.debug("Дубликат вебхука Telegram проигнорирован: update_id=%s", raw_id)
                continue

            processor = await state.processor_for(inbound)
            if processor is None:
                logger.warning(
                    "Telegram событие для незарегистрированного бота %s — "
                    "подтверждено без обработки", bot_id,
                )
                continue
            background_tasks.add_task(state.handle_event, inbound, processor)
        return {"ok": True}


def register_telegram_owner_webhook(app: FastAPI, settings: Settings, state) -> None:
    """Вебхук Telegram-бота владельца: POST /webhooks/telegram-owner.

    Обрабатывает команды /start <code> для привязки чатов менеджеров.
    Проверяет секретный токен из настроек (TELEGRAM_OWNER_WEBHOOK_SECRET).
    """

    @app.post("/webhooks/telegram-owner")
    async def telegram_owner_webhook(request: Request, background_tasks: BackgroundTasks):
        """Приём апдейта от Telegram-бота владельца."""
        expected_secret = getattr(settings, "telegram_owner_webhook_secret", "")
        if expected_secret:
            provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if not hmac.compare_digest(provided, expected_secret):
                logger.warning("Telegram-owner вебхук отклонён: секрет не совпал")
                return JSONResponse(status_code=401, content={"ok": False})

        raw_body = await request.body()
        try:
            payload = json.loads(raw_body)
        except ValueError:
            logger.warning("Telegram-owner вебхук с невалидным JSON: %d байт", len(raw_body))
            return {"ok": True}

        update_id = payload.get("update_id")
        if update_id is not None:
            if not check_and_add(f"tg_owner_{update_id}"):
                logger.debug("Дубликат update_id Telegram-owner: %s", update_id)
                return {"ok": True}

        message = payload.get("message")
        if not isinstance(message, dict):
            return {"ok": True}

        text = str(message.get("text") or "").strip()
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = str(chat.get("id") or "")

        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                code = parts[1].strip()
                result = verify_link_code(code)
                if not result:
                    await _owner_bot_reply(settings, chat_id,
                                           "❌ Код неверен или истёк. Получите новый в панели.")
                    return {"ok": True}

                client_key = result["client_key"]
                code_hash = result["code_hash"]
                if count_bindings(client_key) >= MAX_BINDINGS_PER_CLIENT:
                    await _owner_bot_reply(
                        settings, chat_id,
                        f"⚠️ У клиента уже {MAX_BINDINGS_PER_CLIENT} привязанных чатов. "
                        "Отвяжите лишний в панели или удалите этого бота.",
                    )
                    return {"ok": True}
                if not complete_link_code(code_hash, chat_id):
                    await _owner_bot_reply(settings, chat_id,
                                           "⚠️ Этот код уже использован. Попросите новый в панели.")
                    return {"ok": True}
                if add_tg_binding(client_key, chat_id):
                    logger.info("Telegram: чат %s привязан к клиенту %s", chat_id, client_key)
                    await _owner_bot_reply(
                        settings, chat_id,
                        "✅ Подключено! Теперь вы будете получать уведомления о вопросах.",
                    )
                else:
                    await _owner_bot_reply(settings, chat_id,
                                           "Этот чат уже привязан — ничего менять не нужно.")
            else:
                await _owner_bot_reply(
                    settings, chat_id,
                    "👋 Привет! Чтобы подключить уведомления бота, перейдите в панель управления и нажмите «Подключить Telegram» — бот откроется со специальным кодом привязки.",
                )

        elif text.strip() == "/stop":
            removed = 0
            for binding in get_tg_bindings_for_notify_all():
                if binding["chat_id"] == chat_id and remove_tg_binding(binding["client_key"], binding["id"]):
                    removed += 1
            if removed:
                logger.info("Telegram: чат %s отвязан от %d клиентов", chat_id, removed)
                await _owner_bot_reply(settings, chat_id, "✅ Чат отвязан.")
            else:
                await _owner_bot_reply(settings, chat_id, "Этот чат и так ни к чему не привязан.")

        return {"ok": True}


def get_tg_bindings_for_notify_all() -> list[dict]:
    """Все привязки во всех клиентах — нужно для /stop."""
    return [dict(row) for row in fetchall("SELECT id, client_key, chat_id FROM tg_bindings")]


async def _owner_bot_reply(settings: Settings, chat_id: str, text: str) -> None:
    """Ответ в чат менеджера от имени бота JAUAP (ошибки не роняют вебхук)."""
    bot = TelegramClient(settings.telegram_owner_bot_token)
    try:
        await bot.send_text(chat_id, text)
    except Exception:
        logger.exception("Не удалось ответить в Telegram (%s)", chat_id)
    finally:
        await bot.close()
