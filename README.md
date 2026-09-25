# whatsapp-bot-test

WhatsApp-версия FAQ-бота [chat-bot-demo](../chat-bot-demo): тот же сценарий
(база знаний → LLM → `[HANDOFF]` → передача человеку), но канал — WhatsApp
через **Bird API** (bird.com).

## Что делает бот

1. Клиент пишет на бизнес-номер WhatsApp.
2. Если в тексте есть ключевое слово из `fallback_triggers` (жалоба, «позови
   менеджера»…) — сразу вежливый ответ и уведомление владельцу, без LLM.
3. Иначе вопрос уходит LLM вместе с базой знаний клиента (system prompt).
4. Если модель не уверена, она начинает ответ с `[HANDOFF]` → клиенту уходит
   вежливое «передаю ваш вопрос…» (на его языке ru/kk), а владельцу —
   WhatsApp-сообщение с исходным текстом или сводкой `ЗАПИСЬ: услуга — …, время — …`.
5. Таймаут/ошибка LLM — тот же fallback.
6. Фото/голосовые/документы — вежливая просьба написать текстом.

## Стек и структура

Python 3.11+, FastAPI + uvicorn, httpx, PyYAML, python-dotenv.

```
main.py                  FastAPI: вебхук Bird, проверка подписи, ack 200, фоновая обработка
config/
  settings.py            .env + YAML-конфиг клиента, fail-fast валидация
  client_config*.yaml    конфиги клиентов (бизнес, база знаний, триггеры, LLM)
handlers/
  message_handler.py     пайплайн: история → keyword → LLM → [HANDOFF] → ответ/передача
  owner_handler.py       уведомление владельцу через Bird
services/                LLM-клиент, fallback, определение языка (как в оригинале)
whatsapp/
  bird_client.py         POST /v1/whatsapp/messages, чанкинг 4096, retry 5xx/429
  webhook_security.py    проверка подписи вебхука (Standard Webhooks, HMAC-SHA256)
  webhook_payload.py     разбор события whatsapp.received
scripts/send_test.py     тестовая отправка сообщения через Bird API
tests/                   тесты (запуск: python tests/<имя>.py)
```

## Настройка

```bash
pip install -r requirements.txt
cp .env.example .env     # заполните ключи
python main.py
```

Переменные `.env`:

| Переменная | Описание |
|---|---|
| `BIRD_API_KEY` | API-ключ Bird (`bk_eu1_...` / `bk_us1_...`). Регион URL выводится из префикса ключа автоматически. |
| `BIRD_WEBHOOK_SECRET` | Секрет вебхука `whsec_...` — проверка подписи входящих. |
| `BIRD_API_URL` | (необязательно) по умолчанию `https://eu1.platform.bird.com` для ключа `bk_eu1_`. |
| `WHATSAPP_SENDER_NUMBER` | Бизнес-номер отправителя (E.164, с `+`). |
| `OWNER_WHATSAPP_NUMBER` | Номер владельца для уведомлений (перекрывает yaml). |
| `LLM_API_URL`, `LLM_API_KEY`, `LLM_MODEL` | OpenAI-совместимый LLM. |
| `CLIENT_CONFIG` | Какой yaml из `config/` грузить (по умолчанию `client_config.yaml`). |
| `APP_HOST`, `APP_PORT` | Сервер вебхука (по умолчанию `0.0.0.0:8000`). |

## Как работает

1. Bird POST'ит событие `whatsapp.received` на `/webhooks/bird`.
2. Проверяем подпись (HMAC-SHA256 от `webhook-id.webhook-timestamp.<raw body>`,
   ключ = base64-декод секрета без `whsec_`), свежесть ≤ 5 минут, дедуп по
   `webhook-id` — и сразу отвечаем `200` (у Bird 15 секунд на ack).
3. В фоне: keyword-fallback → LLM с историей диалога (8 сообщений, в памяти)
   → токен `[HANDOFF]` → ответ клиенту либо передача владельцу.
4. Ответ — `POST https://eu1.platform.bird.com/v1/whatsapp/messages`
   `{"to": "...", "from": "...", "text": {"body": "..."}}`; длинные ответы
   бьются по 4096 символов.
5. Уведомление владельцу — WhatsApp от бизнес-номера на
   `owner_whatsapp_phone` (yaml) / `OWNER_WHATSAPP_NUMBER` (.env).

## Подключение вебхука Bird

1. Запустите бота и откройте туннель для локального теста:
   ```bash
   ngrok http 8000
   ```
2. В дашборде Bird (**Developers → Webhooks → Create**) укажите:
   - URL: `https://<ваш-домен>/webhooks/bird`
   - Events: `whatsapp.received`
   - Скопируйте выданный секрет `whsec_...` в `.env` → `BIRD_WEBHOOK_SECRET`.
3. Или через API: `POST /v1/webhooks` с `{"url": "...", "events": ["whatsapp.received"]}`.
4. Напишите с телефона на бизнес-номер — в `logs/bot.log` появится обработка.

Проверка исходящих без вебхука:

```bash
python scripts/send_test.py --to +77770000000 --text "Тест"
```

(Ответ 202 = Bird принял; доставка асинхронная.)

## Тесты

```bash
python tests/test_webhook_security.py   # проверка подписи (16)
python tests/test_webhook_payload.py    # разбор событий (19)
python tests/test_pipeline.py           # пайплайн обработки (38)
python tests/test_bird_client.py        # Bird-клиент: чанкинг/ретраи (14)
python tests/test_webhook_server.py     # интеграция FastAPI (12)
```

Все тесты автономны: сеть не используется (LLM и Bird — стабы/моки).

## Деплой

- Railway/Render: start command `python main.py`, переменные окружения из
  `.env.example`; публичный URL сервиса укажите в вебхуке Bird.
- Вебхук должен отвечать 2xx за 15 секунд — сервер отвечает мгновенно,
  обработка идёт в фоновой задаче.
- Ретраи Bird: 5с → 5м → 30м → 2ч → 5ч → 10ч ×2 (at-least-once) — повторные
  доставки той же доставки отфильтровываются по `webhook-id`.

## Отличия от chat-bot-demo (Telegram)

- Вместо long polling — вебхук + проверка подписи Bird (Standard Webhooks).
- Владелец получает уведомления в WhatsApp (номер в `owner_whatsapp_phone`).
- `start` словом (не `/start`), нетекстовый контент → просьба написать текстом.
- Логика (история, `[HANDOFF]`, fallback, LLM-клиент, YAML-конфиги) перенесена
  без изменений.
