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

Профиль WhatsApp-номера (тексты рядом с именем и аватар) живёт в Meta, а не
в clients/*.yaml: /admin/clients/{pid}/profile читает и правит его напрямую
через Graph API. Правится тем же токеном, что и конфиг, — клиент может менять
профиль своего номера, админ — любого.
"""

import hmac
import json
import logging
import os
import shutil
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import File, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from config.clients import is_client_file, is_valid_phone_number_id, validate_tenant_config
from config.settings import Settings
from whatsapp.meta_client import ABOUT_MAX_LENGTH, MetaError, MetaTimeout, MetaWhatsAppClient

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

# --- профиль WhatsApp (живёт в Meta, а не в clients/*.yaml) ------------------

# Поля, которые панель показывает в ответе GET профиля. vertical Meta тоже
# отдаёт, но через API он не меняется и в панели не нужен; photo_url — ссылка
# на текущий аватар (пустая, если номера с фото нет).
PROFILE_RESPONSE_FIELDS = ("about", "description", "email", "websites", "address",
                           "photo_url")

# Поля, которые PATCH умеет применять (display name через API не меняется).
PROFILE_WRITABLE_FIELDS = ("about", "description", "email", "websites", "address")

# Лимит Meta на «Описание» (символы). Проверяется и на бэкенде, и в панели.
DESCRIPTION_MAX_LENGTH = 512

# Ссылок на сайте Meta принимает не больше двух.
PROFILE_WEBSITES_MAX = 2

# Аватар: jpg/png/webp до 5 МБ, файл нигде у нас не остаётся.
PROFILE_PHOTO_MAX_BYTES = 5 * 1024 * 1024
PROFILE_PHOTO_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}

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


# --- профиль WhatsApp ---------------------------------------------------------


def _looks_like_email(value: str) -> bool:
    """Грубая проверка адреса: непустые части вокруг «@» и точка в домене."""
    local, _, domain = value.partition("@")
    return bool(local and domain and "." in domain and " " not in value)


def _validate_profile_fields(incoming: dict) -> tuple[dict, list[str]]:
    """Тело PATCH -> (поля для Meta, список проблем). Есть проблема — Meta не зовём.

    Пустое значение поля допустимо: так клиент очищает «о компании» или email,
    пустой список websites — убирает сайт. Смысл проверок — не дать Meta
    обрезать значение молча и показать в панели внятный русский текст.
    """
    payload: dict = {}
    problems: list[str] = []

    for key in ("about", "description", "address"):
        if key not in incoming:
            continue
        text = str(incoming[key] or "").strip()
        if key == "about" and len(text) > ABOUT_MAX_LENGTH:
            problems.append(
                f"«О компании» — максимум {ABOUT_MAX_LENGTH} символов, "
                f"сейчас {len(text)}"
            )
            continue
        if key == "description" and len(text) > DESCRIPTION_MAX_LENGTH:
            problems.append(
                f"Описание — максимум {DESCRIPTION_MAX_LENGTH} символов, "
                f"сейчас {len(text)}"
            )
            continue
        payload[key] = text

    if "email" in incoming:
        email = str(incoming["email"] or "").strip()
        if email and not _looks_like_email(email):
            problems.append("email выглядит неполным — проверьте адрес")
        else:
            payload["email"] = email

    if "websites" in incoming:
        raw = incoming["websites"]
        raw = [] if raw is None else raw
        if not isinstance(raw, (list, tuple)):
            problems.append("сайт — список ссылок (пустой список = убрать сайт)")
        else:
            sites = [str(site).strip() for site in raw if str(site or "").strip()]
            if len(sites) > PROFILE_WEBSITES_MAX:
                problems.append(
                    f"сайтов может быть не больше {PROFILE_WEBSITES_MAX} — "
                    "Meta принимает 1–2"
                )
            elif any(not site.startswith("https://") for site in sites):
                problems.append(
                    "ссылка на сайт должна начинаться с https:// — "
                    "например https://example.com"
                )
            else:
                payload["websites"] = sites

    return payload, problems


@asynccontextmanager
async def _profile_meta_client(state, settings: Settings, clients_dir: Path, pid: str):
    """Meta-клиент для правки профиля: бандл клиента или одноразовый.

    Обычный путь — sender из бандла: там уже его токен и открытые соединения.
    Бандла нет, когда конфиг клиента не прошёл валидацию, — тогда собираем
    одноразовый клиент и закрываем его на выходе. None — работать нечем:
    у номера нет токена.
    """
    await state.refresh_tenants()
    bundle = state.tenants.get(pid)
    if bundle is not None and isinstance(bundle.sender, MetaWhatsAppClient):
        yield bundle.sender
        return
    path = _client_yaml_path(clients_dir, pid)
    cfg = _read_cfg(path) if path else None
    token = str((cfg or {}).get("access_token") or "").strip() or settings.whatsapp_access_token
    if not token:
        yield None
        return
    client = _make_meta_client(state, settings, token, pid)
    try:
        yield client
    finally:
        await client.close()


def _make_meta_client(state, settings: Settings, access_token: str, pid: str) -> MetaWhatsAppClient:
    """Одноразовый Meta-клиент номера — через фабрику приложения, если она есть.

    Фабрика (state.sender_factory) уважает подмену транспорта в тестах; без
    неё (вне мультитенанта, где этот путь не встречается) — прямой клиент.
    """
    factory = getattr(state, "sender_factory", None)
    if factory is None:
        return MetaWhatsAppClient(access_token, pid, graph_version=settings.meta_graph_version)
    return factory(replace(
        settings,
        whatsapp_access_token=access_token,
        whatsapp_phone_number_id=pid,
    ))


def _meta_failure(exc: MetaError | MetaTimeout) -> JSONResponse:
    """Ошибка Meta наружу: 504 на таймаут, 502 на отказ API — с текстом Meta.

    Текст показываем в панели как есть: в нём Meta пишет, чего именно не
    хватило (чаще всего — прав токена на whatsapp_business_management).
    """
    if isinstance(exc, MetaTimeout):
        return JSONResponse(status_code=504, content={"error": str(exc)})
    return JSONResponse(status_code=502, content={"error": str(exc)})


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

    @app.get("/admin/clients/{pid}/profile")
    async def get_client_profile(pid: str, request: Request):
        """Профиль WhatsApp-номера из Meta — что сейчас видно рядом с именем.

        Кэша нет: источник истины — Meta, и панель зовёт его на каждый заход
        в редактор. Отказ Meta уходит клиенту как 502 с её текстом.
        """
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})
        async with _profile_meta_client(state, settings, clients_dir, pid) as meta:
            if meta is None:
                return JSONResponse(
                    status_code=409,
                    content={"error": "у клиента нет токена для работы с профилем"},
                )
            try:
                profile = await meta.get_business_profile()
            except (MetaError, MetaTimeout) as exc:
                return _meta_failure(exc)
        return {
            key: profile.get(key, [] if key == "websites" else "")
            for key in PROFILE_RESPONSE_FIELDS
        }

    @app.patch("/admin/clients/{pid}/profile")
    async def patch_client_profile(pid: str, request: Request):
        """Текстовые поля профиля: валидация -> PATCH в Meta -> строка аудита.

        Конфиг клиента при этом не меняется — в clients/*.yaml профиля нет,
        профиль живёт в Meta, и source of truth там.
        """
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})
        raw_body = await request.body()
        if len(raw_body) > MAX_BODY_BYTES:
            return JSONResponse(status_code=413, content={"error": "тело запроса слишком большое"})
        try:
            incoming = json.loads(raw_body)
        except ValueError:
            return JSONResponse(
                status_code=400,
                content={"error": "тело запроса должно быть JSON-объектом с полями профиля"},
            )
        if not isinstance(incoming, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "ожидается JSON-объект с полями профиля"},
            )
        unknown = sorted(key for key in incoming if key not in PROFILE_WRITABLE_FIELDS)
        if unknown:
            return JSONResponse(
                status_code=400,
                content={"error": f"поля не меняются через API: {', '.join(unknown)}"},
            )
        payload, problems = _validate_profile_fields(incoming)
        if problems:
            return JSONResponse(
                status_code=400, content={"error": "проверьте поля", "problems": problems},
            )
        if not payload:
            return JSONResponse(
                status_code=400, content={"error": "нечего менять — пришлите хотя бы одно поле"},
            )
        async with _profile_meta_client(state, settings, clients_dir, pid) as meta:
            if meta is None:
                return JSONResponse(
                    status_code=409,
                    content={"error": "у клиента нет токена для работы с профилем"},
                )
            try:
                await meta.update_business_profile(payload)
            except (MetaError, MetaTimeout) as exc:
                return _meta_failure(exc)
        actor = "admin" if role == "admin" else f"client:{pid}"
        _audit(clients_dir, actor, "profile", pid, {}, payload)
        logger.info("Админ-API: профиль %s обновлён (%s): %s", pid, role, ", ".join(sorted(payload)))
        return {"ok": True, "changed": sorted(payload)}

    @app.post("/admin/clients/{pid}/profile/photo")
    async def upload_client_profile_photo(pid: str, request: Request,
                                          file: UploadFile = File(...)):
        """Аватар номера: файл уходит в Meta и здесь нигде не остаётся.

        Расширение для Meta берём из content-type, а не из имени файла: в
        имени может быть что угодно, а Graph API ждёт имя с .jpg/.png/.webp.
        """
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})
        content_type = (file.content_type or "").lower()
        suffix = PROFILE_PHOTO_TYPES.get(content_type)
        if suffix is None:
            return JSONResponse(
                status_code=400,
                content={"error": "аватар — файл jpg, png или webp"},
            )
        content = await file.read(PROFILE_PHOTO_MAX_BYTES + 1)
        if len(content) > PROFILE_PHOTO_MAX_BYTES:
            return JSONResponse(
                status_code=413, content={"error": "файл больше 5 МБ — уменьшите размер"},
            )
        if not content:
            return JSONResponse(status_code=400, content={"error": "файл пустой"})
        async with _profile_meta_client(state, settings, clients_dir, pid) as meta:
            if meta is None:
                return JSONResponse(
                    status_code=409,
                    content={"error": "у клиента нет токена для работы с профилем"},
                )
            try:
                await meta.upload_profile_photo(
                    f"whatsapp-profile{suffix}", content, content_type,
                )
            except (MetaError, MetaTimeout) as exc:
                return _meta_failure(exc)
        actor = "admin" if role == "admin" else f"client:{pid}"
        _audit(clients_dir, actor, "profile_photo", pid, {}, {"photo": "avatar"})
        logger.info("Админ-API: аватар %s обновлён (%s)", pid, role)
        return {"ok": True}
