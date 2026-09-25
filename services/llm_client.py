"""Обёртка над LLM API (OpenAI-совместимый формат: OpenRouter, Inception и т.п.).

Задача модуля — один асинхронный вызов chat.completions с таймаутом
и понятными исключениями, чтобы хендлеры не зависели от деталей HTTP.
"""

import asyncio
import logging

import httpx

from config.settings import LLMParams

logger = logging.getLogger(__name__)

# Лимит токенов для повторного запроса, когда reasoning-модель израсходовала
# основной max_tokens и вернула пустой content (см. chat()).
EMPTY_RESPONSE_RETRY_LIMIT = 5000

# Пауза перед единственным повтором при разовом сбое провайдера (5xx).
SERVER_ERROR_RETRY_DELAY_SEC = 1.0


class LLMError(Exception):
    """Любая ошибка вызова LLM (сеть, HTTP-ошибка, невалидный ответ)."""


class LLMTimeout(LLMError):
    """LLM не ответил за отведённое время — трактуется как fallback."""


class LLMClient:
    """Асинхронный клиент чат-комплишенов. Создаётся один раз при старте бота.

    Адаптация к требованиям конкретной модели: при отказе API (400) клиент
    один раз повторяет запрос без параметра, на который пожаловался провайдер.
    Так смена модели через конфиг не ломает бот на первом же запросе:
      - max_tokens -> max_completion_tokens (OpenAI gpt-6-*, o-серия);
      - запрос без temperature (модели, принимающие только дефолт);
      - запрос без reasoning_effort (если модель его не принимает).
    """

    def __init__(self, api_url: str, api_key: str, params: LLMParams) -> None:
        self._params = params
        self._endpoint = api_url.rstrip("/") + "/chat/completions"
        # Флаги адаптации «прилипают»: параметр, отклонённый провайдером,
        # больше не отправляется в рамках этого клиента.
        self._use_max_completion_tokens = False
        self._omit_temperature = False
        self._omit_reasoning_effort = False
        # Общий таймаут = timeout_seconds из конфига; на установку соединения даём 10с.
        # read-таймаут — главная защита от «зависшего» ответа модели.
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(params.timeout_seconds, connect=10.0),
        )

    async def chat(
        self,
        system_prompt: str,
        user_message: str,
        history: list[dict] | None = None,
    ) -> str:
        """Задаёт вопрос LLM и возвращает текст ответа.

        history — предыдущие сообщения диалога в формате
        [{"role": "user"|"assistant", "content": "..."}]; передаются как есть,
        перед текущим user-сообщением.

        Бросает LLMTimeout при таймауте и LLMError при прочих сбоях —
        вызывающий код превращает оба в fallback-сценарий.
        """
        payload = self._build_payload(system_prompt, user_message, history)
        response = await self._send(payload)

        # Модель не приняла какой-то параметр — убираем его и повторяем.
        for _ in range(3):
            if response.status_code != 400:
                break
            adapted = self._adapt_for_rejection(payload, response)
            if adapted is None:
                break  # нечего адаптировать — вернём ошибку провайдера как есть
            payload = adapted
            response = await self._send(payload)

        # Разовые сбои провайдера (502/503/504) встречаются и проходят:
        # делаем одну короткую повторную попытку, прежде чем звать владельца.
        if response.status_code >= 500:
            logger.warning(
                "LLM API вернул статус %d — повторяю запрос через %d сек",
                response.status_code,
                SERVER_ERROR_RETRY_DELAY_SEC,
            )
            await asyncio.sleep(SERVER_ERROR_RETRY_DELAY_SEC)
            response = await self._send(payload)

        choice = self._parse_choice(response)

        # У reasoning-моделей reasoning может израсходовать весь max_tokens,
        # и тогда content приходит пустым. Повторяем один раз с запасом,
        # прежде чем отдавать fallback пользователю.
        if self._content_of(choice) == "":
            logger.info(
                "Пустой ответ (content=null) — повторяю запрос с лимитом %d токенов",
                EMPTY_RESPONSE_RETRY_LIMIT,
            )
            payload[self._limit_key()] = EMPTY_RESPONSE_RETRY_LIMIT
            response = await self._send(payload)
            choice = self._parse_choice(response)
            if self._content_of(choice) == "":
                raise LLMError(
                    "LLM вернула пустой ответ (content=null) даже после повтора "
                    f"с лимитом {EMPTY_RESPONSE_RETRY_LIMIT} токенов — "
                    "вероятно, reasoning исчерпал max_tokens"
                )

        return str(choice["message"]["content"]).strip()

    async def close(self) -> None:
        await self._client.aclose()

    # --- внутреннее -----------------------------------------------------------

    def _build_payload(self, system_prompt: str, user_message: str,
                       history: list[dict] | None = None) -> dict:
        """Тело запроса с учётом параметров, уже отклонённых провайдером.

        history — предыдущие сообщения диалога (без system prompt),
        вставляются между system-инструкцией и текущим вопросом.
        """
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": user_message})

        payload: dict = {
            "model": self._params.model,
            "messages": messages,
        }
        if not self._omit_temperature:
            payload["temperature"] = self._params.temperature
        if self._params.reasoning_effort and not self._omit_reasoning_effort:
            payload["reasoning_effort"] = self._params.reasoning_effort
        payload[self._limit_key()] = self._params.max_tokens
        return payload

    def _limit_key(self) -> str:
        """Имя параметра лимита токенов зависит от провайдера/модели."""
        return "max_completion_tokens" if self._use_max_completion_tokens else "max_tokens"

    async def _send(self, payload: dict) -> httpx.Response:
        try:
            return await self._client.post(self._endpoint, json=payload)
        except httpx.TimeoutException as exc:
            raise LLMTimeout(f"LLM не ответил за {self._params.timeout_seconds} сек") from exc
        except httpx.HTTPError as exc:  # сетевые сбои: DNS, обрыв соединения и т.п.
            raise LLMError(f"Сетевая ошибка при вызове LLM: {exc}") from exc

    def _adapt_for_rejection(self, payload: dict, response: httpx.Response) -> dict | None:
        """Если 400-ошибка связана с неподдерживаемым параметром — убирает его.

        Возвращает обновлённый payload или None, если убирать нечего.
        """
        text = response.text
        # OpenAI gpt-6-* и o-серия принимают только max_completion_tokens.
        if not self._use_max_completion_tokens and "max_completion_tokens" in text:
            logger.info("Провайдер требует max_completion_tokens вместо max_tokens — переключаюсь")
            self._use_max_completion_tokens = True
            payload.pop("max_tokens", None)
            payload[self._limit_key()] = self._params.max_tokens
            return payload
        # Часть моделей принимает только дефолтную temperature (часто = 1).
        if not self._omit_temperature and "temperature" in text:
            logger.warning("Провайдер не принимает temperature — повторяю запрос без него")
            self._omit_temperature = True
            payload.pop("temperature", None)
            return payload
        # Модель не принимает reasoning_effort — убираем, работаем на дефолте.
        if (
            self._params.reasoning_effort
            and not self._omit_reasoning_effort
            and "reasoning_effort" in text
        ):
            logger.warning(
                "Провайдер отклонил reasoning_effort=%r — повторяю запрос без него",
                self._params.reasoning_effort,
            )
            self._omit_reasoning_effort = True
            payload.pop("reasoning_effort", None)
            return payload
        return None

    def _parse_choice(self, response: httpx.Response) -> dict:
        """Проверяет статус/формат и возвращает choice; логирует диагностику."""
        if response.status_code != 200:
            # Короткий фрагмент тела в сообщении ошибки: провайдер объясняет
            # в нём, какой параметр невалиден.
            raise LLMError(f"LLM API вернул статус {response.status_code}: {response.text[:200]}")

        try:
            data = response.json()
            choice = data["choices"][0]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("Неожиданный формат ответа LLM API") from exc

        # Диагностика расхода токенов: если reasoning съедает max_tokens,
        # content приходит пустым — по этим полям это сразу видно в логах.
        logger.info(
            "LLM ответ | finish_reason=%s | usage=%s",
            choice.get("finish_reason"),
            data.get("usage"),
        )
        return choice

    @staticmethod
    def _content_of(choice: dict) -> str:
        """Достаёт текст ответа; у reasoning-моделей content бывает null."""
        content = (choice.get("message") or {}).get("content")
        return str(content).strip() if content else ""
