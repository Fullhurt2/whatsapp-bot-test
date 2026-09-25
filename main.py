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
from fastapi.responses import JSONResponse

from config.settings import Settings, get_settings
from handlers.message_handler import MessageProcessor
from services.llm_client import LLMClient
from whatsapp.bird_client import BirdWhatsAppClient
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
        # Один LLM-клиент и один Bird-клиент на всё приложение
        # (переиспользуют HTTP-соединения).
        self.llm_client = LLMClient(settings.llm_api_url, settings.llm_api_key, settings.llm)
        self.bird = BirdWhatsAppClient(
            settings.bird_api_key, settings.bird_api_url, settings.whatsapp_sender_number
        )
        self.processor = MessageProcessor(settings, self.llm_client, self.bird)
        self._seen_webhook_ids: OrderedDict[str, None] = OrderedDict()

    # --- дедупликация доставок ---------------------------------------------------

    def seen_before(self, webhook_id: str) -> bool:
        """True, если доставку с таким webhook-id уже обрабатывали.

        Регистрирует новую доставку и вытесняет самые старые id — кэш
        ограничен, чтобы не рос бесконечно.
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
            # Падение обработки не должно крашить сервер: Bird при не-2xx
            # начнёт ретраи, а ответ клиенту уже мог уйти.
            logger.exception("Ошибка при обработке вебхука (%s)", inbound.phone)

    async def shutdown(self) -> None:
        await self.llm_client.close()
        await self.bird.close()


def create_app(settings: Settings) -> FastAPI:
    """Собирает приложение: вебхук Bird + healthcheck.

    settings передаётся явно — так тесты подсовывают тестовые настройки
    без .env (см. tests/), а при запуске python main.py их даёт get_settings().
    """
    logger.info(
        "Запуск бота | бизнес: %s | конфиг: %s | модель: %s | Bird: %s",
        settings.business_name, settings.config_file, settings.llm.model,
        settings.bird_api_url,
    )
    state = WebhookState(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.state = state
        yield
        await state.shutdown()
        logger.info("Бот остановлен")

    app = FastAPI(title="whatsapp-bot-test", lifespan=lifespan)

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

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "business": settings.business_name}

    return app


def main() -> None:
    # Логирование настраиваем до загрузки настроек, чтобы видеть её
    # предупреждения (некорректный номер владельца и т.п.).
    setup_logging(os.getenv("LOG_LEVEL", "INFO").strip().upper())
    settings = get_settings()
    app = create_app(settings)
    logger.info("Вебхук: http://0.0.0.0:%d/webhooks/bird", settings.app_port)
    uvicorn.run(app, host=settings.app_host, port=settings.app_port, log_config=None)


if __name__ == "__main__":
    main()
