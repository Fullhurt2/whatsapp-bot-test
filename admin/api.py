"""Админ-API: правка клиентских конфигов clients/*.yaml без деплоя и SSH.

Роли (без БД и сессий — токен в заголовке каждого запроса):
  - X-Admin-Token (env ADMIN_TOKEN) — полный доступ: список, чтение, запись,
    удаление любых клиентов;
  - X-Client-Token — персональный токен клиента (поле management_token в его
    yaml): доступ только к своему конфигу и только к бизнес-полям.

Правила записи:
  - перед записью конфиг прогоняется через тот же валидатор, что ест реестр
    (config.clients.validate_tenant_config) — битый файл на диск не попадёт;
  - запись атомарная (tmp + rename); предыдущая версия уходит в
    clients/.history/<pid>/<UTC-время>.yaml (откат возможен всегда);
  - каждое изменение — строка в clients/.audit.jsonl: кто, когда и какие
    поля поменял (значения токенов в лог никогда не попадают);
  - после записи реестр перечитывается штатным hot-reload.

Секреты (access_token, management_token) в GET-ответах маскируются
("EAAY…ab12"); пустое или замаскированное значение в PUT означает
«оставить прежнее» — токен невозможно затереть случайно.
"""

import hmac
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from config.clients import is_client_file, is_valid_phone_number_id, validate_tenant_config
from config.settings import Settings

logger = logging.getLogger(__name__)

HEADER_ADMIN = "X-Admin-Token"
HEADER_CLIENT = "X-Client-Token"

# Минимальная длина админ-токена для продакшена (иначе warning при старте).
ADMIN_TOKEN_MIN_LENGTH = 32

# Секретные поля: маскируются в GET, пустое/замаскированное значение в PUT
# означает «оставить прежнее».
SECRET_FIELDS = ("access_token", "management_token")

# Поля, которые клиент с management_token менять не может (только админ):
# его собственный WABA-токен, токен панели и параметры LLM (лимит расходов).
RESTRICTED_FIELDS = ("access_token", "management_token", "llm")

# Поля, доступные клиенту для правки у себя (бизнес-конфиг).
CLIENT_EDITABLE_FIELDS = (
    "business_name",
    "tone",
    "language",
    "knowledge_base",
    "owner_whatsapp_phone",
    "fallback_triggers",
    "style_examples",
    "fallback_reply_ru",
    "fallback_reply_kk",
    "timeout_reply_ru",
    "timeout_reply_kk",
)

HISTORY_DIR = ".history"
AUDIT_FILE = ".audit.jsonl"

MAX_BODY_BYTES = 256 * 1024  # база знаний бывает большой, но не безграничной

_PAGE_PATH = Path(__file__).resolve().parent / "static" / "index.html"

# --- вспомогательные ----------------------------------------------------------


def tokens_equal(provided: str, expected: str) -> bool:
    """Constant-time сравнение токенов; пустые строки не проходят никогда."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(
        provided.strip().encode("utf-8"), expected.strip().encode("utf-8")
    )


def mask_secret(value: str) -> str:
    """Маска секрета для ответов API: "EAAY…ab12" (значение не раскрывается)."""
    value = str(value or "").strip()
    if not value:
        return ""
    if len(value) <= 12:
        return "••••"
    return f"{value[:4]}…{value[-4:]}"


def _client_yaml_path(clients_dir: Path, pid: str) -> Path | None:
    """Текущий файл клиента (<pid>.yaml; .yml тоже находим)."""
    for suffix in (".yaml", ".yml"):
        candidate = clients_dir / f"{pid}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def _read_cfg(path: Path) -> dict | None:
    """Содержимое yaml-файла как словарь; None — битый/нечитаемый."""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except (OSError, yaml.YAMLError):
        return None
    return cfg if isinstance(cfg, dict) else None


def _management_token_of(clients_dir: Path, pid: str) -> str | None:
    """management_token из текущего yaml клиента (None — не задан/файла нет).

    Читаем файл напрямую, а не из реестра: так токен актуален сразу после
    правки конфига, без ожидания перечитки.
    """
    path = _client_yaml_path(clients_dir, pid)
    if path is None:
        return None
    cfg = _read_cfg(path)
    value = str((cfg or {}).get("management_token") or "").strip()
    return value or None


def _authorize(settings: Settings, request: Request, clients_dir: Path,
               pid: str | None) -> tuple[str | None, int | None]:
    """(роль, статус ошибки): 'admin' | 'client' | (None, 401|403).

    401 — заголовков с токеном нет вовсе; 403 — токен был, но не подошёл
    (не раскрываем, какая именно из проверок провалилась).
    """
    admin_header = request.headers.get(HEADER_ADMIN, "")
    client_header = request.headers.get(HEADER_CLIENT, "")
    if settings.admin_token and tokens_equal(admin_header, settings.admin_token):
        return "admin", None
    if pid and client_header:
        expected = _management_token_of(clients_dir, pid)
        if expected and tokens_equal(client_header, expected):
            return "client", None
    if admin_header or client_header:
        return None, 403
    return None, 401


def _atomic_write(path: Path, cfg: dict) -> None:
    """Запись конфига через временный файл — обрыв не оставит битый yaml."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    os.replace(tmp, path)


def _backup(path: Path, clients_dir: Path, pid: str) -> Path | None:
    """Сохраняет предыдущую версию файла в clients/.history/<pid>/."""
    if not path.exists():
        return None
    dest_dir = clients_dir / HISTORY_DIR / pid
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    dest = dest_dir / f"{stamp}.yaml"
    try:
        shutil.copy2(path, dest)
        return dest
    except OSError:
        logger.exception("Не удалось сохранить бэкап %s", path)
        return None


def _audit(clients_dir: Path, actor: str, action: str, pid: str,
           old_cfg: dict | None, new_cfg: dict | None) -> None:
    """Строка в .audit.jsonl: кто/когда/какие поля. Значения не пишем —
    только имена полей, чтобы токены и бизнес-данные не оседали в логах."""
    old = old_cfg or {}
    fresh = new_cfg or {}
    changed = sorted(
        key for key in set(old) | set(fresh)
        if old.get(key) != fresh.get(key)
    )
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actor": actor,
        "action": action,
        "phone_number_id": pid,
        "changed": changed,
    }
    try:
        with open(clients_dir / AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        # Аудит не должен ломать саму операцию — но факт сбоя логируем.
        logger.exception("Не удалось дописать аудит-лог")


def _merge_incoming(old_cfg: dict, incoming: dict) -> dict:
    """Старый конфиг + присланное тело PUT -> итоговый dict.

    Секретные поля с пустым/замаскированным значением сохраняют прежнее
    содержимое — токен нельзя затереть по неосторожности, вернув из формы
    замаскированную маску.
    """
    merged = dict(old_cfg)
    for key, value in incoming.items():
        if key in SECRET_FIELDS:
            provided = str(value if value is not None else "").strip()
            old_value = str(old_cfg.get(key) or "")
            if provided and provided != mask_secret(old_value):
                merged[key] = provided
            # пустое/замаскированное значение = оставить прежнее
        else:
            merged[key] = value
    return merged


def _incoming_differs(key: str, old_value, new_value) -> bool:
    """Прислано ли для закрытого поля по-настоящему новое значение.

    Равное текущему или его маске (форма возвращает маску как есть) и пустое
    значение изменением не считаются; всё остальное — попытка изменения.
    """
    if key in SECRET_FIELDS:
        provided = str(new_value if new_value is not None else "").strip()
        old_str = str(old_value or "")
        return provided not in ("", mask_secret(old_str), old_str)
    # llm
    return new_value not in (None, {}) and new_value != old_value


# --- роуты --------------------------------------------------------------------


def register_admin_api(app, settings: Settings, state) -> None:
    """Регистрирует админ-роуты и страницу /admin.

    Отключено (роуты не существуют -> 404), если нет ADMIN_TOKEN или сервис
    работает вне мультитенанта — админ-поверхность просто не появляется.
    """
    if not state.multitenant:
        if settings.admin_token:
            logger.warning(
                "ADMIN_TOKEN задан, но админ-API работает только в мультитенанте "
                "(clients/) — роуты отключены"
            )
        return
    if not settings.admin_token:
        logger.warning("ADMIN_TOKEN не задан — админ-API и панель /admin отключены")
        return
    if len(settings.admin_token) < ADMIN_TOKEN_MIN_LENGTH:
        logger.warning(
            "ADMIN_TOKEN короче %d символов — для продакшена сгенерируйте длиннее",
            ADMIN_TOKEN_MIN_LENGTH,
        )

    clients_dir = Path(settings.clients_dir)

    @app.get("/admin")
    async def admin_page():
        """Одна страница панели: админ видит всех, клиент — только себя."""
        try:
            html = _PAGE_PATH.read_text(encoding="utf-8")
        except OSError:
            logger.exception("Не найден admin/static/index.html")
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
                token = _management_token_of(clients_dir, Path(name).stem)
                if token and tokens_equal(client_header, token):
                    return {"role": "client", "phone_number_id": Path(name).stem}
        return JSONResponse(status_code=401, content={"error": "токен не распознан"})

    @app.get("/admin/clients")
    async def admin_list(request: Request):
        """Список клиентов: валидные + пропущенные файлы с причинами (только админ)."""
        role, error = _authorize(settings, request, clients_dir, pid=None)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нужен админ-токен"})
        if role != "admin":
            return JSONResponse(status_code=403, content={"error": "только для администратора"})

        clients, skipped = [], []
        try:
            names = sorted(os.listdir(clients_dir))
        except OSError:
            names = []
        for name in names:
            if not name.lower().endswith((".yaml", ".yml")) or name.startswith("_"):
                continue
            pid = Path(name).stem
            if not is_valid_phone_number_id(pid):
                skipped.append({"file": name, "problems": [
                    "имя файла должно быть phone_number_id (цифры)",
                ]})
                continue
            cfg = _read_cfg(clients_dir / name)
            if cfg is None:
                skipped.append({"file": name, "problems": ["yaml не читается или не является словарём"]})
                continue
            tenant, problems, _ = validate_tenant_config(cfg, settings, pid, name)
            if tenant is None:
                skipped.append({"file": name, "problems": problems})
                continue
            clients.append({
                "phone_number_id": pid,
                "business_name": str(cfg.get("business_name") or ""),
                "config_file": name,
                "has_own_token": bool(str(cfg.get("access_token") or "").strip()),
                "has_management_token": bool(str(cfg.get("management_token") or "").strip()),
                "owner_phone": tenant.owner_phone or "",
            })
        return {"clients": clients, "skipped": skipped}

    @app.get("/admin/clients/{pid}")
    async def get_client(pid: str, request: Request):
        """Конфиг клиента с замаскированными секретами (админ или сам клиент)."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        path = _client_yaml_path(clients_dir, pid)
        if path is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})
        cfg = _read_cfg(path)
        if cfg is None:
            return JSONResponse(
                status_code=409,
                content={"error": "yaml не читается — перепишите конфиг целиком через PUT"},
            )
        masked = {
            key: (mask_secret(value) if key in SECRET_FIELDS and isinstance(value, str) else value)
            for key, value in cfg.items()
        }
        return {"phone_number_id": pid, "config_file": path.name, **masked}

    @app.put("/admin/clients/{pid}")
    async def put_client(pid: str, request: Request):
        """Создание/обновление конфига: валидация -> бэкап -> атомарная запись.

        Админ может всё; клиент — только бизнес-поля своего конфига. Пустое
        или замаскированное значение секрета сохраняет прежний токен.
        """
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if not is_valid_phone_number_id(pid):
            return JSONResponse(
                status_code=400,
                content={"error": "phone_number_id должен состоять из цифр"},
            )
        raw_body = await request.body()
        if len(raw_body) > MAX_BODY_BYTES:
            return JSONResponse(status_code=413, content={"error": "тело запроса слишком большое"})
        try:
            incoming = json.loads(raw_body)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": "тело запроса должно быть JSON-объектом с полями конфига"},
            )
        if not isinstance(incoming, dict):
            return JSONResponse(status_code=400, content={"error": "ожидается JSON-объект с полями конфига"})
        incoming_pid = str(incoming.get("phone_number_id") or "").strip()
        if incoming_pid and incoming_pid != pid:
            return JSONResponse(
                status_code=400,
                content={"error": "phone_number_id в теле не совпадает с адресом запроса"},
            )

        path = _client_yaml_path(clients_dir, pid)
        old_cfg = _read_cfg(path) if path else None
        if role == "client":
            if path is None:
                return JSONResponse(status_code=404, content={"error": "клиент не найден"})
            denied = [
                key for key in RESTRICTED_FIELDS
                if key in incoming and _incoming_differs(key, (old_cfg or {}).get(key), incoming[key])
            ]
            if denied:
                return JSONResponse(
                    status_code=403,
                    content={"error": f"изменение полей {', '.join(denied)} доступно только администратору"},
                )
            incoming = {key: value for key, value in incoming.items() if key in CLIENT_EDITABLE_FIELDS}

        merged = _merge_incoming(old_cfg or {}, incoming)
        tenant, problems, warnings = validate_tenant_config(merged, settings, pid, f"{pid}.yaml")
        if tenant is None:
            # Битый конфиг на диск не пишется — текущий файл остаётся рабочим.
            return JSONResponse(status_code=400, content={"ok": False, "problems": problems})

        if path is not None:
            _backup(path, clients_dir, pid)
        target = path or clients_dir / f"{pid}.yaml"
        _atomic_write(target, merged)
        actor = "admin" if role == "admin" else f"client:{pid}"
        _audit(clients_dir, actor, "put", pid, old_cfg, merged)
        await state.refresh_tenants()
        logger.info("Админ-API: конфиг %s записан (%s)", pid, role)
        return {"ok": True, "phone_number_id": pid, "warnings": warnings}

    @app.delete("/admin/clients/{pid}")
    async def delete_client(pid: str, request: Request):
        """Отключение клиента: бэкап + удаление yaml (только админ)."""
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if role != "admin":
            return JSONResponse(status_code=403, content={"error": "удаление доступно только администратору"})
        path = _client_yaml_path(clients_dir, pid)
        if path is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})
        old_cfg = _read_cfg(path)
        _backup(path, clients_dir, pid)
        path.unlink()
        _audit(clients_dir, "admin", "delete", pid, old_cfg, None)
        await state.refresh_tenants()
        return {"ok": True}
