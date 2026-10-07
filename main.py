"""Точка входа: FastAPI-сервер, принимающий вебхуки WhatsApp (Zernio или Meta).

Запуск: python main.py
Все настройки берутся из .env; конфиги клиентов — из config/ (single-tenant,
CLIENT_CONFIG) или из папки clients/ (мультитенант, один файл на клиента,
имя файла = ключ клиента; включается переменной CLIENTS_DIR или наличием
непустой папки clients/).

Схема работы: провайдер POST'ит входящие сообщения на /webhooks/{zernio|meta}.
Мы проверяем подпись, сразу отвечаем 200 (у провайдера лимит на быстрый ack)
и обрабатываем сообщение в фоновой задаче: ключевые слова -> LLM ->
[HANDOFF] -> ответ клиенту и уведомление владельцу.
"""

import asyncio
import hmac
import json
import logging
import os
import re
import shutil
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from admin.api import register_admin_api, RateLimitMiddleware, CSPMiddleware
from config.clients import ClientRegistry
from config.settings import Settings, get_settings
from handlers.message_handler import MessageProcessor
from handlers.owner_handler import notify_owner
from services.llm_client import LLMClient
from storage import (
    init_db,
    check_and_add,
    add_message,
    get_context_for_llm,
    get_conversation_by_client_and_phone,
    create_conversation,
    update_conversation_status,
    increment_unread,
    get_open_handoff,
    resolve_handoff,
    update_delivery_status,
    get_message_by_provider_id,
    get_handoffs_for_conversation,
    backup as db_backup,
    cleanup_old_seen_events,
    cleanup_old_conversations,
)
from whatsapp.meta_client import MetaWhatsAppClient
from whatsapp.meta_payload import parse_meta_events
from whatsapp.meta_security import (
    META_HEADER_SIGNATURE,
    QUERY_CHALLENGE,
    QUERY_MODE,
    QUERY_VERIFY_TOKEN,
    verify_meta_signature,
    verify_subscription,
)
from whatsapp.telegram_client import TelegramClient
from whatsapp.telegram_payload import parse_telegram_update
from whatsapp.telegram_token import bot_id_from_token
from whatsapp.zernio_client import ZernioWhatsAppClient
from whatsapp.zernio_payload import parse_zernio_events, ZernioEvent
from whatsapp.zernio_security import (
    HEADER_SIGNATURE as ZERNIO_HEADER_SIGNATURE,
    HEADER_SIGNATURE_LEGACY as ZERNIO_HEADER_SIGNATURE_LEGACY,
    verify_zernio_signature,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "bot.log"

# Дедупликация доставок: провайдеры доставляют at-least-once, при ретрае у
# события тот же id — второй раз отвечать клиенту нельзя.
# Храним только последние id, чтобы словарь не рос бесконечно.
SEEN_WEBHOOK_IDS_MAX = 4096

# Заголовок с секретом вебхука Telegram (задаётся при setWebhook).
TELEGRAM_HEADER_SECRET = "X-Telegram-Bot-Api-Secret-Token"


def setup_logging(level: str) -> None:
    """Логи пишутся одновременно в файл logs/bot.log и в консоль."""
    LOG_DIR.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")

    file_handler = RotatingFileHandler(
        LOG_DIR / "bot.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    root.addHandler(file_handler)
    root.addHandler(console_handler)

    # Слишком болтливые библиотеки — только предупреждения и выше.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def build_meta_sender(settings: Settings) -> MetaWhatsAppClient:
    """Meta-клиент для конкретного клиента: его токен и его phone_number_id.

    Вынесен в фабрику, чтобы тесты подменяли транспорт (MockTransport) без
    правки кода: create_app(settings, sender_factory=...).
    """
    return MetaWhatsAppClient(
        settings.whatsapp_access_token,
        settings.whatsapp_phone_number_id,
        graph_version=settings.meta_graph_version,
    )


def build_sender(settings: Settings):
    """Клиент отправки под провайдера клиента (meta / zernio / telegram).

    Провайдер определяет Settings клиента: у WhatsApp-клиентов reestr ставит
    messaging_provider="meta" или "zernio", у Telegram — "telegram". Все
    клиенты реализуют один интерфейс send_text(to, text, conversation_id)/close(),
    поэтому обработчик и уведомления владельцу от транспорта не зависят.
    """
    if settings.messaging_provider == "telegram":
        return TelegramClient(settings.telegram_bot_token)
    if settings.messaging_provider == "zernio":
        return ZernioWhatsAppClient(
            settings.zernio_api_key,
            settings.zernio_account_id,
            base_url=settings.zernio_base_url,
        )
    return build_meta_sender(settings)


class TenantBundle:
    """Рабочий набор одного клиента мультитенанта.

    У каждого клиента свои: настройки (Settings из его yaml), LLM-клиент
    (общий ключ из env, но параметры модели — из его yaml), Meta-клиент
    (токен его WABA) и MessageProcessor (свой system prompt и своя
    история диалогов). Бандл пересоздаётся при изменении yaml клиента;
    если конфиг не менялся — переиспользуется, история диалогов живёт.
    """

    def __init__(self, settings: Settings, llm_client, sender, processor) -> None:
        self.settings = settings
        self.llm_client = llm_client
        self.sender = sender
        self.processor = processor

    async def close(self) -> None:
        for resource in (self.sender, self.llm_client):
            try:
                await resource.close()
            except Exception:
                logger.exception(
                    "Ошибка закрытия ресурсов клиента %s",
                    self.settings.whatsapp_phone_number_id,
                )


class WebhookState:
    """Общие объекты приложения: настройки, клиенты, обработчики, seen-кэш.

    Single-tenant (clients_dir пуст): один sender + один processor, как раньше.
    Мультитенант (Meta): реестр clients/ и по бандлу на каждого клиента —
    маршрутизация входящих по phone_number_id.
    Создаётся один раз на жизненный цикл приложения (lifespan).
    """

    def __init__(self, settings: Settings, sender_factory: Callable | None = None) -> None:
        self.settings = settings
        # Дедупликация теперь в БД (storage.seen_events), но держим in-memory
        # LRU как быстрый кэш для горячих событий (fallback на случай проблем с БД).
        self._seen_webhook_ids: OrderedDict[str, None] = OrderedDict()
        self._reload_lock = asyncio.Lock()
        # Мультитенант обслуживает все транспорты: WhatsApp-клиенты (meta или
        # zernio) и Telegram-клиенты (telegram) живут в одном реестре clients/.
        self.multitenant = bool(settings.clients_dir) and settings.messaging_provider in (
            "meta", "zernio", "telegram",
        )

        if not self.multitenant:
            # Single-tenant: один LLM-клиент и один транспорт на всё приложение.
            # Провайдер задаёт MESSAGING_PROVIDER (meta / zernio / telegram).
            self.registry = None
            self.tenants: dict[str, TenantBundle] = {}
            self.llm_client = LLMClient(settings.llm_api_url, settings.llm_api_key, settings.llm)
            self.sender = build_sender(settings)
            self.processor = MessageProcessor(settings, self.llm_client, self.sender)
            return

        # Мультитенант: по бандлу на каждого клиента реестра.
        self.registry = ClientRegistry(Path(settings.clients_dir), settings)
        self.sender_factory = sender_factory or build_sender
        self.tenants: dict[str, TenantBundle] = {
            pid: self._build_tenant(tenant_settings)
            for pid, tenant_settings in self.registry.tenants.items()
        }
        # В мультитенанте общих sender/processor нет — только per-tenant.
        self.llm_client = None
        self.sender = None
        self.processor = None

    # --- мультитенант: бандлы клиентов ------------------------------------------

    def _build_tenant(self, tenant_settings: Settings) -> TenantBundle:
        """Собирает бандл одного клиента из его Settings."""
        pid = tenant_settings.whatsapp_phone_number_id
        llm_client = LLMClient(self.settings.llm_api_url, self.settings.llm_api_key, tenant_settings.llm)
        sender = self.sender_factory(tenant_settings)
        processor = MessageProcessor(tenant_settings, llm_client, sender)
        return TenantBundle(tenant_settings, llm_client, sender, processor)

    async def refresh_tenants(self) -> None:
        """Hot-reload: сверяет снапшот папки clients/ и пересобирает бандлы.

        Новый yaml -> клиент появился без рестарта. Изменённый yaml ->
        бандл пересобран (история диалогов этого клиента начинается заново).
        Удалённый yaml -> клиент отключён, ресурсы закрыты. Неизменённые
        клиенты продолжают жить со своей историей.
        """
        async with self._reload_lock:
            if not self.registry.maybe_reload():
                return
            stale: list[TenantBundle] = []
            new_bundles: dict[str, TenantBundle] = {}
            for pid, tenant_settings in self.registry.tenants.items():
                existing = self.tenants.get(pid)
                if existing is not None and existing.settings == tenant_settings:
                    # yaml не менялся — бандл живёт, история диалогов сохранена.
                    new_bundles[pid] = existing
                    continue
                if old := self.tenants.get(pid):
                    stale.append(old)
                new_bundles[pid] = self._build_tenant(tenant_settings)
                logger.info(
                    "Клиент обновлён | phone_number_id=%s | бизнес=%s",
                    pid, tenant_settings.business_name,
                )
            for pid, old in self.tenants.items():
                if pid not in new_bundles:
                    stale.append(old)
                    logger.info("Клиент отключён | phone_number_id=%s", pid)
            self.tenants = new_bundles
        for bundle in stale:
            await bundle.close()

    # --- маршрутизация -----------------------------------------------------------

    async def processor_for(self, inbound):
        """Процессор клиента для входящего сообщения по ключу маршрутизации.

        В single-tenant процессор всегда один. В мультитенанте перед поиском
        сверяется снапшот папки clients/ (hot-reload), затем клиент ищется
        по inbound.phone_number_id (у Meta — ID бизнес-номера, у Zernio —
        accountId); None — номер/аккаунт не зарегистрирован.
        """
        if not self.multitenant:
            return self.processor
        await self.refresh_tenants()
        bundle = self.tenants.get(inbound.phone_number_id)
        return bundle.processor if bundle else None

    # --- дедупликация доставок ---------------------------------------------------

    def seen_before(self, webhook_id: str) -> bool:
        """True, если доставку с таким id уже обрабатывали.

        Регистрирует только последние id, чтобы кэш не рос бесконечно. Для Meta
        сюда идёт id сообщения (wamid...), для Zernio — id события/сообщения,
        для Telegram — update_id.
        """
        if not webhook_id:
            return False
        if webhook_id in self._seen_webhook_ids:
            return True
        self._seen_webhook_ids[webhook_id] = None
        while len(self._seen_webhook_ids) > SEEN_WEBHOOK_IDS_MAX:
            self._seen_webhook_ids.popitem(last=False)
        return False

    async def handle_event(self, inbound, processor: MessageProcessor | None = None) -> None:
        """Обработка разобранного входящего сообщения (фоновая задача).

        Единая точка входа из вебхука: приветствие по слову «start»,
        нетекстовый контент — вежливая просьба написать текстом, остальное —
        полный сценарий MessageProcessor. Процессор подбирается по клиенту
        (processor_for); в single-tenant он один. Любое исключение гасится
        здесь: сбой обработки не должен ронять воркер.
        """
        processor = processor or self.processor
        conversation_id = getattr(inbound, "conversation_id", "")
        try:
            if getattr(inbound, "content_kind", "text") in ("text", "") and inbound.text.strip("/").casefold() == "start":
                await processor.handle_greeting(inbound.phone, conversation_id=conversation_id)
            elif (
                getattr(inbound, "media_url", None)
                or getattr(inbound, "content_kind", "") in ("voice", "audio", "image", "video", "document", "file")
            ):
                await processor.handle_media(
                    inbound.phone, inbound.display_name, inbound,
                    conversation_id=conversation_id,
                )
            elif inbound.text:
                await processor.handle_incoming(
                    inbound.phone, inbound.display_name, inbound.text,
                    conversation_id=conversation_id,
                )
            else:
                await processor.handle_non_text(
                    inbound.phone, inbound.display_name, inbound.content_kind,
                    conversation_id=conversation_id,
                )
        except Exception:

            # Падение обработки не должно крашить сервер: провайдер при не-2xx
            # начнёт ретраи, а ответ клиенту уже мог уйти.
            logger.exception("Ошибка при обработке вебхука (%s)", inbound.phone)

    async def shutdown(self) -> None:
        if self.multitenant:
            for bundle in self.tenants.values():
                await bundle.close()
            return
        await self.llm_client.close()
        await self.sender.close()


def create_app(settings: Settings, sender_factory: Callable | None = None) -> FastAPI:
    """Собирает приложение: вебхук активного провайдера + healthcheck.

    settings передаётся явно — так тесты подсовывают тестовые настройки
    без .env (см. tests/), а при запуске python main.py их даёт get_settings().
    Провайдер выбирается переменной MESSAGING_PROVIDER: "meta" -> /webhooks/meta
    (Graph API), "zernio" -> /webhooks/zernio, "telegram" -> /webhooks/telegram.
    Мультитенант включён, если settings.clients_dir задан: тогда маршрутизация
    идёт по ключу клиента из clients/ (sender_factory — точка подмены для тестов).
    """
    if settings.clients_dir:
        logger.info(
            "Запуск бота | режим: мультитенант | папка клиентов: %s | провайдер: %s",
            settings.clients_dir, settings.messaging_provider,
        )
    else:
        logger.info(
            "Запуск бота | бизнес: %s | конфиг: %s | модель: %s | провайдер: %s",
            settings.business_name, settings.config_file, settings.llm.model,
            settings.messaging_provider,
        )

    # Определяем пути: settings.clients_dir > CLIENTS_DIR env > RAILWAY_VOLUME_MOUNT_PATH/clients > авто-детект
    railway_volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    explicit_clients_dir = settings.clients_dir or os.getenv("CLIENTS_DIR")
    
    if explicit_clients_dir:
        # settings.clients_dir или явный CLIENTS_DIR из env имеет наивысший приоритет
        clients_dir = Path(explicit_clients_dir)
        logger.info("CLIENTS_DIR задан: %s", clients_dir)
    elif railway_volume:
        # Авто-детект из Railway Volume
        clients_dir = Path(railway_volume) / "clients"
        if not clients_dir.exists():
            logger.warning("CLIENTS_DIR не найден в Volume: %s не существует", clients_dir)
        logger.info("CLIENTS_DIR авто-детект из Volume: %s", clients_dir)
    else:
        clients_dir = None
        logger.warning("RAILWAY_VOLUME_MOUNT_PATH не задан, CLIENTS_DIR не задан — мультитенант отключён")

    # Определяем путь к БД
    if railway_volume:
        db_dir = Path(railway_volume) / "db"
        db_path = db_dir / "jauap.db"
        backups_dir = db_dir / "backups"
    else:
        # Локальный запуск / фолбэк
        db_path = Path(os.getenv("JAUAP_DB_PATH", "/data/jauap.db"))
        backups_dir = db_path.parent / "backups"

    # Переопределяем пути через env для совместимости с storage
    os.environ["JAUAP_DB_PATH"] = str(db_path)
    if clients_dir:
        os.environ["CLIENTS_DIR"] = str(clients_dir)

    # Создаём директории ДО проверки Volume: на чистом контейнере папки db ещё
    # не существует, и проверка падала с ENOENT, помечая живой volume как битый.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    backups_dir.mkdir(parents=True, exist_ok=True)

    # Инициализация БД (таблицы/миграции) — до любых запросов: вебхуки и
    # админ-API пишут в неё сразу, без этого sqlite падал "no such table".
    from storage import init_db
    init_db()
    logger.info("БД инициализирована: %s", db_path)

    # Проверка Volume: пишем тестовый файл, читаем, удаляем — только так на Railway
    volume_ok = True
    try:
        test_file = db_path.parent / ".volume_test"
        test_file.write_text("ok")
        content = test_file.read_text()
        if content != "ok":
            raise ValueError("Volume read-back mismatch")
        test_file.unlink()
        logger.info("Volume check OK: %s", db_path.parent)
    except Exception as e:
        volume_ok = False
        logger.error("VOLUME CHECK FAILED: %s — %s", db_path.parent, e)

    # Если мультитенант включён через CLIENTS_DIR
    if clients_dir:
        settings = replace(settings, clients_dir=str(clients_dir))

    # APScheduler для фоновых задач
    scheduler = AsyncIOScheduler(timezone="UTC")

    async def scheduled_jobs():
        """Ежедневные задачи: бэкап, чистка."""
        try:
            # 1. Бэкап БД
            from storage import backup as db_backup, get_db_path
            backup_path = Path(os.getenv("JAUAP_DB_PATH", "/data/jauap.db")).parent / "backups" / f"jauap-{datetime.utcnow().strftime('%Y%m%d')}.db"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(db_backup, backup_path)
            logger.info("Ежедневный бэкап создан: %s", backup_path)

            # 2. Чистка seen_events > 7 дней
            from storage import cleanup_old_seen_events
            deleted = await asyncio.to_thread(cleanup_old_seen_events, 7)
            logger.info("Чистка seen_events: удалено %d записей", deleted)

            # 3. Чистка старых диалогов (> 365 дней)
            from storage import cleanup_old_conversations
            deleted = await asyncio.to_thread(cleanup_old_conversations, 365)
            logger.info("Чистка старых диалогов: удалено %d диалогов", deleted)

            # 4. Чистка медиафайлов старше media_retention_days (30 дней)
            from storage import cleanup_expired_media
            retention_days = int(getattr(settings, "media_retention_days", 30) or 30)
            cleaned_media = await asyncio.to_thread(cleanup_expired_media, retention_days)
            logger.info("Чистка старых медиафайлов: удалено %d файлов", cleaned_media)

        except Exception as e:

            logger.exception("Ошибка в ежедневных задачах: %s", e)

    async def reminder_job():
        """Напоминание менеджерам каждые 15 минут: если handoff без ответа > 2ч.

        Приходит ровно одно напоминание (ставится reminded_at).
        """
        from storage import get_handoffs_needing_reminder, mark_reminded
        handoffs = await asyncio.to_thread(get_handoffs_needing_reminder, 2)
        for h in handoffs:
            try:
                hid = h["handoff_id"]
                cid = h["conversation_id"]
                ckey = h.get("client_key")
                # Находим подходящие settings и sender
                target_settings = settings
                target_sender = getattr(state, "sender", None)
                if state.multitenant and ckey:
                    bundle = state.tenants.get(ckey)
                    if bundle:
                        target_settings = bundle.settings
                        target_sender = bundle.sender

                who = f"{h.get('contact_name') or 'клиент'} ({h.get('contact_phone') or '-'})"
                text = (
                    f"⏰ Напоминание: диалог без ответа более 2 часов!\n"
                    f"От: {who}\n"
                    f"Причина: {h.get('reason') or '-'}"
                )
                from handlers.owner_handler import _notify_recipients, _dialog_button, _send_telegram_notification
                recipients = _notify_recipients(target_settings)
                button = _dialog_button(target_settings, cid)
                for chat_id in recipients:
                    try:
                        await _send_telegram_notification(
                            target_sender, target_settings, chat_id, text, button, disable_notification=False,
                        )
                    except Exception as err:
                        logger.warning("Не удалось отправить напоминание в Telegram (%s): %s", chat_id, err)

                await asyncio.to_thread(mark_reminded, hid)
                logger.info("Напоминание отправлено по handoff_id=%s | conv_id=%s", hid, cid)
            except Exception as e:
                logger.exception("Ошибка в reminder_job для handoff %s: %s", h.get("handoff_id"), e)

    async def manual_timeout_job():
        """Автовозврат к боту: диалоги, висевшие в manual дольше таймаута.

        В мультитенанте таймаут свой у каждого клиента, поэтому обходим
        тенантов по одному. Возвращает диалог в режим бота и чистит счётчик
        непрочитанных — дальше бот снова отвечает сам.
        """
        from storage import (
            get_conversations_needing_timeout_check,
            mark_read,
            update_conversation_status,
            get_open_handoff,
            resolve_handoff,
        )

        targets: list[tuple[int, str]] = []
        if state.multitenant:
            await state.refresh_tenants()
            for bundle in state.tenants.values():
                ts = bundle.settings
                db_key = ts.whatsapp_phone_number_id or ts.zernio_account_id
                targets.append((int(ts.manual_timeout_hours or 12), db_key))
        else:
            targets.append((int(settings.manual_timeout_hours or 12), None))

        total = 0
        for hours, db_key in targets:
            convs = await asyncio.to_thread(get_conversations_needing_timeout_check, hours, db_key)
            for conv in convs:
                await asyncio.to_thread(update_conversation_status, conv["id"], "bot")
                await asyncio.to_thread(mark_read, conv["id"])
                open_h = await asyncio.to_thread(get_open_handoff, conv["id"])
                if open_h:
                    await asyncio.to_thread(resolve_handoff, open_h["id"])
                total += 1
                logger.info(
                    "Диалог возвращён боту по таймауту | conv_id=%s | часов=%d",
                    conv["id"], hours,
                )
        if total:
            logger.info("Автовозврат к боту: обработано диалогов — %d", total)

    # Планировщик: job добавляем здесь, а запуск — в lifespan (нужен работающий
    # event loop; create_app вызывается при импорте модуля, до старта loop).
    scheduler.add_job(scheduled_jobs, "cron", hour=3, minute=0, id="daily_maintenance", replace_existing=True)
    scheduler.add_job(manual_timeout_job, "interval", hours=1, id="manual_timeout", replace_existing=True)
    scheduler.add_job(reminder_job, "interval", minutes=15, id="reminder_job", replace_existing=True)

    # Lifespan контекст-менеджер (должен быть определён до создания FastAPI app)
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler.start()
        logger.info(
            "APScheduler запущен: ежедневные задачи в 03:00 UTC, "
            "автовозврат из ручного режима — раз в час, напоминания — каждые 15 мин"
        )
        # Привязываем адрес вебхука бота JAUAP на стороне Telegram: без этого
        # бот не пришлёт /start <код> и привязка чата менеджера не сработает.
        if settings.telegram_owner_bot_token and settings.public_base_url:
            from whatsapp.telegram_client import TelegramClient, webhook_owner_url
            owner_bot = TelegramClient(settings.telegram_owner_bot_token)
            try:
                await owner_bot.set_webhook(
                    webhook_owner_url(settings.public_base_url),
                    settings.telegram_owner_webhook_secret,
                )
                logger.info("Вебхук бота JAUAP зарегистрирован: %s",
                            webhook_owner_url(settings.public_base_url))
            except Exception:
                logger.exception(
                    "Не удалось зарегистрировать вебхук бота JAUAP — привязка чата "
                    "менеджера не заработает (проверьте токен и PUBLIC_BASE_URL)"
                )
            finally:
                await owner_bot.close()
        app.state.state = state
        app.state.scheduler = scheduler
        app.state.volume_ok = volume_ok
        if not volume_ok:
            logger.error("HEALTH CHECK: Volume недоступен, сервис может работать некорректно")
        yield
        scheduler.shutdown(wait=True)
        await state.shutdown()
        logger.info("Бот остановлен")

    # Создаём FastAPI приложение
    app = FastAPI(lifespan=lifespan)

    # Безопасность панели /admin: ограничение попыток входа (10 за 10 минут с
    # одного IP) и защитные заголовки (CSP, nosniff, frame-ancestors none).
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(CSPMiddleware)

    state = WebhookState(settings, sender_factory)
    app.state.state = state

    # Регистрация вебхуков
    if state.multitenant:
        # В мультитенанте доступны все транспорты: WhatsApp-вебхук поднимаем
        # только если заданы секреты провайдера (иначе деплой чисто для
        # Telegram), Telegram-вебхук — всегда (у каждого бота свой токен).
        if settings.meta_app_secret and settings.meta_verify_token:
            _register_meta_webhook(app, settings, state)
        else:
            logger.warning(
                "Вебхук Meta не зарегистрирован: не заданы META_APP_SECRET/"
                "META_VERIFY_TOKEN — Meta-клиенты не обслуживаются"
            )
        if settings.zernio_webhook_secret:
            _register_zernio_webhook(app, settings, state)
        else:
            logger.warning(
                "Вебхук Zernio не зарегистрирован: не задан ZERNIO_WEBHOOK_SECRET "
                "— Zernio-клиенты не обслуживаются"
            )
        _register_telegram_webhook(app, settings, state)
    elif settings.messaging_provider == "meta":
        _register_meta_webhook(app, settings, state)
    elif settings.messaging_provider == "zernio":
        _register_zernio_webhook(app, settings, state)
    elif settings.messaging_provider == "telegram":
        _register_telegram_webhook(app, settings, state)

    # Вебхук Telegram-бота владельца (для уведомлений): регистрируем, если задан токен.
    if settings.telegram_owner_bot_token:
        _register_telegram_owner_webhook(app, settings, state)
        if not settings.telegram_owner_webhook_secret:
            logger.warning(
                "Секрет вебхука бота JAUAP не задан (TELEGRAM_OWNER_WEBHOOK_SECRET) — "
                "запросы /webhooks/telegram-owner не проверяются"
            )
    else:
        logger.warning(
            "Бот JAUAP не настроен: TELEGRAM_OWNER_BOT_TOKEN пуст — уведомления "
            "менеджерам в Telegram и привязка чатов работать не будут"
        )

    # Админ-API и панель /admin: только в мультитенанте и с заданным
    # ADMIN_TOKEN (иначе роуты не существуют — см. admin/api.py).
    if state.multitenant:
        register_admin_api(app, settings, state)

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "volume_ok": volume_ok,
            "db_path": str(db_path),
            "clients_dir": os.getenv("CLIENTS_DIR"),
            "scheduler_running": scheduler.running,
            "clients": len(state.tenants) if state.multitenant else 1,
            # Что сервис видит из окружения: без этого непонятно, почему
            # не работает привязка менеджера или кнопка в уведомлении.
            "telegram_owner_bot": bool(settings.telegram_owner_bot_token),
            "public_base_url": bool(settings.public_base_url),
        }

    @app.get("/privacy")
    async def privacy():
        """Короткая памятка о данных клиента — открывается по ссылке из браузера.

        Ссылку удобно указать в профиле бизнеса WhatsApp: клиенты видят,
        какие данные собираются и как их удалить. В мультитенанте страница
        общая для всех клиентов (свой текст появится с админкой/БД).
        """
        if state.multitenant:
            return _multitenant_privacy()
        return _single_tenant_privacy(settings)

    @app.get("/connect/done")
    async def connect_done():
        """Страница возврата после Embedded Signup (redirect_url у Zernio).

        Клиент попадает сюда из браузера после подключения номера: показываем
        нейтральное «готово, закройте окно». Сам accountId панель забирает из
        Zernio кнопкой «Проверить подключение».
        """
        return HTMLResponse(
            "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
            "<title>Готово</title></head><body style='font-family:sans-serif;"
            "max-width:32rem;margin:4rem auto;text-align:center'>"
            "<h1>Подключение завершено</h1>"
            "<p>Номер подключён. Можно закрыть эту страницу и вернуться в бот.</p>"
            "</body></html>"
        )

    return app


def _single_tenant_privacy(settings: Settings) -> dict:
    """Памятка о данных клиента для single-tenant (имя бизнеса подставляется)."""
    return {
        "business": settings.business_name,
        "what_we_store": (
            "Бот хранит только несколько последних сообщений этого диалога "
            "— в оперативной памяти, без базы данных. История стирается "
            "командой «start» от вас или при перезапуске сервиса."
        ),
        "how_we_use": (
            "Ваши сообщения используются только для ответов на вопросы "
            f"о {settings.business_name}. Мы не пересылаем их третьим "
            "лицам: при необходимости менеджеру уходит лишь ваш вопрос."
        ),
        "delete": (
            "Чтобы удалить переписку и связаться с человеком, напишите "
            "«хочу человека» — владелец свяжется с вами."
        ),
    }


def _multitenant_privacy() -> dict:
    """Общая памятка для всех клиентов мультитенантного деплоя."""
    return {
        "business": "помощник бизнеса в WhatsApp",
        "what_we_store": (
            "Бот хранит только несколько последних сообщений этого диалога "
            "— в оперативной памяти, без базы данных. История стирается "
            "командой «start» от вас или при перезапуске сервиса."
        ),
        "how_we_use": (
            "Ваши сообщения используются только для ответов на вопросы "
            "о бизнесе, номер которого вам написал. Мы не пересылаем их "
            "третьим лицам: при необходимости менеджеру уходит лишь ваш вопрос."
        ),
        "delete": (
            "Чтобы удалить переписку и связаться с человеком, напишите "
            "«хочу человека» — владелец свяжется с вами."
        ),
    }


# Кэш бизнес-номеров по accountId (для фильтрации message.sent от бизнеса).
# Храним и пустой результат: запрос в Zernio идёт больше секунды, а без кэша
# пустого ответа он повторялся на каждом входящем и задерживал подтверждение.
_business_phone_cache: dict[str, tuple[str, float]] = {}
BUSINESS_PHONE_CACHE_TTL = 900  # 15 минут


async def _get_business_phone(account_id: str, zernio_client) -> str:
    """Бизнес-номер из кэша или Zernio API (пустой результат тоже кэшируется)."""
    cached = _business_phone_cache.get(account_id)
    if cached and time.monotonic() - cached[1] < BUSINESS_PHONE_CACHE_TTL:
        return cached[0]

    phone = ""
    try:
        # Используем Zernio API для получения информации об аккаунте
        info = await zernio_client.get_number_info()
        phone = str(info.get("username") or info.get("phoneNumber") or "").strip()
    except Exception:
        logger.debug("Не удалось получить бизнес-номер из Zernio", exc_info=True)
    _business_phone_cache[account_id] = (phone, time.monotonic())
    return phone


async def _handle_zernio_inbound(state: WebhookState, event, processor) -> None:
    """Фоновая обработка входящего Zernio: фильтр «от бизнеса» + сценарий бота.

    Вынесено из вебхука, чтобы ack не ждал сетевых запросов к Zernio: чем
    раньше подтверждено событие, тем раньше клиент увидит ответ.
    """
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


def _register_zernio_webhook(app: FastAPI, settings: Settings, state: WebhookState) -> None:
    @app.post("/webhooks/zernio")
    async def zernio_webhook(request: Request, background_tasks: BackgroundTasks):
        """Приём вебхука Zernio: проверка X-Zernio-Signature -> ack 200 -> фон.

        Обрабатывает: message.received, message.sent, message.failed, message.delivered, message.read.
        Фильтрует сообщения от бизнеса (sender.phoneNumber == бизнес-номер).
        Дедупликация через БД (storage.seen_events.check_and_add).
        """
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
        # Замер разбора вебхука: если он упирается в сотни миллисекунд, Zernio
        # может ретраить, и вся задержка до клиента растёт.
        logger.info(
            "Вебхук Zernio разобран | событий=%d | %.3f с",
            len(events), time.monotonic() - received_at,
        )
        if not events:
            return {"ok": True}

        for event in events:
            # Дедупликация через БД (с учётом типа события, чтобы sent/delivered/read не блокировали друг друга)
            raw_id = event.provider_message_id or event.timestamp or ""
            event_id = f"zernio:{event.event_type}:{raw_id}" if raw_id else ""
            if event_id and not check_and_add(event_id):
                logger.debug("Дубликат вебхука Zernio проигнорирован: event_id=%s", event_id)
                continue

            # Получить процессор для этого аккаунта
            # Создаём mock inbound для processor_for
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

            # Обработка по типу события
            if event.event_type == "message_received":
                if event.inbound is None:
                    continue

                # Фильтр «это сообщение от самого бизнеса» делаем уже после
                # подтверждения: запрос номера в Zernio занимает больше секунды,
                # и на нём задерживался ответ клиенту.
                background_tasks.add_task(
                    _handle_zernio_inbound, state, event, processor
                )

            elif event.event_type == "message_sent":
                # Исходящее от бизнеса/оператора — обновляем статус доставки
                if event.provider_message_id:
                    update_delivery_status(event.provider_message_id, "sent")

                if event.conversation_id:
                    from storage.db import fetchone, execute
                    row = fetchone(
                        "SELECT id FROM conversations WHERE zernio_conversation_id = ? AND client_key = ?",
                        (event.conversation_id, event.account_id),
                    )
                    if row:
                        conv_id = row["id"]
                        from storage import update_last_message_times, get_message_by_provider_id, add_message
                        update_last_message_times(conv_id, is_client=False)

                        raw_msg = (event.raw_payload.get("message") or {}) if isinstance(event.raw_payload, dict) else {}
                        event_text = str(raw_msg.get("text") or "").strip()

                        # Проверяем: это исходящее от самого бота или ответ живого оператора
                        is_bot_message = False
                        if event.provider_message_id:
                            existing = get_message_by_provider_id(event.provider_message_id)
                            if existing and existing.get("role") == "bot":
                                is_bot_message = True

                        last_msg = fetchone(
                            "SELECT id, role, text, provider_message_id FROM messages WHERE conversation_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                            (conv_id,),
                        )

                        if not is_bot_message and last_msg and last_msg["role"] == "bot":
                            # Сообщение бота: сопоставляем СТРОГО по совпадению текста
                            if event_text and last_msg["text"] == event_text:
                                is_bot_message = True
                                if event.provider_message_id and not last_msg["provider_message_id"]:
                                    execute(
                                        "UPDATE messages SET provider_message_id = ?, delivery_status = 'sent' WHERE id = ?",
                                        (event.provider_message_id, last_msg["id"]),
                                    )

                        if not is_bot_message:
                            # Проверяем, не было ли это сообщение оператора уже сохранено из админки панели
                            existing_human = None
                            if event.provider_message_id:
                                existing_human = get_message_by_provider_id(event.provider_message_id)

                            # Если сообщение отправлено из веб-панели (с временным idempotency ID), связываем его
                            if not existing_human and last_msg and last_msg["role"] == "human":
                                if event_text and last_msg["text"] == event_text:
                                    existing_human = last_msg
                                    if event.provider_message_id:
                                        execute(
                                            "UPDATE messages SET provider_message_id = ?, delivery_status = 'sent' WHERE id = ?",
                                            (event.provider_message_id, last_msg["id"]),
                                        )

                            if not existing_human and event_text:
                                # Сообщение отправлено человеком напрямую (из приложения WhatsApp / Inbox)
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
                                from storage import mark_first_human_reply
                                mark_first_human_reply(open_handoff["id"])

            elif event.event_type == "message_failed":
                if event.provider_message_id:
                    update_delivery_status(event.provider_message_id, "failed")

            elif event.event_type == "message_delivered":
                if event.provider_message_id:
                    update_delivery_status(event.provider_message_id, "delivered")

            elif event.event_type == "message_read":
                if event.provider_message_id:
                    update_delivery_status(event.provider_message_id, "read")

        logger.info(
            "Вебхук Zernio: ack через %.3f с | событий=%d",
            time.monotonic() - received_at, len(events),
        )
        return {"ok": True}


def _register_meta_webhook(app: FastAPI, settings: Settings, state: WebhookState) -> None:
    """Роуты вебхука Meta: GET-верификация подписки + POST событий."""

    @app.get("/webhooks/meta")
    async def meta_verify(request: Request):
        """Привязка вебхука в дашборде Meta: echo challenge при верном токене.

        Meta однократно дёргает наш URL GET-запросом hub.mode=subscribe —
        при совпадении META_VERIFY_TOKEN возвращаем hub.challenge как текст.
        """
        params = request.query_params
        if not verify_subscription(
            hub_mode=params.get(QUERY_MODE, ""),
            verify_token=params.get(QUERY_VERIFY_TOKEN, ""),
            expected_token=settings.meta_verify_token,
        ):
            logger.warning("Привязка вебхука Meta отклонена: verify_token не совпал")
            return PlainTextResponse("forbidden", status_code=403)
        # Возвращаем challenge ровно как прислал Meta, чистым текстом.
        return PlainTextResponse(params.get(QUERY_CHALLENGE, ""))

    @app.post("/webhooks/meta")
    async def meta_webhook(request: Request, background_tasks: BackgroundTasks):
        """Приём вебхука Meta: проверка X-Hub-Signature-256 -> ack 200 -> фон.

        Требование Meta то же, что у Zernio: ответить 2xx быстро, обработка —
        в фоновой задаче (LLM + отправка могут длиться дольше).
        Дедупликация через БД (storage.seen_events.check_and_add).
        """
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
            # Статусы доставки и прочие не-наши события — подтверждаем молча.
            return {"ok": True}

        for inbound in inbound_messages:
            # Дедупликация через БД (persistent across restarts)
            event_id = getattr(inbound, "message_id", None) or ""
            if not check_and_add(event_id):
                logger.debug("Дубликат вебхука Meta проигнорирован: message_id=%s", event_id)
                continue

            processor = await state.processor_for(inbound)
            if processor is None:
                # Неизвестный номер: не наш клиент — ack, чтобы Meta не ретраила.
                logger.warning(
                    "Событие для незарегистрированного номера: phone_number_id=%s "
                    "— подтверждено без обработки (клиента нет в clients/)",
                    inbound.phone_number_id or "-",
                )
                continue
            background_tasks.add_task(state.handle_event, inbound, processor)
        return {"ok": True}


def _register_telegram_webhook(app: FastAPI, settings: Settings, state: WebhookState) -> None:
    """Роут вебхука Telegram: POST /webhooks/telegram/{bot_id}."""

    @app.post("/webhooks/telegram/{bot_id}")
    async def telegram_webhook(bot_id: str, request: Request,
                               background_tasks: BackgroundTasks):
        """Приём апдейта Telegram: проверка секрета -> ack 200 -> обработка в фоне.

        Telegram ждёт быстрый 2xx и повторяет доставку при ошибке, поэтому
        отвечаем сразу, а сценарий (LLM + отправка) уводим в фоновую задачу.
        Дедуп — по update_id (Telegram тоже доставляет at-least-once). Клиент
        ищется по id бота из URL; секрет вебхука — из его конфига.
        Дедупликация через БД (storage.seen_events.check_and_add).
        """
        if state.multitenant:
            # Реестр подтягивает изменения yaml на лету; бота ищем по id из URL.
            await state.refresh_tenants()
            bundle = state.tenants.get(bot_id)
            if bundle is None:
                logger.warning("Telegram вебхук: неизвестный бот %s", bot_id)
                return JSONResponse(status_code=404, content={"ok": False})
            expected_secret = bundle.settings.telegram_webhook_secret
        else:
            expected_secret = settings.telegram_webhook_secret
            configured = bot_id_from_token(settings.telegram_bot_token)
            if configured and configured != bot_id:
                logger.warning("Telegram вебхук: bot_id %s не совпадает с токеном", bot_id)
                return JSONResponse(status_code=404, content={"ok": False})

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
            # Редактирование, статусы и прочие не-наши события — молча ack.
            return {"ok": True}

        for inbound in inbound_messages:
            # Дедупликация через БД (persistent across restarts). update_id у
            # Telegram счётчик ВНУТРИ бота: два бота с update_id=100 не должны
            # считаться дублем друг друга, поэтому ключ составной.
            raw_id = getattr(inbound, "message_id", None) or ""
            event_id = f"tg:{bot_id}:{raw_id}" if raw_id else ""
            if not check_and_add(event_id):
                logger.debug("Дубликат вебхука Telegram проигнорирован: update_id=%s", raw_id)
                continue

            processor = await state.processor_for(inbound)
            if processor is None:
                # Неизвестный бот: ack, чтобы Telegram не ретраил доставку.
                logger.warning(
                    "Telegram событие для незарегистрированного бота %s — "
                    "подтверждено без обработки", bot_id,
                )
                continue
            background_tasks.add_task(state.handle_event, inbound, processor)
        return {"ok": True}


def _register_telegram_owner_webhook(app: FastAPI, settings: Settings, state: WebhookState) -> None:
    """Вебхук Telegram-бота владельца: POST /webhooks/telegram-owner.

    Обрабатывает команды /start <code> для привязки чатов менеджеров.
    Проверяет секретный токен из настроек (TELEGRAM_OWNER_WEBHOOK_SECRET).
    """

    @app.post("/webhooks/telegram-owner")
    async def telegram_owner_webhook(request: Request, background_tasks: BackgroundTasks):
        """Приём апдейта от Telegram-бота владельца."""
        # Проверка секретного токена
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

        message = payload.get("message")
        if not isinstance(message, dict):
            return {"ok": True}

        text = str(message.get("text") or "").strip()
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id = str(chat.get("id") or "")

        # Обработка команд: /start <code> — привязка чата, /stop — отвязка.
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            if len(parts) == 2:
                code = parts[1].strip()
                from storage import (
                    MAX_BINDINGS_PER_CLIENT,
                    add_tg_binding,
                    complete_link_code,
                    count_bindings,
                    verify_link_code,
                )
                from whatsapp.telegram_client import TelegramClient

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
                # Код одноразовый: если его уже заняли — привязка не пройдёт.
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

        elif text.strip() == "/stop":
            # Отвязка чата от всех клиентов, где он привязан.
            from storage import get_tg_bindings, remove_tg_binding
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
    """Все привязки во всех клиентах — нужно для /stop (чат мог привязаться к разным)."""
    from storage.db import fetchall
    return [dict(row) for row in fetchall("SELECT id, client_key, chat_id FROM tg_bindings")]


async def _owner_bot_reply(settings: Settings, chat_id: str, text: str) -> None:
    """Ответ в чат менеджера от имени бота JAUAP (ошибки не роняют вебхук)."""
    from whatsapp.telegram_client import TelegramClient
    bot = TelegramClient(settings.telegram_owner_bot_token)
    try:
        await bot.send_text(chat_id, text)
    except Exception:
        logger.exception("Не удалось ответить в Telegram (%s)", chat_id)
    finally:
        await bot.close()


def main() -> None:
    # Логирование настраиваем до загрузки настроек, чтобы видеть её
    # предупреждения (некорректный номер владельца и т.п.).
    setup_logging(os.getenv("LOG_LEVEL", "INFO").strip().upper())
    settings = get_settings()
    app = create_app(settings)
    logger.info(
        "Вебхук: http://%s:%d/webhooks/%s",
        settings.app_host, settings.app_port, settings.messaging_provider,
    )
    uvicorn.run(app, host=settings.app_host, port=settings.app_port, log_config=None)


def _build_default_app() -> FastAPI:
    """Собирает приложение с настройками из .env — для `uvicorn main:app`.

    Локальный запуск `python main.py` идёт через main(); облачные платформы
    (Railway/Render/Fly) стартуют сервис командой `uvicorn main:app --port $PORT`
    и импортируют модуль — тогда build выполняется здесь, при первом обращении
    к атрибуту app. Если настройки не заданы, упадём с внятной ошибкой.
    """
    setup_logging(os.getenv("LOG_LEVEL", "INFO").strip().upper())
    return create_app(get_settings())


def __getattr__(name: str):
    """Ленивая сборка `app` по запросу `uvicorn main:app`.

    На уровне модуля объект не создаём намеренно: тесты и скрипты импортируют
    create_app и подсовывают свои настройки — без .env это падать не должно.
    """
    if name == "app":
        app = _build_default_app()
        globals()["app"] = app  # кэшируем: дальше атрибут находится напрямую
        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    main()
