# whatsapp-bot-test

WhatsApp-версия FAQ-бота [chat-bot-demo](../chat-bot-demo): тот же сценарий
(база знаний → LLM → `[HANDOFF]` → передача человеку). Транспорт переключается
переменной `MESSAGING_PROVIDER`:

- **`meta`** (рекомендуется) — WhatsApp Cloud API напрямую от Meta. Ответы
  клиентам в 24-часовом окне бесплатны; платно только шаблонные рассылки,
  боту они не нужны.
- **`bird`** — Bird API (bird.com). Нужен пополненный кошелёк Bird.

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
main.py                  FastAPI: вебхук провайдера, проверка подписи, ack 200, фоновая обработка
config/
  settings.py            .env + YAML-конфиг клиента, fail-fast валидация
  client_config*.yaml    конфиги клиентов (бизнес, база знаний, триггеры, LLM)
handlers/
  message_handler.py     пайплайн: история → keyword → LLM → [HANDOFF] → ответ/передача
  owner_handler.py       уведомление владельцу через активного провайдера
services/                LLM-клиент, fallback, определение языка (как в оригинале)
whatsapp/
  errors.py              общие исключения MessagingError (для обоих провайдеров)
  meta_client.py         POST /{phone_number_id}/messages (Graph API), чанкинг 4096, retry
  meta_security.py       проверка X-Hub-Signature-256 + GET-верификация подписки
  meta_payload.py        разбор entry/changes/messages
  bird_client.py         POST /v1/whatsapp/messages, чанкинг 4096, retry 5xx/429
  webhook_security.py    проверка подписи Bird (Standard Webhooks, HMAC-SHA256)
  webhook_payload.py     разбор события whatsapp.received
scripts/send_test.py     тестовая отправка сообщения через активного провайдера
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
| `MESSAGING_PROVIDER` | `meta` (WhatsApp Cloud API) или `bird`. Метa — по умолчанию в примере. |
| `WHATSAPP_ACCESS_TOKEN` | (meta) токен System User с правами `whatsapp_business_messaging`. |
| `WHATSAPP_PHONE_NUMBER_ID` | (meta) ID бизнес-номера отправителя из дашборда Meta. |
| `META_APP_SECRET` | (meta) App Secret приложения — проверка `X-Hub-Signature-256`. |
| `META_VERIFY_TOKEN` | (meta) строка для привязки вебхука в дашборде Meta. |
| `META_GRAPH_VERSION` | (meta, необязательно) версия Graph API, по умолчанию `v21.0`. |
| `BIRD_API_KEY` | (bird) API-ключ Bird (`bk_eu1_...`). Регион выводится из префикса. |
| `BIRD_WEBHOOK_SECRET` | (bird) секрет вебхука `whsec_...`. |
| `BIRD_API_URL` | (bird, необязательно) по умолчанию `https://eu1.platform.bird.com`. |
| `WHATSAPP_SENDER_NUMBER` | (bird) бизнес-номер отправителя (E.164, с `+`). |
| `OWNER_WHATSAPP_NUMBER` | Номер владельца для уведомлений (перекрывает yaml). |
| `LLM_API_URL`, `LLM_API_KEY`, `LLM_MODEL` | OpenAI-совместимый LLM. |
| `CLIENT_CONFIG` | Какой yaml из `config/` грузить (по умолчанию `client_config.yaml`). |
| `APP_HOST`, `APP_PORT` | Сервер вебхука (по умолчанию `0.0.0.0:8000`; на Railway порт берётся из `PORT`). |

Бот стартует только тогда, когда заполнены переменные активного провайдера —
недостающие перечисляются прямо в ошибке запуска (fail-fast, как в оригинале).

## Как работает

1. Провайдер POST'ит входящее сообщение на `/webhooks/meta` (meta) или
   `/webhooks/bird` (bird).
2. Проверяем подпись: Meta — `X-Hub-Signature-256` (HMAC-SHA256 от raw body
   с App Secret), Bird — Standard Webhooks (`webhook-id.webhook-timestamp.<raw
   body>`, ключ из `whsec_`). Дедуп по id сообщения/доставки — и сразу `200`
   (у обоих провайдеров ~15 секунд на ack).
3. В фоне: keyword-fallback → LLM с историей диалога (8 сообщений, в памяти)
   → токен `[HANDOFF]` → ответ клиенту либо передача владельцу.
4. Ответ — через активного провайдера (Meta: `POST graph.facebook.com/v21.0/
   {phone_number_id}/messages`, Bird: `POST /v1/whatsapp/messages`); длинные
   ответы бьются по 4096 символов.
5. Уведомление владельцу — WhatsApp от бизнес-номера на
   `owner_whatsapp_phone` (yaml) / `OWNER_WHATSAPP_NUMBER` (.env).

## Подключение вебхука Meta (провайдер `meta`)

1. developers.facebook.com → My Apps → Create App (Business) → добавить продукт **WhatsApp**.
2. Из приложения возьмите `Phone Number ID` (WhatsApp → API Setup) и
   `App Secret` (App settings → Basic) → в `.env`.
3. Business Settings → Users → System users → создайте пользователя, дайте
   права `whatsapp_business_messaging` + `whatsapp_business_management`,
   сгенерируйте токен → `WHATSAPP_ACCESS_TOKEN`.
4. В настройках приложения (WhatsApp → Configuration → Webhooks):
   - Callback URL: `https://<ваш-домен>/webhooks/meta`
   - Verify token: то же значение, что в `META_VERIFY_TOKEN`
   - Подпишитесь на поле `messages`.
   Meta дёрнет GET-запрос — сервер сам вернёт `hub.challenge` (роут уже есть).
5. Напишите с телефона на номер — в `logs/bot.log` появится обработка.

В dev-режиме писать боту могут только до 5 «проверенных» номеров
(WhatsApp → API Setup → To). Для реальных клиентов подключите свой номер
и пройдите верификацию бизнеса.

## Подключение вебхука Bird (провайдер bird)

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

(Провайдер берётся из `MESSAGING_PROVIDER`; у Bird успех = 202, у Meta = 200.)

## Тесты

```bash
python tests/test_webhook_security.py   # подпись Bird (16)
python tests/test_webhook_payload.py    # разбор событий Bird (19)
python tests/test_pipeline.py           # пайплайн обработки (38)
python tests/test_bird_client.py        # Bird-клиент: чанкинг/ретраи (14)
python tests/test_webhook_server.py     # интеграция FastAPI, Bird-роут (12)
python tests/test_meta_security.py      # подпись Meta + верификация (13)
python tests/test_meta_payload.py       # разбор событий Meta (18)
python tests/test_meta_client.py        # Meta-клиент: чанкинг/ретраи (14)
python tests/test_meta_webhook.py       # интеграция FastAPI, Meta-роуты (11)
```

Все тесты автономны: сеть не используется (LLM и провайдеры — стабы/моки).

## Деплой на Railway (Render/Fly аналогично)

1. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`. Модуль отдаёт
   `app` лениво, так что `uvicorn main:app` работает.
2. Переменные окружения в дашборде: активного провайдера (см. `.env.example`)
   + `LLM_API_URL`, `LLM_API_KEY`, `CLIENT_CONFIG`. `PORT` Railway подставляет
   сам — он имеет приоритет над `APP_PORT`; `APP_HOST` уже `0.0.0.0`.
3. В Settings → Networking задайте порт, который слушает приложение
   (PORT из окружения), и подключите домен.
4. Вебхук провайдера укажите на публичный домен:
   - Meta: `https://<домен>/webhooks/meta` (Callback URL в настройках приложения,
     Verify token = `META_VERIFY_TOKEN`, подписка на поле `messages`);
   - Bird: `https://<домен>/webhooks/bird` (events: `whatsapp.received`).

## Отличия от chat-bot-demo (Telegram)

- Вместо long polling — вебхук с проверкой подписи (Meta или Bird),
  переключение провайдера через `MESSAGING_PROVIDER` без изменения логики.
- Владелец получает уведомления в WhatsApp (номер в `owner_whatsapp_phone`).
- `start` словом (не `/start`), нетекстовый контент → просьба написать текстом.
- Логика (история, `[HANDOFF]`, fallback, LLM-клиент, YAML-конфиги) перенесена
  без изменений.
