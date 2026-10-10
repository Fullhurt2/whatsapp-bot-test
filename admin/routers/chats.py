"""Эндпоинты диалогов (список, сообщения, медиа, отправка оператором, переключение режима)."""

import json
import logging
import os
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

from admin.routers.common import (
    _authorize,
    _check_conv_ownership,
    _client_provider,
    _client_pids,
    _client_yaml_path,
    _conversation_client,
    _conversation_client_key,
)
from config.settings import Settings
from storage import (
    add_message,
    get_conversation,
    get_last_client_message_at,
    get_message,
    get_message_by_provider_id,
    get_messages,
    get_open_handoff,
    list_conversations,
    mark_first_human_reply,
    mark_read,
    resolve_conversation_handoffs,
    update_conversation_status,
    update_last_message_times,
)
from whatsapp.errors import MessagingError

logger = logging.getLogger(__name__)


def register_chats_routes(app, settings: Settings, state, clients_dir: Path) -> None:
    @app.get("/admin/clients/{pid}/conversations")
    async def get_conversations(pid: str, request: Request,
                                status: str | None = None,
                                q: str | None = None,
                                cursor: str | None = None,
                                since: str | None = None,
                                limit: int = 50):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})

        if status and status not in ("bot", "manual"):
            return JSONResponse(status_code=400, content={"error": "status должен быть 'bot' или 'manual'"})
        if limit < 1 or limit > 100:
            limit = 50

        convs = list_conversations(_conversation_client_key(clients_dir, pid), status=status, q=q, cursor=cursor, since=since, limit=limit)
        return {"conversations": convs, "count": len(convs)}

    @app.get("/admin/clients/{pid}/conversations/{cid}/messages")
    async def get_conversation_messages(pid: str, cid: str, request: Request,
                                        before: str | None = None,
                                        limit: int = 50):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        conv = get_conversation(cid)
        if not _check_conv_ownership(conv, _conversation_client_key(clients_dir, pid)):
            return JSONResponse(status_code=404, content={"error": "диалог не найден"})

        if limit < 1 or limit > 200:
            limit = 50

        msgs = get_messages(cid, before=before, limit=limit)
        return {"messages": msgs, "count": len(msgs)}

    @app.get("/admin/clients/{pid}/conversations/{cid}/messages/{mid}/media")
    async def get_message_media(pid: str, cid: str, mid: int, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        conv = get_conversation(cid)
        if not _check_conv_ownership(conv, _conversation_client_key(clients_dir, pid)):
            return JSONResponse(status_code=404, content={"error": "диалог не найден"})

        msg = get_message(mid)
        if not msg or msg.get("conversation_id") != cid:
            return JSONResponse(status_code=404, content={"error": "сообщение не найдено"})

        media_path = msg.get("media_path")
        media_status = msg.get("media_status")
        if media_status == "expired" or not media_path:
            return JSONResponse(status_code=404, content={"error": "Файл удалён по истечении срока хранения"})

        if not os.path.exists(media_path):
            return JSONResponse(status_code=404, content={"error": "Файл не найден на сервере"})

        try:
            import tempfile
            resolved_file = Path(media_path).resolve()
            resolved_base = Path(settings.media_dir).resolve()
            resolved_tmp = Path(tempfile.gettempdir()).resolve()
            is_in_media = resolved_file.is_relative_to(resolved_base) if hasattr(resolved_file, "is_relative_to") else str(resolved_file).startswith(str(resolved_base))
            is_in_tmp = resolved_file.is_relative_to(resolved_tmp) if hasattr(resolved_file, "is_relative_to") else str(resolved_file).startswith(str(resolved_tmp))
            if not (is_in_media or is_in_tmp):
                logger.warning("Попытка Path Traversal через media_path: %s", media_path)
                return JSONResponse(status_code=403, content={"error": "нет доступа"})
        except Exception:
            return JSONResponse(status_code=403, content={"error": "нет доступа"})

        mime = str(msg.get("media_mime") or "application/octet-stream").strip().lower()
        if mime == "image/svg+xml" or not (mime.startswith("audio/") or mime.startswith("image/")):
            mime = "application/octet-stream"

        return FileResponse(
            media_path,
            media_type=mime,
            headers={"X-Content-Type-Options": "nosniff"},
        )

    @app.post("/admin/clients/{pid}/conversations/{cid}/messages/{mid}/retry-media")
    async def retry_message_media(pid: str, cid: str, mid: int, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        conv = get_conversation(cid)
        if not _check_conv_ownership(conv, _conversation_client_key(clients_dir, pid)):
            return JSONResponse(status_code=404, content={"error": "диалог не найден"})

        msg = get_message(mid)
        if not msg or msg.get("conversation_id") != cid:
            return JSONResponse(status_code=404, content={"error": "сообщение не найдено"})

        media_path = msg.get("media_path")
        if not media_path or not os.path.exists(media_path):
            return JSONResponse(status_code=404, content={"error": "Исходный файл не найден на сервере"})

        db_key = _conversation_client_key(clients_dir, pid)
        bundle = state.tenants.get(db_key) or state.tenants.get(pid)
        t_settings = bundle.settings if bundle else settings

        content_kind = msg.get("content_kind")
        from services.media import transcribe_audio, describe_image
        with open(media_path, "rb") as f:
            data = f.read()

        try:
            if content_kind in ("voice", "audio"):
                hint = f"{t_settings.business_name}. {t_settings.knowledge_base[:300]}"
                res = await transcribe_audio(
                    data=data,
                    mime=msg.get("media_mime") or "audio/ogg",
                    language=t_settings.language,
                    hint=hint,
                    settings=t_settings,
                    retry=False,
                )
                text = f"[Голосовое сообщение] {res.text.strip()}"
            elif content_kind in ("image", "photo"):
                res = await describe_image(
                    data=data,
                    mime=msg.get("media_mime") or "image/jpeg",
                    caption="",
                    business_name=t_settings.business_name,
                    settings=t_settings,
                    retry=False,
                )
                text = f"[Фото] Описание: {res.text.strip()}"
            else:
                return JSONResponse(status_code=400, content={"error": "Неподдерживаемый тип медиа для расшифровки"})

            from storage.db import execute
            execute(
                """
                UPDATE messages
                SET text = ?, media_status = 'ok', media_duration_s = ?, media_model = ?, media_cost = ?
                WHERE id = ?
                """,
                (text, res.duration_s, res.model, res.cost, mid),
            )
            return {"ok": True, "text": text}
        except Exception as exc:
            logger.exception("Ошибка повторной расшифровки медиа (%d)", mid)
            return JSONResponse(status_code=502, content={"error": f"Ошибка расшифровки: {exc}"})

    @app.post("/admin/clients/{pid}/conversations/{cid}/messages")
    async def send_message_in_conversation(pid: str, cid: str, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        conv = get_conversation(cid)
        if not _check_conv_ownership(conv, _conversation_client_key(clients_dir, pid)):
            return JSONResponse(status_code=404, content={"error": "диалог не найден"})

        provider = _client_provider(clients_dir, pid)
        if provider in ("wa", "zernio"):
            last_client = get_last_client_message_at(cid)
            if last_client:
                from datetime import datetime, timedelta, timezone
                last_dt = datetime.fromisoformat(last_client.replace("Z", "+00:00"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) - last_dt > timedelta(hours=24):
                    return JSONResponse(
                        status_code=409,
                        content={"error": "window_closed", "message": "24-часовое окно закрыто, используйте шаблон"}
                    )

        try:
            incoming = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON"})
        if not isinstance(incoming, dict):
            return JSONResponse(status_code=400, content={"error": "ожидается JSON-объект"})
        text = str(incoming.get("text") or "").strip()
        if not text:
            return JSONResponse(status_code=400, content={"error": "текст сообщения обязателен"})
        idempotency_key = str(incoming.get("idempotency_key") or "").strip()

        sender = await _conversation_client(state, settings, clients_dir, pid)
        if sender is None:
            return JSONResponse(status_code=409, content={"error": "нет транспорта для отправки"})

        conversation_id = conv.get("zernio_conversation_id", "")
        if provider == "zernio" and not conversation_id:
            return JSONResponse(status_code=409, content={"error": "нет conversation_id для Zernio"})

        if idempotency_key:
            existing = get_message_by_provider_id(idempotency_key)
            if existing and existing.get("conversation_id") == cid:
                return {"ok": True, "delivered": True, "deduplicated": True, "message_id": existing.get("id")}

        try:
            if provider == "zernio":
                await sender.send_text(conversation_id, text, conversation_id=conversation_id)
            else:
                await sender.send_text(conv["contact_phone"], text)
        except MessagingError as exc:
            return JSONResponse(status_code=502, content={"error": str(exc)})

        try:
            add_message(
                conversation_id=cid,
                role="human",
                text=text,
                content_kind="text",
                provider_message_id=idempotency_key,
                delivery_status="sent",
            )
            update_last_message_times(cid, is_client=False, is_human=True)
            open_handoff = get_open_handoff(cid)
            if open_handoff:
                mark_first_human_reply(open_handoff["id"])
        except Exception:
            logger.exception("Сообщение доставлено клиенту (%s), но произошла ошибка сохранения в БД", cid)

        return {"ok": True, "delivered": True}

    @app.post("/admin/clients/{pid}/conversations/{cid}/mode")
    async def set_conversation_mode(pid: str, cid: str, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        conv = get_conversation(cid)
        if not _check_conv_ownership(conv, _conversation_client_key(clients_dir, pid)):
            return JSONResponse(status_code=404, content={"error": "диалог не найден"})

        try:
            incoming = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON"})
        if not isinstance(incoming, dict):
            return JSONResponse(status_code=400, content={"error": "ожидается JSON-объект"})
        mode = str(incoming.get("mode") or "").strip()
        if mode not in ("bot", "manual"):
            return JSONResponse(status_code=400, content={"error": "mode должен быть 'bot' или 'manual'"})

        update_conversation_status(cid, mode)
        if mode == "bot":
            mark_read(cid)
            resolve_conversation_handoffs(cid)

        return {"ok": True, "conversation_id": cid, "mode": mode}

    @app.post("/admin/clients/{pid}/conversations/{cid}/read")
    async def mark_conversation_read(pid: str, cid: str, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        conv = get_conversation(cid)
        if not _check_conv_ownership(conv, _conversation_client_key(clients_dir, pid)):
            return JSONResponse(status_code=404, content={"error": "диалог не найден"})
        mark_read(cid)
        return {"ok": True}

    @app.get("/admin/conversations/{cid}/client")
    async def conversation_client(request: Request, cid: str, pid: str | None = None):
        conv = get_conversation(cid)
        if not conv:
            return JSONResponse(status_code=404, content={"error": "диалог не найден"})
        db_key = str(conv.get("client_key") or "")

        target_pid = pid
        if not target_pid:
            for stem in _client_pids(clients_dir):
                if _conversation_client_key(clients_dir, stem) == db_key:
                    target_pid = stem
                    break

        role, error = _authorize(settings, request, clients_dir, target_pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})

        if not target_pid:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})

        if role != "admin":
            expected_key = _conversation_client_key(clients_dir, target_pid)
            if db_key != expected_key:
                return JSONResponse(status_code=404, content={"error": "диалог не найден"})

        return {"pid": target_pid, "conversation_id": cid}
