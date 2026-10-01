"""Точка входа: FastAPI-сервер, принимающий вебхуки WhatsApp (Bird или Meta).

Запуск: python main.py
Все настройки берутся из .env; конфиги клиентов — из config/ (single-tenant,
CLIENT_CONFIG) или из папки clients/ (мультитенант, один файл на клиента,
имя файла = phone_number_id; включается переменной CLIENTS_DIR или наличием
непустой папки clients/).

Схема работы: провайдер POST'ит входящие сообщения на /webhooks/{bird|meta}.
Мы проверяем подпись, сразу отвечаем 200 (у провайдера лимит 15 секунд на
ack) и обрабатываем сообщение в фоновой задаче: ключевые слова -> LLM ->
[HANDOFF] -> ответ клиенту и уведомление владельцу.
"""

import asyncio
import hmac
import json
import logging
import os
from collections import OrderedDict
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from admin.api import register_admin_api
from config.clients import ClientRegistry
from config.settings import Settings, get_settings
from handlers.message_handler import MessageProcessor
from services.llm_client import LLMClient
from whatsapp.bird_client import BirdWhatsAppClient
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
from whatsapp.webhook_payload import parse_incoming_event
from whatsapp.webhook_security import (
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    HEADER_WEBHOOK_ID,
    verify_signature,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "bot.log"

# Дедупликация доставок Bird: они приходят at-least-once, при ретрае у
# доставки тот же webhook-id — второй раз отвечать клиенту нельзя.
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
    """Клиент отправки под провайдера клиента (meta / telegram / bird).

    Провайдер определяет Settings клиента: у WhatsApp-клиентов reestr ставит
    messaging_provider="meta", у Telegram — "telegram". Все клиенты реализуют
    один интерфейс send_text(to, text)/close(), поэтому обработчик и
    уведомления владельцу от транспорта не зависят.
    """
    if settings.messaging_provider == "telegram":
        return TelegramClient(settings.telegram_bot_token)
    if settings.messaging_provider == "bird":
        return BirdWhatsAppClient(
            settings.bird_api_key, settings.bird_api_url, settings.whatsapp_sender_number
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
        self._seen_webhook_ids: OrderedDict[str, None] = OrderedDict()
        self._reload_lock = asyncio.Lock()
        # Мультитенант обслуживает оба транспорта: WhatsApp-клиенты (meta) и
        # Telegram-клиенты (telegram) живут в одном реестре clients/.
        self.multitenant = bool(settings.clients_dir) and settings.messaging_provider in (
            "meta", "telegram",
        )

        if not self.multitenant:
            # Single-tenant: один LLM-клиент и один транспорт на всё приложение.
            # Провайдер задаёт MESSAGING_PROVIDER (bird / meta / telegram).
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
        """Процессор клиента для входящего сообщения по его phone_number_id.

        В single-tenant процессор всегда один. В мультитенанте перед поиском
        сверяется снапшот папки clients/ (hot-reload), затем клиент ищется
        по inbound.phone_number_id; None — номер не зарегистрирован.
        """
        if not self.multitenant:
            return self.processor
        await self.refresh_tenants()
        bundle = self.tenants.get(inbound.phone_number_id)
        return bundle.processor if bundle else None

    # --- дедупликация доставок ---------------------------------------------------

    def seen_before(self, webhook_id: str) -> bool:
        """True, если доставку с таким id уже обрабатывали.

        Регистрирует новую доставку и вытесняет самые старые id — кэш
        ограничен, чтобы не рос бесконечно. Для Meta сюда идёт id сообщения
        (wamid...), для Bird — заголовок webhook-id.
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
        try:
            if inbound.text.strip("/").casefold() == "start":
                await processor.handle_greeting(inbound.phone)
            elif inbound.text:
                await processor.handle_incoming(
                    inbound.phone, inbound.display_name, inbound.text
                )
            else:
                await processor.handle_non_text(
                    inbound.phone, inbound.display_name, inbound.content_kind
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
    (Graph API), "bird" -> /webhooks/bird (Standard Webhooks). Мультитенант
    включён, если settings.clients_dir задан: тогда маршрутизация идёт по
    phone_number_id из clients/ (sender_factory — точка подмены для тестов).
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
    state = WebhookState(settings, sender_factory)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.state = state
        yield
        await state.shutdown()
        logger.info("Бот остановлен")

    app = FastAPI(title="whatsapp-bot-test", lifespan=lifespan)

    if state.multitenant:
        # В мультитенанте доступны оба транспорта: WhatsApp-вебхук поднимаем
        # только если заданы Meta-секреты (иначе деплой чисто для Telegram),
        # Telegram-вебхук — всегда (у каждого бота свой токен).
        if settings.meta_app_secret and settings.meta_verify_token:
            _register_meta_webhook(app, settings, state)
        else:
            logger.warning(
                "WhatsApp-вебхук не зарегистрирован: не заданы META_APP_SECRET/"
                "META_VERIFY_TOKEN — обслуживаются только Telegram-клиенты"
            )
        _register_telegram_webhook(app, settings, state)
    elif settings.messaging_provider == "meta":
        _register_meta_webhook(app, settings, state)
    elif settings.messaging_provider == "telegram":
        _register_telegram_webhook(app, settings, state)
    else:
        _register_bird_webhook(app, settings, state)

    # Админ-API и панель /admin: только в мультитенанте и с заданным
    # ADMIN_TOKEN (иначе роуты не существуют — см. admin/api.py).
    if state.multitenant:
        register_admin_api(app, settings, state)

    @app.get("/healthz")
    async def healthz():
        if state.multitenant:
            return {"status": "ok", "clients": len(state.tenants)}
        return {"status": "ok", "business": settings.business_name}

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


def _register_bird_webhook(app: FastAPI, settings: Settings, state: WebhookState) -> None:
    @app.post("/webhooks/bird")
    async def bird_webhook(request: Request, background_tasks: BackgroundTasks):
        """Приём вебхука Bird: проверка подписи -> ack 200 -> обработка в фоне.

        Bird ждёт 2xx за 15 секунд, а обработка (LLM + отправка) может
        длиться дольше — поэтому отвечаем 200 сразу, а полный сценарий
        уходит в фоновую задачу.
        """
        raw_body = await request.body()
        signature_ok = verify_signature(
            secret=settings.bird_webhook_secret,
            webhook_id=request.headers.get(HEADER_WEBHOOK_ID, ""),
            timestamp=request.headers.get(HEADER_TIMESTAMP, ""),
            signature_header=request.headers.get(HEADER_SIGNATURE, ""),
            raw_body=raw_body,
        )
        if not signature_ok:
            # Чужой запрос: Bird такое не пришлёт, отвечает 401 без ретрая.
            logger.warning("Вебхук отклонён: подпись не прошла проверку")
            return JSONResponse(status_code=401, content={"error": "invalid signature"})

        webhook_id = request.headers.get(HEADER_WEBHOOK_ID, "")
        if state.seen_before(webhook_id):
            return {"ok": True, "deduplicated": True}

        try:
            payload = json.loads(raw_body)
        except ValueError:
            # Битый JSON от Bird бессмысленно ретраить — ack, чтобы не было ретрая.
            logger.warning("Вебхук с невалидным JSON: %d байт", len(raw_body))
            return {"ok": True}

        inbound = parse_incoming_event(payload)
        if inbound is None:
            # Не наше событие (статусы доставки и т.п.) — молча подтверждаем.
            return {"ok": True}

        background_tasks.add_task(state.handle_event, inbound)
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

        Требование Meta то же, что у Bird: ответить 2xx быстро, обработка —
        в фоновой задаче (LLM + отправка могут длиться дольше).
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
            if state.seen_before(inbound.message_id or ""):
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
            if state.seen_before(inbound.message_id):
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
