"""Плейграунд для тестирования системного промпта и ответов LLM."""

import json
import logging
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from admin.routers.common import (
    _authorize,
    _client_yaml_path,
    _read_cfg,
)
from config.settings import LLMParams, Settings
from services.llm_client import LLMClient

logger = logging.getLogger(__name__)


def register_playground_routes(app, settings: Settings, state, clients_dir: Path) -> None:
    @app.post("/admin/clients/{pid}/playground")
    async def test_llm_playground(pid: str, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})

        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON"})
        if not isinstance(body, dict):
            return JSONResponse(status_code=400, content={"error": "ожидается JSON-объект"})

        user_message = str(body.get("message") or "").strip()
        if not user_message:
            return JSONResponse(status_code=400, content={"error": "сообщение обязательно"})

        custom_prompt = body.get("system_prompt")
        cfg = _read_cfg(_client_yaml_path(clients_dir, pid)) or {}
        llm_cfg = cfg.get("llm") or {}

        from handlers.message_handler import SYSTEM_PROMPT_TEMPLATE
        system_prompt = custom_prompt if custom_prompt is not None else SYSTEM_PROMPT_TEMPLATE.format(
            business_name=cfg.get("business_name") or "Бизнес",
            tone=cfg.get("tone") or "вежливый",
            language=cfg.get("language") or "ru",
            knowledge_base=cfg.get("knowledge_base") or "",
            style_examples=cfg.get("style_examples") or "",
        )

        llm = LLMClient(
            settings.llm_api_url,
            settings.llm_api_key,
            LLMParams(
                model=str(llm_cfg.get("model") or settings.llm.model),
                temperature=float(llm_cfg.get("temperature", 0.6)),
                max_tokens=int(llm_cfg.get("max_tokens", 500)),
                timeout_seconds=int(llm_cfg.get("timeout_seconds", 20)),
                reasoning_effort=llm_cfg.get("reasoning_effort"),
            ),
        )

        try:
            response = await llm.chat(system_prompt, user_message, history=[])
            return {"ok": True, "response": response}
        except Exception as exc:
            logger.exception("Ошибка в playground")
            return JSONResponse(status_code=502, content={"error": f"ошибка LLM: {exc}"})
        finally:
            await llm.close()
