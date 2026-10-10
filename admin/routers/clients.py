"""Эндпоинты управления клиентами (CRUD, профиль, Zernio connect)."""

import json
import logging
import os
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import File, Request, UploadFile
from fastapi.responses import JSONResponse

from admin.routers.common import (
    ABOUT_MAX_LENGTH,
    CLIENT_EDITABLE_FIELDS,
    DESCRIPTION_MAX_LENGTH,
    MAX_BODY_BYTES,
    PROFILE_PHOTO_MAX_BYTES,
    PROFILE_PHOTO_TYPES,
    PROFILE_RESPONSE_FIELDS,
    PROFILE_VERTICALS,
    PROFILE_WRITABLE_FIELDS,
    RESTRICTED_FIELDS,
    SECRET_FIELDS,
    TOKEN_HASH_PREFIX,
    TOKEN_PBKDF2_PREFIX,
    _atomic_write,
    _audit,
    _authorize,
    _backup,
    _client_editable_cfg,
    _client_pids,
    _client_provider,
    _client_yaml_path,
    _incoming_differs,
    _merge_incoming,
    _read_cfg,
    hash_token,
    mask_secret,
)
from config.clients import is_valid_client_id, validate_tenant_config
from config.settings import Settings
from whatsapp.errors import MessagingError, MessagingTimeout
from whatsapp.meta_client import MetaWhatsAppClient
from whatsapp.telegram_client import TelegramClient, TelegramError, TelegramTimeout, webhook_url
from whatsapp.zernio_client import ZernioApiClient, ZernioWhatsAppClient

logger = logging.getLogger(__name__)


def _looks_like_email(value: str) -> bool:
    local, _, domain = value.partition("@")
    return bool(local and domain and "." in domain and " " not in value)


def _validate_profile_fields(incoming: dict) -> tuple[dict, list[str]]:
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

    if "vertical" in incoming:
        vertical = str(incoming["vertical"] or "").strip().upper()
        if vertical and vertical not in PROFILE_VERTICALS:
            problems.append(
                "категория бизнеса не из списка WhatsApp — выберите значение из списка"
            )
        else:
            payload["vertical"] = vertical

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
            if len(sites) > 2:
                problems.append("сайтов может быть не больше 2 — Meta принимает 1–2")
            elif any(not site.startswith("https://") for site in sites):
                problems.append("ссылка на сайт должна начинаться с https:// — например https://example.com")
            else:
                payload["websites"] = sites

    return payload, problems


def _make_meta_client(state, settings: Settings, access_token: str, pid: str) -> MetaWhatsAppClient:
    factory = getattr(state, "sender_factory", None)
    if factory is None:
        return MetaWhatsAppClient(access_token, pid, graph_version=settings.meta_graph_version)
    return factory(replace(
        settings,
        whatsapp_access_token=access_token,
        whatsapp_phone_number_id=pid,
    ))


@asynccontextmanager
async def _profile_meta_client(state, settings: Settings, clients_dir: Path, pid: str):
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


@asynccontextmanager
async def _profile_zernio_client(state, settings: Settings, clients_dir: Path, pid: str):
    path = _client_yaml_path(clients_dir, pid)
    cfg = _read_cfg(path) if path else None
    account_id = str((cfg or {}).get("zernio_account_id") or "").strip().lower()
    if not account_id:
        yield None
        return
    await state.refresh_tenants()
    bundle = state.tenants.get(account_id)
    if bundle is not None and isinstance(bundle.sender, ZernioWhatsAppClient):
        yield bundle.sender
        return
    factory = getattr(state, "sender_factory", None)
    if factory is not None:
        client = factory(replace(settings, zernio_account_id=account_id))
    else:
        client = ZernioWhatsAppClient(
            settings.zernio_api_key, account_id, base_url=settings.zernio_base_url,
        )
    try:
        yield client
    finally:
        await client.close()


@asynccontextmanager
async def _profile_client(state, settings: Settings, clients_dir: Path, pid: str):
    if _client_provider(clients_dir, pid) == "zernio":
        async with _profile_zernio_client(state, settings, clients_dir, pid) as client:
            yield client
        return
    async with _profile_meta_client(state, settings, clients_dir, pid) as client:
        yield client


def _profile_failure(exc: MessagingError | MessagingTimeout) -> JSONResponse:
    if isinstance(exc, MessagingTimeout):
        return JSONResponse(status_code=504, content={"error": str(exc)})
    return JSONResponse(status_code=502, content={"error": str(exc)})


def _zernio_api_client(state, settings: Settings) -> ZernioApiClient:
    factory = getattr(state, "zernio_api_factory", None)
    if factory is not None:
        return factory(settings)
    return ZernioApiClient(settings.zernio_api_key, base_url=settings.zernio_base_url)


def _validate_redirect_url(raw: str) -> str | None:
    url = str(raw or "").strip()
    if not url.startswith(("https://", "http://")):
        return None
    return url


async def _setup_telegram_webhook(
    settings: Settings, bot_id: str, bot_token: str, secret: str,
) -> list[str]:
    base = settings.public_base_url
    target = webhook_url(base, bot_id)
    if not base:
        return [
            "PUBLIC_BASE_URL не задан — вебхук Telegram не привязан автоматически. "
            f"Вызовите setWebhook вручную на адрес {target} "
            "(или задайте PUBLIC_BASE_URL и сохраните ещё раз)."
        ]

    notes: list[str] = []
    client = TelegramClient(bot_token)
    try:
        try:
            me = await client.get_me()
            token_bot_id = str(me.get("id") or "").strip()
            if token_bot_id and token_bot_id != bot_id:
                notes.append(
                    f"Токен принадлежит боту {token_bot_id}, а ключ клиента — {bot_id}. "
                    "Пересоздайте клиента с правильным токеном."
                )
        except (TelegramError, TelegramTimeout) as exc:
            notes.append(f"Не удалось проверить токен бота: {exc}")
        try:
            await client.set_webhook(target, secret)
            logger.info("Telegram вебхук привязан: %s", target)
        except (TelegramError, TelegramTimeout) as exc:
            notes.append(
                f"Не удалось привязать вебхук Telegram ({target}): {exc}. "
                "Проверьте, что адрес доступен из интернета по HTTPS."
            )
    finally:
        await client.close()
    return notes


def register_clients_routes(app, settings: Settings, state, clients_dir: Path) -> None:
    @app.get("/admin/clients")
    async def admin_list(request: Request):
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
            if not is_valid_client_id(pid):
                skipped.append({"file": name, "problems": [
                    "имя файла — цифры (Meta phone_number_id / Telegram bot id) "
                    "или slug (Zernio)",
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
                "provider": str(cfg.get("provider") or "wa").strip().lower() or "wa",
                "zernio_account_id": str(cfg.get("zernio_account_id") or "").strip(),
                "has_own_token": bool(str(cfg.get("access_token") or "").strip()),
                "has_management_token": bool(str(cfg.get("management_token") or "").strip()),
                "owner_phone": tenant.owner_phone or "",
                "owner_chat_id": tenant.owner_telegram_chat_id or "",
            })
        return {"clients": clients, "skipped": skipped}

    @app.get("/admin/clients/{pid}")
    async def get_client(pid: str, request: Request):
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
        masked.setdefault("timezone", str(settings.timezone or "Asia/Almaty"))
        return {"phone_number_id": pid, "config_file": path.name, **masked}

    @app.put("/admin/clients/{pid}")
    async def put_client(pid: str, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if not is_valid_client_id(pid):
            return JSONResponse(
                status_code=400,
                content={"error": "ключ клиента — цифры (Meta/TG) или slug (Zernio)"},
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
            if "media" in incoming and isinstance(incoming["media"], dict):
                old_media = (old_cfg or {}).get("media") or {}
                if not isinstance(old_media, dict):
                    old_media = {}
                denied_media = [
                    f"media.{k}" for k in (
                        "daily_limit", "max_audio_seconds", "max_image_mb",
                        "whisper_model", "vision_model", "transcribe_model", "describe_model",
                    )
                    if k in incoming["media"] and incoming["media"][k] != old_media.get(k)
                ]
                if denied_media:
                    return JSONResponse(
                        status_code=403,
                        content={"error": f"изменение полей {', '.join(denied_media)} доступно только администратору"},
                    )
            incoming = {key: value for key, value in incoming.items() if key in CLIENT_EDITABLE_FIELDS}

        merged = _merge_incoming(old_cfg or {}, incoming)

        if "management_token" in merged:
            raw_token = str(merged["management_token"] or "").strip()
            if raw_token.startswith(TOKEN_HASH_PREFIX) or raw_token.startswith(TOKEN_PBKDF2_PREFIX):
                pass
            elif raw_token:
                merged["management_token"] = hash_token(raw_token)
            else:
                merged.pop("management_token", None)

        provider = str(
            incoming.get("provider") or (old_cfg or {}).get("provider") or "wa"
        ).strip().lower() or "wa"
        merged["provider"] = provider
        if provider == "tg" and not str(merged.get("telegram_webhook_secret") or "").strip():
            merged["telegram_webhook_secret"] = secrets.token_urlsafe(32)

        tenant, problems, warnings = validate_tenant_config(merged, settings, pid, f"{pid}.yaml")
        if tenant is None:
            return JSONResponse(status_code=400, content={"ok": False, "problems": problems})

        if path is not None:
            _backup(path, clients_dir, pid)
        target = path or clients_dir / f"{pid}.yaml"
        _atomic_write(target, merged)
        actor = "admin" if role == "admin" else f"client:{pid}"
        _audit(clients_dir, actor, "put", pid, old_cfg, merged)
        await state.refresh_tenants()
        if provider == "tg":
            warnings = list(warnings) + await _setup_telegram_webhook(
                settings, pid, tenant.telegram_bot_token, tenant.telegram_webhook_secret,
            )
        logger.info("Админ-API: конфиг %s записан (%s, провайдер=%s)", pid, role, provider)
        return {"ok": True, "phone_number_id": pid, "provider": provider, "warnings": warnings}

    @app.delete("/admin/clients/{pid}")
    async def delete_client(pid: str, request: Request):
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
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})
        if _client_provider(clients_dir, pid) == "tg":
            return JSONResponse(
                status_code=409,
                content={"error": "профиль и аватар есть только у WhatsApp-клиентов (wa/zernio)"},
            )
        async with _profile_client(state, settings, clients_dir, pid) as profile_client:
            if profile_client is None:
                return JSONResponse(
                    status_code=409,
                    content={"error": "у клиента нет доступа для работы с профилем"},
                )
            try:
                profile = await profile_client.get_business_profile()
            except (MessagingError, MessagingTimeout) as exc:
                return _profile_failure(exc)
        return {
            key: profile.get(key, [] if key == "websites" else "")
            for key in PROFILE_RESPONSE_FIELDS
        }

    @app.patch("/admin/clients/{pid}/profile")
    async def patch_client_profile(pid: str, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})
        if _client_provider(clients_dir, pid) == "tg":
            return JSONResponse(
                status_code=409,
                content={"error": "профиль и аватар есть только у WhatsApp-клиентов (wa/zernio)"},
            )
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
        async with _profile_client(state, settings, clients_dir, pid) as profile_client:
            if profile_client is None:
                return JSONResponse(
                    status_code=409,
                    content={"error": "у клиента нет доступа для работы с профилем"},
                )
            try:
                await profile_client.update_business_profile(payload)
            except (MessagingError, MessagingTimeout) as exc:
                return _profile_failure(exc)
        actor = "admin" if role == "admin" else f"client:{pid}"
        _audit(clients_dir, actor, "profile", pid, {}, payload)
        logger.info("Админ-API: профиль %s обновлён (%s): %s", pid, role, ", ".join(sorted(payload)))
        return {"ok": True, "changed": sorted(payload)}

    @app.post("/admin/clients/{pid}/profile/photo")
    async def upload_client_profile_photo(pid: str, request: Request,
                                          file: UploadFile = File(...)):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нет доступа"})
        if _client_yaml_path(clients_dir, pid) is None:
            return JSONResponse(status_code=404, content={"error": "клиент не найден"})
        if _client_provider(clients_dir, pid) == "tg":
            return JSONResponse(
                status_code=409,
                content={"error": "профиль и аватар есть только у WhatsApp-клиентов (wa/zernio)"},
            )
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
        async with _profile_client(state, settings, clients_dir, pid) as profile_client:
            if profile_client is None:
                return JSONResponse(
                    status_code=409,
                    content={"error": "у клиента нет доступа для работы с профилем"},
                )
            try:
                await profile_client.upload_profile_photo(
                    f"whatsapp-profile{suffix}", content, content_type,
                )
            except (MessagingError, MessagingTimeout) as exc:
                return _profile_failure(exc)
        actor = "admin" if role == "admin" else f"client:{pid}"
        _audit(clients_dir, actor, "profile_photo", pid, {}, {"photo": "avatar"})
        logger.info("Админ-API: аватар %s обновлён (%s)", pid, role)
        return {"ok": True}

    def _zernio_guard(pid: str, request: Request):
        role, error = _authorize(settings, request, clients_dir, pid)
        if error is not None:
            return None, None, JSONResponse(status_code=error, content={"error": "нет доступа"})
        path = _client_yaml_path(clients_dir, pid)
        if path is None:
            return None, None, JSONResponse(status_code=404, content={"error": "клиент не найден"})
        if _client_provider(clients_dir, pid) != "zernio":
            return None, None, JSONResponse(
                status_code=409,
                content={"error": "подключение Zernio доступно только клиентам provider: zernio"},
            )
        if not settings.zernio_api_key:
            return None, None, JSONResponse(
                status_code=409,
                content={"error": "не задан ZERNIO_API_KEY в .env"},
            )
        return role, _read_cfg(path) or {}, None

    @app.post("/admin/clients/{pid}/zernio/connect-link")
    async def zernio_connect_link(pid: str, request: Request):
        role, cfg, error = _zernio_guard(pid, request)
        if error is not None:
            return error
        try:
            incoming = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON"})
        if not isinstance(incoming, dict):
            return JSONResponse(status_code=400, content={"error": "ожидается JSON-объект"})
        redirect_url = _validate_redirect_url(incoming.get("redirect_url") or "")
        if not redirect_url:
            redirect_url = _validate_redirect_url(
                f"{settings.public_base_url}/connect/done" if settings.public_base_url else ""
            )
        if not redirect_url:
            return JSONResponse(status_code=400, content={"error": (
                "не задан redirect_url и PUBLIC_BASE_URL — некуда вернуть клиента "
                "после подключения")})

        client = _zernio_api_client(state, settings)
        try:
            profile_id = str(cfg.get("zernio_profile_id") or "").strip()
            if not profile_id:
                name = str(cfg.get("business_name") or "").strip() or pid
                profile = await client.create_profile(name)
                profile_id = str(profile.get("_id") or "").strip()
                if not profile_id:
                    return JSONResponse(status_code=502, content={
                        "error": "Zernio не вернул id созданного профиля"})
                actor = "admin" if role == "admin" else f"client:{pid}"
                cfg = _client_editable_cfg(
                    clients_dir, pid, cfg, {"zernio_profile_id": profile_id},
                    actor, "zernio_profile", state,
                )
                await state.refresh_tenants()
            auth_url = await client.whatsapp_connect_url(
                profile_id, redirect_url,
                onboarding="api",
                brand_name=str(cfg.get("business_name") or "").strip(),
            )
        except (MessagingError, MessagingTimeout) as exc:
            return _profile_failure(exc)
        finally:
            await client.close()
        if not auth_url:
            return JSONResponse(status_code=502, content={"error": "Zernio не вернул ссылку подключения"})
        return {"ok": True, "authUrl": auth_url, "profileId": profile_id,
                "redirectUrl": redirect_url}

    @app.post("/admin/clients/{pid}/zernio/sync-account")
    async def zernio_sync_account(pid: str, request: Request):
        role, cfg, error = _zernio_guard(pid, request)
        if error is not None:
            return error
        profile_id = str(cfg.get("zernio_profile_id") or "").strip()
        if not profile_id:
            return JSONResponse(status_code=409, content={"error": (
                "у клиента нет zernio_profile_id — сначала сгенерируйте ссылку "
                "подключения")})
        client = _zernio_api_client(state, settings)
        try:
            accounts = await client.list_accounts(profile_id)
        except (MessagingError, MessagingTimeout) as exc:
            return _profile_failure(exc)
        finally:
            await client.close()
        whatsapp = [
            a for a in accounts
            if str(a.get("platform") or "").lower() == "whatsapp" and a.get("_id")
        ]
        if not whatsapp:
            return JSONResponse(status_code=409, content={"error": (
                "в этом профиле Zernio пока нет подключённого WhatsApp-номера — "
                "отправьте клиенту ссылку и повторите после подключения")})
        if len(whatsapp) > 1:
            return JSONResponse(status_code=409, content={"error": (
                "в профиле больше одного WhatsApp-аккаунта — оставьте один "
                "или впишите zernio_account_id вручную")})
        account_id = str(whatsapp[0]["_id"]).strip().lower()
        actor = "admin" if role == "admin" else f"client:{pid}"
        _client_editable_cfg(
            clients_dir, pid, cfg, {"zernio_account_id": account_id},
            actor, "zernio_account", state,
        )
        await state.refresh_tenants()
        logger.info("Админ-API: Zernio-аккаунт %s привязан к %s", account_id, pid)
        return {"ok": True, "accountId": account_id,
                "username": str(whatsapp[0].get("username") or "")}

    @app.get("/admin/clients/{pid}/zernio/number-info")
    async def zernio_number_info(pid: str, request: Request):
        role, cfg, error = _zernio_guard(pid, request)
        if error is not None:
            return error
        if not str(cfg.get("zernio_account_id") or "").strip():
            return JSONResponse(status_code=409, content={"error": (
                "номер ещё не подключён — сначала сгенерируйте ссылку и нажмите "
                "«Проверить и сохранить»")})
        async with _profile_zernio_client(state, settings, clients_dir, pid) as client:
            if client is None:
                return JSONResponse(status_code=409, content={"error": "нет доступа к Zernio"})
            try:
                info = await client.get_number_info()
            except (MessagingError, MessagingTimeout) as exc:
                return _profile_failure(exc)
        phone = info.get("phone") if isinstance(info.get("phone"), dict) else {}
        waba = info.get("waba") if isinstance(info.get("waba"), dict) else {}
        return {
            "ok": True,
            "status": str(phone.get("status") or ""),
            "displayPhoneNumber": str(phone.get("display_phone_number") or ""),
            "verifiedName": str(phone.get("verified_name") or ""),
            "nameStatus": str(phone.get("name_status") or ""),
            "qualityRating": str(phone.get("quality_rating") or ""),
            "messagingLimitTier": str(phone.get("messaging_limit_tier") or ""),
            "platformType": str(phone.get("platform_type") or ""),
            "wabaName": str(waba.get("name") or "") if waba else "",
        }

    @app.post("/admin/clients/{pid}/zernio/register-number")
    async def zernio_register_number(pid: str, request: Request):
        role, cfg, error = _zernio_guard(pid, request)
        if error is not None:
            return error
        if not str(cfg.get("zernio_account_id") or "").strip():
            return JSONResponse(status_code=409, content={"error": (
                "номер ещё не подключён — сначала сгенерируйте ссылку и нажмите "
                "«Проверить и сохранить»")})
        try:
            incoming = json.loads(await request.body() or b"{}")
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "тело должно быть JSON"})
        pin = str((incoming or {}).get("pin") or "").strip() if isinstance(incoming, dict) else ""
        if pin and not re.fullmatch(r"\d{6}", pin):
            return JSONResponse(status_code=400, content={"error": "PIN — ровно 6 цифр"})
        async with _profile_zernio_client(state, settings, clients_dir, pid) as client:
            if client is None:
                return JSONResponse(status_code=409, content={"error": "нет доступа к Zernio"})
            try:
                result = await client.register_number(pin)
            except (MessagingError, MessagingTimeout) as exc:
                return _profile_failure(exc)
        actor = "admin" if role == "admin" else f"client:{pid}"
        _audit(clients_dir, actor, "zernio_register", pid, {}, {"registered": True})
        logger.info("Админ-API: номер %s зарегистрирован в Cloud API", pid)
        return {"ok": True, "registered": bool(result.get("registered")),
                "phoneNumberId": str(result.get("phoneNumberId") or "")}

    @app.get("/admin/clients/{pid}/zernio/accounts")
    async def zernio_profile_accounts(pid: str, request: Request):
        role, cfg, error = _zernio_guard(pid, request)
        if error is not None:
            return error
        profile_id = str(cfg.get("zernio_profile_id") or "").strip()
        client = _zernio_api_client(state, settings)
        try:
            accounts = await client.list_accounts(profile_id) if profile_id else []
        except (MessagingError, MessagingTimeout) as exc:
            return _profile_failure(exc)
        finally:
            await client.close()
        return {"ok": True, "profileId": profile_id, "accounts": [
            {"accountId": str(a.get("_id") or ""), "platform": str(a.get("platform") or ""),
             "username": str(a.get("username") or ""), "isActive": bool(a.get("isActive"))}
            for a in accounts
        ]}

    @app.post("/admin/zernio/register-webhook")
    async def zernio_register_webhook(request: Request):
        role, error = _authorize(settings, request, clients_dir, pid=None)
        if error is not None:
            return JSONResponse(status_code=error, content={"error": "нужен админ-токен"})
        if role != "admin":
            return JSONResponse(status_code=403, content={"error": "только для администратора"})
        if not settings.zernio_api_key:
            return JSONResponse(status_code=409, content={"error": "не задан ZERNIO_API_KEY в .env"})
        if not settings.zernio_webhook_secret:
            return JSONResponse(status_code=409, content={"error": (
                "не задан ZERNIO_WEBHOOK_SECRET в .env — задайте и перезапустите сервис")})
        if not settings.public_base_url:
            return JSONResponse(status_code=409, content={"error": (
                "не задан PUBLIC_BASE_URL в .env — вебхук должен указывать на "
                "публичный адрес сервиса")})
        target = f"{settings.public_base_url}/webhooks/zernio"
        client = _zernio_api_client(state, settings)
        try:
            webhook = await client.create_webhook(
                "whatsapp-bot", target, settings.zernio_webhook_secret,
                ["message.received", "message.sent", "message.failed"],
            )
        except (MessagingError, MessagingTimeout) as exc:
            return _profile_failure(exc)
        finally:
            await client.close()
        logger.info("Админ-API: вебхук Zernio зарегистрирован на %s", target)
        return {"ok": True, "url": target,
                "webhookId": str(webhook.get("_id") or "")}
