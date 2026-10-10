"""Эндпоинты аналитики, экспорта CSV, Telegram-привязок и вопросов без ответа."""

import json
import logging
import os
import re
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, PlainTextResponse

from admin.routers.common import (
    _authorize,
    _client_editable_cfg,
    _client_pids,
    _client_yaml_path,
    _conversation_client_key,
    _read_cfg,
)
from config.settings import LLMParams, Settings
from services.llm_client import LLMClient
from storage import (
    MAX_BINDINGS_PER_CLIENT,
    count_bindings,
    create_link_code,
    create_unanswered_group,
    export_stats_csv,
    get_admin_overview_stats,
    get_client_stats,
    get_last_regroup_time,
    get_questions_for_group,
    get_recent_unanswered_for_regroup,
    get_tg_bindings,
    get_unanswered_group,
    get_unanswered_question,
    list_unanswered_groups,
    list_ungrouped_questions,
    mark_question_answered,
    mark_question_ignored,
    parse_date_range,
    remove_tg_binding,
    update_group_status,
)
from storage.db import execute

logger = logging.getLogger(__name__)


def _kb_has_question(knowledge_base: str, question: str) -> bool:
    needle = " ".join(str(question or "").strip().lower().split())
    if not needle:
        return False
    for line in str(knowledge_base or "").splitlines():
        if line.strip().lower().startswith("вопрос:"):
            existing = " ".join(line.strip()[len("вопрос:"):].strip().lower().split())
            if existing == needle:
                return True
    return False


def _append_answers_to_kb(knowledge_base: str, questions: list[dict], answer: str) -> str:
    text = str(knowledge_base or "").strip()
    blocks = []
    for question in questions:
        raw = str((question or {}).get("question") or "").strip()
        if not raw or _kb_has_question(text, raw):
            continue
        blocks.append("Вопрос: " + raw + "\nОтвет: " + answer)
    if not blocks:
        return text
    addition = "\n\n---\n\n".join(blocks)
    return (text + "\n\n---\n\n" + addition).strip() if text else addition


def _parse_groups_json(raw: str) -> list | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\[.*\]", text, re.S)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if isinstance(parsed, dict):
        parsed = parsed.get("groups") or []
    if not isinstance(parsed, list):
        return None
    return parsed


def register_analytics_routes(app, settings: Settings, state, clients_dir: Path) -> None:
    # --- Telegram-привязка для уведомлений ---

    @app.post("/admin/clients/{pid}/telegram/link-code")
    async def tg_link_code(pid: str, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})

        db_key = _conversation_client_key(clients_dir, pid)
        bot_username = os.getenv("TELEGRAM_OWNER_BOT_USERNAME", "").strip().lstrip("@")
        link = create_link_code(db_key, bot_username=bot_username)
        return {
            "ok": True,
            "code": link["code"],
            "url": link["url"],
            "expires_at": link["expires_at"],
            "bot_username": bot_username,
            "bound": count_bindings(db_key),
            "max_bindings": MAX_BINDINGS_PER_CLIENT,
        }

    @app.get("/admin/clients/{pid}/telegram")
    async def tg_bindings_list(pid: str, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        db_key = _conversation_client_key(clients_dir, pid)
        return {
            "bindings": get_tg_bindings(db_key),
            "max_bindings": MAX_BINDINGS_PER_CLIENT,
            "owner_bot_configured": bool(settings.telegram_owner_bot_token),
        }

    @app.delete("/admin/clients/{pid}/telegram/{binding_id}")
    async def tg_binding_remove(pid: str, binding_id: int, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if not remove_tg_binding(_conversation_client_key(clients_dir, pid), binding_id):
            return JSONResponse(status_code=404, content={"error": "привязка не найдена"})
        return {"ok": True}

    # --- Вопросы без ответа ---

    @app.get("/admin/clients/{pid}/unanswered/{group_id}/questions")
    async def unanswered_group_questions(pid: str, group_id: int, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        db_key = _conversation_client_key(clients_dir, pid)
        group = get_unanswered_group(group_id)
        if not group or group.get("client_key") != db_key:
            return JSONResponse(status_code=404, content={"error": "группа не найдена"})
        questions = get_questions_for_group(group_id)
        return {
            "questions": [{
                "id": q["id"],
                "question": q["question"],
                "status": q["status"],
                "answer_text": q.get("answer_text") or "",
                "created_at": q.get("created_at") or "",
            } for q in questions]
        }

    @app.get("/admin/clients/{pid}/unanswered")
    async def get_unanswered(pid: str, request: Request, status: str | None = None):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        db_key = _conversation_client_key(clients_dir, pid)
        groups = list_unanswered_groups(db_key, status=status)
        return {
            "groups": groups,
            "ungrouped": list_ungrouped_questions(db_key, status=status or None),
        }

    @app.post("/admin/clients/{pid}/unanswered/question/{question_id}/answer")
    async def answer_unanswered_question(pid: str, question_id: int, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        try:
            incoming = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON"})
        if not isinstance(incoming, dict):
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON-объектом"})
        answer = str(incoming.get("answer") or "").strip()
        if not answer:
            return JSONResponse(status_code=400, content={"error": "ответ не может быть пустым"})
        if len(answer) > 20000:
            return JSONResponse(status_code=400, content={"error": "ответ слишком длинный (максимум 20 000 символов)"})

        db_key = _conversation_client_key(clients_dir, pid)
        question = get_unanswered_question(question_id)
        if not question or question.get("client_key") != db_key:
            return JSONResponse(status_code=404, content={"error": "вопрос не найден"})

        path = _client_yaml_path(clients_dir, pid)
        cfg = _read_cfg(path)
        if cfg is None:
            return JSONResponse(status_code=409, content={"error": "не удалось прочитать конфигурацию клиента"})
        old_kb = str(cfg.get("knowledge_base") or "").strip()
        new_kb = _append_answers_to_kb(old_kb, [question], answer)
        _client_editable_cfg(clients_dir, pid, cfg, {"knowledge_base": new_kb},
                             "admin" if role == "admin" else f"client:{pid}",
                             "unanswered_answer", state)
        mark_question_answered(question_id, answer)
        await state.refresh_tenants()
        return {"ok": True, "updated_kb": True}

    @app.post("/admin/clients/{pid}/unanswered/{group_id}/answer")
    async def answer_unanswered(pid: str, group_id: int, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        try:
            incoming = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON"})
        if not isinstance(incoming, dict):
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON-объектом"})
        answer = str(incoming.get("answer") or "").strip()
        if not answer:
            return JSONResponse(status_code=400, content={"error": "ответ не может быть пустым"})
        if len(answer) > 20000:
            return JSONResponse(status_code=400, content={"error": "ответ слишком длинный (максимум 20 000 символов)"})

        group = get_unanswered_group(group_id)
        if not group or group.get("client_key") != _conversation_client_key(clients_dir, pid):
            return JSONResponse(status_code=404, content={"error": "группа не найдена"})

        path = _client_yaml_path(clients_dir, pid)
        cfg = _read_cfg(path)
        if cfg is None:
            return JSONResponse(status_code=409, content={"error": "не удалось прочитать конфигурацию клиента"})
        old_kb = str(cfg.get("knowledge_base") or "").strip()
        questions = get_questions_for_group(group_id)
        new_kb = _append_answers_to_kb(old_kb, questions, answer)

        _client_editable_cfg(clients_dir, pid, cfg, {"knowledge_base": new_kb},
                             "admin" if role == "admin" else f"client:{pid}", "unanswered_answer", state)

        for q in questions:
            mark_question_answered(q["id"], answer)
        update_group_status(group_id, "answered")

        await state.refresh_tenants()
        return {"ok": True, "updated_kb": True}

    @app.post("/admin/clients/{pid}/unanswered/question/{question_id}/ignore")
    async def ignore_unanswered_question(pid: str, question_id: int, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        db_key = _conversation_client_key(clients_dir, pid)
        question = get_unanswered_question(question_id)
        if not question or question.get("client_key") != db_key:
            return JSONResponse(status_code=404, content={"error": "вопрос не найден"})
        mark_question_ignored(question_id)
        return {"ok": True}

    @app.post("/admin/clients/{pid}/unanswered/{group_id}/ignore")
    async def ignore_unanswered(pid: str, group_id: int, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        db_key = _conversation_client_key(clients_dir, pid)
        group = get_unanswered_group(group_id)
        if not group or group.get("client_key") != db_key:
            return JSONResponse(status_code=404, content={"error": "группа не найдена"})
        update_group_status(group_id, "ignored")
        execute("UPDATE unanswered_questions SET status = 'ignored' WHERE group_id = ? AND status = 'new'", (group_id,))
        return {"ok": True}

    @app.post("/admin/clients/{pid}/unanswered/regroup")
    async def regroup_unanswered(pid: str, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})

        db_key = _conversation_client_key(clients_dir, pid)

        if role != "admin":
            last_time = get_last_regroup_time(db_key)
            if last_time:
                from datetime import datetime, timezone
                try:
                    dt_last = datetime.fromisoformat(last_time.replace("Z", "+00:00"))
                    if dt_last.tzinfo is None:
                        dt_last = dt_last.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - dt_last).total_seconds()
                    if elapsed < 600:
                        remaining = int(600 - elapsed)
                        return JSONResponse(
                            status_code=429,
                            content={"error": f"Группировка уже выполнялась недавно. Повторите через {remaining} сек."},
                        )
                except Exception:
                    pass

        questions = get_recent_unanswered_for_regroup(db_key, limit=200)
        if len(questions) < 2:
            return {"ok": True, "groups_created": 0, "message": "недостаточно вопросов для группировки"}

        system_prompt = (
            "Ты — помощник для группировки похожих вопросов клиентов. "
            "Дан список вопросов. Сгруппируй их по смыслу: вопросы об одной и той же теме "
            "(например, цена, время работы, запись на маникюр) должны быть в одной группе. "
            "Верни JSON: список групп, где каждая группа — объект с полями "
            "'name' (краткое название темы) и 'question_ids' (массив id вопросов). "
            "Не придумывай вопросы, используй только те, что даны. Вопросы, которые не "
            "подходят ни к какой группе, не включай."
        )
        user_prompt = "Вопросы:\n" + "\n".join(f"{q['id']}: {q['question']}" for q in questions)

        client_settings = _read_cfg(_client_yaml_path(clients_dir, pid)) or {}
        llm_params = client_settings.get("llm") or {}
        llm = LLMClient(
            settings.llm_api_url,
            settings.llm_api_key,
            LLMParams(
                model=str(llm_params.get("model") or settings.llm.model),
                temperature=float(llm_params.get("temperature", 0.3)),
                max_tokens=int(llm_params.get("max_tokens", 2000)),
                timeout_seconds=int(llm_params.get("timeout_seconds", 30)),
                reasoning_effort=llm_params.get("reasoning_effort"),
            ),
        )

        try:
            response = await llm.chat(system_prompt, user_prompt, history=[])
        except Exception as exc:
            logger.exception("Группировка вопросов: LLM не ответил")
            return JSONResponse(status_code=502, content={"error": f"ошибка LLM: {exc}"})
        finally:
            await llm.close()

        groups = _parse_groups_json(response)
        if groups is None:
            return JSONResponse(
                status_code=502,
                content={"error": "LLM вернул нечитаемый ответ вместо списка групп"},
            )

        created = 0
        for g in groups:
            if not isinstance(g, dict):
                continue
            name = str(g.get("name") or "").strip()
            raw_ids = g.get("question_ids")
            if not isinstance(raw_ids, list):
                continue
            ids = [int(x) for x in raw_ids if str(x).isdigit()]
            if name and ids:
                create_unanswered_group(db_key, name, ids)
                created += 1

        return {"ok": True, "groups_created": created, "groups": groups}

    # --- Аналитика ---

    @app.get("/admin/clients/{pid}/stats")
    async def get_stats_for_client(pid: str, request: Request,
                                   from_date: str | None = None,
                                   to_date: str | None = None):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})

        from_date, to_date = parse_date_range(from_date, to_date)
        client_tz = str((_read_cfg(_client_yaml_path(clients_dir, pid)) or {}).get("timezone")
                        or settings.timezone or "Asia/Almaty")
        try:
            stats = get_client_stats(_conversation_client_key(clients_dir, pid),
                                     from_date, to_date, timezone=client_tz)
            return stats
        except Exception as exc:
            logger.exception("Ошибка получения статистики для клиента %s: %s", pid, exc)
            return JSONResponse(status_code=500, content={"error": f"Ошибка сбора аналитики: {exc}"})

    @app.get("/admin/clients/{pid}/stats/export.csv")
    async def export_client_stats(pid: str, request: Request,
                                  from_date: str | None = None,
                                  to_date: str | None = None):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        from_date, to_date = parse_date_range(from_date, to_date)
        csv_data = export_stats_csv(_conversation_client_key(clients_dir, pid), from_date, to_date)
        return PlainTextResponse(
            csv_data,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="stats-{pid}-{from_date[:10]}-{to_date[:10]}.csv"'}
        )

    @app.get("/admin/stats/overview")
    async def admin_stats_overview(request: Request,
                                   from_date: str | None = None,
                                   to_date: str | None = None):
        role, error = _authorize(settings, request, clients_dir, pid=None)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нужен админ-токен"})
        if role != "admin":
            return JSONResponse(status_code=403, content={"error": "только для администратора"})

        from_date, to_date = parse_date_range(from_date, to_date)
        client_map = {
            stem: _conversation_client_key(clients_dir, stem)
            for stem in _client_pids(clients_dir)
        }
        try:
            stats = get_admin_overview_stats(from_date, to_date, client_map)
            return stats
        except Exception as exc:
            logger.exception("Ошибка получения общей статистики: %s", exc)
            return JSONResponse(status_code=500, content={"error": f"Ошибка сбора общей аналитики: {exc}"})
