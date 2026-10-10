"""Эндпоинты авторизации и идентификации (whoami)."""

import os
from pathlib import Path
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse

from admin.routers.common import (
    HEADER_ADMIN,
    HEADER_CLIENT,
    PAGE_PATH,
    _management_token_of,
    tokens_equal,
    verify_token_hash,
)
from config.settings import Settings

router = APIRouter()


def register_auth_routes(app, settings: Settings, clients_dir: Path) -> None:
    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.exists():
        from fastapi.staticfiles import StaticFiles
        app.mount("/admin/static", StaticFiles(directory=str(static_dir)), name="admin_static")

    @app.get("/admin")
    async def admin_page():
        """Одна страница панели: админ видит всех, клиент — только себя."""
        try:
            html = PAGE_PATH.read_text(encoding="utf-8")
        except OSError:
            return PlainTextResponse("панель недоступна", status_code=503)
        return HTMLResponse(html)

    @app.get("/admin/whoami")
    async def admin_whoami(request: Request):
        """Роль токена: admin, либо pid клиента, чьим management_token он является."""
        if tokens_equal(request.headers.get(HEADER_ADMIN, ""), settings.admin_token):
            return {"role": "admin"}
        client_header = request.headers.get(HEADER_CLIENT, "")
        if client_header:
            try:
                names = sorted(os.listdir(clients_dir))
            except OSError:
                names = []
            for name in names:
                if not name.lower().endswith((".yaml", ".yml")) or name.startswith("_"):
                    continue
                token_hash = _management_token_of(clients_dir, Path(name).stem)
                if token_hash and verify_token_hash(client_header, token_hash):
                    return {"role": "client", "phone_number_id": Path(name).stem}
        return JSONResponse(status_code=401, content={"error": "токен не распознан"})
