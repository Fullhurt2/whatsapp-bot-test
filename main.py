"""Точка входа: FastAPI-сервер, принимающий вебхуки Bird (WhatsApp).

Запуск: python main.py
Все настройки берутся из .env и конфига клиента из config/
(какой файл — переменная CLIENT_CONFIG, по умолчанию client_config.yaml).

Схема работы: Bird POST'ит входящие сообщения на /webhooks/bird. Мы
проверяем подпись (whsec-секрет, Standard Webhooks), сразу отвечаем 200
(у Bird лимит 15 секунд на ack) и обрабатываем сообщение в фоновой
задаче: ключевые слова -> LLM -> [HANDOFF] -> ответ клиенту и уведомление
владельцу через Bird.
"""

import json
import logging
import os
from collections import OrderedDict
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse

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


class WebhookState:
    """Общие объекты приложения: настройки, клиенты, обработчик, seen-кэш.

    Создаётся один раз на жизненный цикл приложения (lifespan).
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Один LLM-клиент и один WhatsApp-клиент на всё приложение
        # (переиспользуют HTTP-соединения). Провайдер задаёт MESSAGING_PROVIDER.
        self.llm_client = LLMClient(settings.llm_api_url, settings.llm_api_key, settings.llm)
        if settings.messaging_provider == "meta":
            self.sender = MetaWhatsAppClient(
                settings.whatsapp_access_token,
                settings.whatsapp_phone_number_id,
                graph_version=settings.meta_graph_version,
            )
        else:
            self.sender = BirdWhatsAppClient(
                settings.bird_api_key, settings.bird_api_url, settings.whatsapp_sender_number
            )
        self.processor = MessageProcessor(settings, self.llm_client, self.sender)
        self._seen_webhook_ids: OrderedDict[str, None] = OrderedDict()

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

    async def handle_event(self, inbound) -> None:
        """Обработка разобранного входящего сообщения (фоновая задача).

        Единая точка входа из вебхука: приветствие по слову «start»,
        нетекстовый контент — вежливая просьба написать текстом, остальное —
        полный сценарий MessageProcessor. Любое исключение гасится здесь:
        сбой обработки не должен ронять воркер.
        """
        try:
            if inbound.text.strip("/").casefold() == "start":
                await self.processor.handle_greeting(inbound.phone)
            elif inbound.text:
                await self.processor.handle_incoming(
                    inbound.phone, inbound.display_name, inbound.text
                )
            else:
                await self.processor.handle_non_text(
                    inbound.phone, inbound.display_name, inbound.content_kind
                )
        except Exception:
            # Падение обработки не должно крашить сервер: провайдер при не-2xx
            # начнёт ретраи, а ответ клиенту уже мог уйти.
            logger.exception("Ошибка при обработке вебхука (%s)", inbound.phone)

    async def shutdown(self) -> None:
        await self.llm_client.close()
        await self.sender.close()


def create_app(settings: Settings) -> FastAPI:
    """Собирает приложение: вебхук активного провайдера + healthcheck.

    settings передаётся явно — так тесты подсовывают тестовые настройки
    без .env (см. tests/), а при запуске python main.py их даёт get_settings().
    Провайдер выбирается переменной MESSAGING_PROVIDER: "meta" -> /webhooks/meta
    (Graph API), "bird" -> /webhooks/bird (Standard Webhooks).
    """
    logger.info(
        "Запуск бота | бизнес: %s | конфиг: %s | модель: %s | провайдер: %s",
        settings.business_name, settings.config_file, settings.llm.model,
        settings.messaging_provider,
    )
    state = WebhookState(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.state = state
        yield
        await state.shutdown()
        logger.info("Бот остановлен")

    app = FastAPI(title="whatsapp-bot-test", lifespan=lifespan)

    if settings.messaging_provider == "meta":
        _register_meta_webhook(app, settings, state)
    else:
        _register_bird_webhook(app, settings, state)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "business": settings.business_name}

    @app.get("/privacy")
    async def privacy():
        """Короткая памятка о данных клиента — открывается по ссылке из браузера.

        Ссылку удобно указать в профиле бизнеса WhatsApp: клиенты видят,
        какие данные собираются и как их удалить.
        """
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

    return app


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
            background_tasks.add_task(state.handle_event, inbound)
        return {"ok": True}


        for inbound in inbound_messages:
            if state.seen_before(inbound.message_id or ""):
                continue
            background_tasks.add_task(state.handle_event, inbound)
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
