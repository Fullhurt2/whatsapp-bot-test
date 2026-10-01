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
main.py                  FastAPI: вебхук провайдера, проверка подписи, ack 200, фоновая обработка;
                         мультитенант: маршрутизация по ключу клиента, hot-reload реестра
admin/
  api.py                 админ-API: CRUD конфигов clients/, валидация, бэкапы, аудит,
                         setWebhook Telegram при сохранении клиента
  static/index.html      страница панели /admin (админ — все клиенты, клиент — свой)
config/
  settings.py            .env + YAML-конфиг клиента, fail-fast валидация
  clients.py             реестр клиентов clients/*.yaml (мультитенант, hot-reload, provider wa|tg)
clients/
  _example.yaml          образец клиентского yaml (в git, без секретов)
  <key>.yaml             один файл на клиента: phone_number_id (wa) или bot_id (tg)
handlers/
  message_handler.py     пайплайн: история → keyword → LLM → [HANDOFF] → ответ/передача
  owner_handler.py       уведомление владельцу через активного провайдера
services/                LLM-клиент, fallback, определение языка (как в оригинале)
whatsapp/
  errors.py              общие исключения MessagingError (для всех провайдеров)
  meta_client.py         POST /{phone_number_id}/messages (Graph API), чанкинг 4096, retry
  meta_security.py       проверка X-Hub-Signature-256 + GET-верификация подписки
  meta_payload.py        разбор entry/changes/messages (+metadata.phone_number_id)
  bird_client.py         POST /v1/whatsapp/messages, чанкинг 4096, retry 5xx/429
  webhook_security.py    проверка подписи Bird (Standard Webhooks, HMAC-SHA256)
  webhook_payload.py     разбор события whatsapp.received
  telegram_client.py     Bot API: sendMessage/setWebhook/getMe, чанкинг 4096, retry
  telegram_payload.py    разбор update (message) → InboundMessage
  telegram_token.py      разбор токена бота: id бота = ключ клиента
scripts/send_test.py     тестовая отправка сообщения через активного провайдера
wa_onboard.py            автономный онбординг клиента (2FA, подписка WABA, печать .env)
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
| `MESSAGING_PROVIDER` | `meta` (WhatsApp Cloud API), `telegram` или `bird`. В мультитенанте `meta`/`telegram` включают реестр `clients/` и обслуживают оба транспорта одновременно. |
| `WHATSAPP_ACCESS_TOKEN` | (meta) токен System User с правами `whatsapp_business_messaging`. |
| `WHATSAPP_PHONE_NUMBER_ID` | (meta) ID бизнес-номера отправителя из дашборда Meta. |
| `META_APP_SECRET` | (meta) App Secret приложения — проверка `X-Hub-Signature-256`. |
| `META_VERIFY_TOKEN` | (meta) строка для привязки вебхука в дашборде Meta. |
| `META_GRAPH_VERSION` | (meta, необязательно) версия Graph API, по умолчанию `v21.0`. |
| `TELEGRAM_BOT_TOKEN` | (telegram, single-tenant) токен бота от @BotFather. В мультитенанте — у каждого клиента в yaml. |
| `TELEGRAM_WEBHOOK_SECRET` | (telegram, single-tenant) секрет вебхука; в мультитенанте генерируется автоматически. |
| `PUBLIC_BASE_URL` | Публичный адрес сервиса — база для `setWebhook` Telegram-клиентов. Не задан → вебхук привязывается вручную. |
| `BIRD_API_KEY` | (bird) API-ключ Bird (`bk_eu1_...`). Регион выводится из префикса. |
| `BIRD_WEBHOOK_SECRET` | (bird) секрет вебхука `whsec_...`. |
| `BIRD_API_URL` | (bird, необязательно) по умолчанию `https://eu1.platform.bird.com`. |
| `WHATSAPP_SENDER_NUMBER` | (bird) бизнес-номер отправителя (E.164, с `+`). |
| `OWNER_WHATSAPP_NUMBER` | Номер владельца для уведомлений (перекрывает yaml; только single-tenant). |
| `LLM_API_URL`, `LLM_API_KEY`, `LLM_MODEL` | OpenAI-совместимый LLM. |
| `CLIENT_CONFIG` | (single-tenant) какой yaml из `config/` грузить (по умолчанию `client_config.yaml`). |
| `CLIENTS_DIR` | (необязательно) папка реестра клиентов для мультитенанта, по умолчанию `clients/`. |
| `ADMIN_TOKEN` | (необязательно, мультитенант) токен админ-API и панели `/admin`, от 32 случайных символов. Без него админ-роуты отключены. |
| `APP_HOST`, `APP_PORT` | Сервер вебхука (по умолчанию `0.0.0.0:8000`; на Railway порт берётся из `PORT`). |

Бот стартует только тогда, когда заполнены переменные активного провайдера —
недостающие перечисляются прямо в ошибке запуска (fail-fast, как в оригинале).

## HTTP-эндпоинты

- `GET /healthz` — проверка живости: `{"status": "ok", "business": ...}` в
  single-tenant, `{"status": "ok", "clients": N}` в мультитенанте.
- `GET /privacy` — памятка о данных клиента (что хранится, как удалить);
  удобно указать её в профиле бизнеса WhatsApp.
- `POST /webhooks/meta` / `/webhooks/bird` — входящие вебхуки провайдера.
- `POST /webhooks/telegram/{bot_id}` — входящие апдейты Telegram-бота
  (проверка `X-Telegram-Bot-Api-Secret-Token`).

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
5. Уведомление владельцу — тем же транспортом, что и ответы: WhatsApp на
   `owner_whatsapp_phone` (yaml) / `OWNER_WHATSAPP_NUMBER` (.env), Telegram —
   в `owner_telegram_chat_id`.

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

## Telegram-боты (provider `tg`)

В мультитенанте WhatsApp- и Telegram-клиенты живут вместе: платформа задаётся
у каждого клиента полем `provider` (`wa` по умолчанию, `tg`). В панели `/admin`
она выбирается переключателем при создании клиента и потом не меняется — смена
транспорта означает другого клиента.

Ключ Telegram-клиента — id бота (цифры до `:` в токене), имя файла
`clients/<bot_id>.yaml`. Обязательные поля: `provider: tg`,
`telegram_bot_token` (от @BotFather), плюс обычные `business_name` и
`knowledge_base`. Уведомления владельцу идут в `owner_telegram_chat_id`
(числовой chat id; узнать у бота @userinfobot).

Приём сообщений — вебхук, как у WhatsApp. При сохранении клиента панель сама
вызывает `setWebhook` на `PUBLIC_BASE_URL/webhooks/telegram/<bot_id>` и
регистрирует `telegram_webhook_secret`, по которому проверяются входящие
(заголовок `X-Telegram-Bot-Api-Secret-Token`). Если `PUBLIC_BASE_URL` не
задан, панель покажет готовый адрес, а вебхук привязывается вручную:

```bash
curl "https://api.telegram.org/bot<ТОКЕН>/setWebhook" \
  -d "url=https://<ваш-домен>/webhooks/telegram/<bot_id>" \
  -d "secret_token=<telegram_webhook_secret из yaml>" \
  -d 'allowed_updates=["message"]'
```

Включить мультитенант: `MESSAGING_PROVIDER=meta` (или `telegram`) и папка
`clients/` хотя бы с одним клиентом. Meta-секреты для Telegram-клиентов не
нужны: если `META_APP_SECRET`/`META_VERIFY_TOKEN` пусты, WhatsApp-вебхук просто
не поднимается, а Telegram продолжает работать.

Single-tenant Telegram (один бот): `MESSAGING_PROVIDER=telegram`,
`TELEGRAM_BOT_TOKEN`, `PUBLIC_BASE_URL`, бизнес-поля в `CLIENT_CONFIG`.

## Тесты

```bash
python tests/test_webhook_security.py   # подпись Bird (16)
python tests/test_webhook_payload.py    # разбор событий Bird (19)
python tests/test_pipeline.py           # пайплайн обработки (38)
python tests/test_bird_client.py        # Bird-клиент: чанкинг/ретраи (14)
python tests/test_webhook_server.py     # интеграция FastAPI, Bird-роут (13)
python tests/test_meta_security.py      # подпись Meta + верификация (13)
python tests/test_meta_payload.py       # разбор событий Meta + phone_number_id (22)
python tests/test_meta_client.py        # Meta-клиент: чанкинг/ретраи (14)
python tests/test_meta_webhook.py       # интеграция FastAPI, Meta-роуты (12)
python tests/test_multitenant.py        # мультитенант: маршрутизация/hot-reload (33)
python tests/test_admin_api.py          # админ-API: auth/маскирование/бэкапы/аудит (40)
python tests/test_admin_profile.py      # профиль WhatsApp: чтение/правка/аватар (52)
python tests/test_telegram_token.py     # токен бота: id/формат (10)
python tests/test_telegram_payload.py   # разбор update Telegram (19)
python tests/test_telegram_client.py    # Telegram-клиент: чанкинг/ok:false/ретраи (17)
python tests/test_telegram_webhook.py   # TG-вебхук + сосуществование с WA (26)
```

Все тесты автономны: сеть не используется (LLM и провайдеры — стабы/моки).

## Деплой на Railway (Render/Fly аналогично)

1. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`. Модуль отдаёт
   `app` лениво, так что `uvicorn main:app` работает.
2. Переменные окружения в дашборде: активного провайдера (см. `.env.example`)
   + `LLM_API_URL`, `LLM_API_KEY`. В single-tenant дополнительно `CLIENT_CONFIG`
   и `WHATSAPP_PHONE_NUMBER_ID`; в мультитенанте — только `META_APP_SECRET`,
   `META_VERIFY_TOKEN` и (при fallback-схеме токенов) `WHATSAPP_ACCESS_TOKEN`.
   `PORT` Railway подставляет сам — он имеет приоритет над `APP_PORT`;
   `APP_HOST` уже `0.0.0.0`.
3. В Settings → Networking задайте порт, который слушает приложение
   (PORT из окружения), и подключите домен.
4. Вебхук провайдера укажите на публичный домен:
   - Meta: `https://<домен>/webhooks/meta` (Callback URL в настройках приложения,
     Verify token = `META_VERIFY_TOKEN`, подписка на поле `messages`);
   - Bird: `https://<домен>/webhooks/bird` (events: `whatsapp.received`).

## Мультитенант: один деплой на 20+ клиентов

Один деплой обслуживает много номеров: событие каждого входящего сообщения
несёт `phone_number_id`, по которому оно маршрутизируется к конфигу клиента.
Режим включается, если задан `CLIENTS_DIR` **или** существует папка `clients/`
хотя бы с одним клиентским yaml — так старый single-tenant деплой не
переключается, пока в `clients/` нет ни одного клиента.

**Формат:** один yaml на клиента, имя файла = `phone_number_id` (цифры):

```
clients/
  _example.yaml             # образец с комментариями (в git, без секретов)
  1354249714436396.yaml     # Nails Studio — первый клиент (в git, токен пустой)
  <phone_number_id>.yaml    # остальные клиенты (не в git — там токены)
```

Скопируйте `clients/_example.yaml` под именем `<phone_number_id>.yaml` и
заполните: `business_name`, `knowledge_base`, `tone`, `language`,
`fallback_triggers`, `style_examples`, `owner_whatsapp_phone`, опционально
блок `llm` (пустой `model` = глобальный `LLM_MODEL`) и `access_token`.

Правила:

- **`access_token`** — токен System User с доступом к WABA клиента. Если поле
  пустое, используется глобальный `WHATSAPP_ACCESS_TOKEN` из env (случай «все
  номера под партнёрством и одним токеном»). Токены в git не попадают: папка
  `clients/` закрыта целиком, кроме `_example.yaml`. Конфиги клиентов живут
  на деплое (volume из `CLIENTS_DIR`) и локально — в репозитории их нет.
- **Hot-reload:** новый/изменённый/удалённый yaml подхватывается без рестарта
  (сверка mtime при каждом входящем событии). Изменение конфига пересоздаёт
  обработчик клиента — история его диалогов начинается заново.
- **Изоляция:** битый yaml или клиент без обязательных полей пропускается с
  warning, остальные клиенты работают; у каждого клиента свой
  `MetaWhatsAppClient` (его токен), свой system prompt и своя история.
- `App Secret` и `META_VERIFY_TOKEN` — глобальные, одни на весь деплой:
  события всех клиентов идут через одно приложение разработчика.
- Событие с незарегистрированным `phone_number_id` подтверждается `200`
  без обработки (warning в логе).
- `OWNER_WHATSAPP_NUMBER` в мультитенанте не применяется — номер владельца
  задаётся в yaml каждого клиента.

## Админ-API и панель /admin

Правка конфигов клиентов без деплоя и SSH. Включается в мультитенанте
заданным `ADMIN_TOKEN` (≥32 случайных символов, только в переменных
окружения — нигде в коде); иначе `/admin*` просто не существует (404).

**Доступ:** `X-Admin-Token: $ADMIN_TOKEN` — полный доступ; `X-Client-Token` —
персональный токен клиента (поле `management_token` в его yaml): клиент видит
и правит только свой конфиг и только бизнес-поля (название, тон, база знаний,
триггеры, номер владельца и т.п.) — свой `access_token`, `management_token` и
параметры `llm` ему менять нельзя. Токены клиентам выдаёте вы (сгенерируйте,
например, `python -c "import secrets; print(secrets.token_urlsafe(24))"` и
впишите в их yaml).

| Эндпоинт | Кто | Что делает |
|---|---|---|
| `GET /admin` | — | страница панели (данные — только через API с токеном) |
| `GET /admin/whoami` | любой токен | роль: `admin` или `client` + свой phone_number_id |
| `GET /admin/clients` | админ | список клиентов + пропущенные файлы с причинами |
| `GET /admin/clients/{id}` | админ или свой клиент | конфиг с замаскированными секретами |
| `PUT /admin/clients/{id}` | админ / свой клиент | создать/обновить: валидация → бэкап → атомарная запись → hot-reload |
| `DELETE /admin/clients/{id}` | админ | отключение клиента (бэкап + удаление yaml) |
| `GET /admin/clients/{id}/profile` | админ / свой клиент | профиль WhatsApp-номера из Meta |
| `PATCH /admin/clients/{id}/profile` | админ / свой клиент | изменить «о компании», контакты, сайт |
| `POST /admin/clients/{id}/profile/photo` | админ / свой клиент | заменить аватар номера (jpg/png/webp, ≤ 5 МБ) |

Гарантии записи: перед сохранением конфиг валидируется тем же кодом, что ест
реестр (битый yaml на диск не попадёт); запись атомарная; предыдущая версия
файла уходит в `clients/.history/<id>/<UTC-время>.yaml`; каждое изменение —
строка в `clients/.audit.jsonl` (кто, когда, какие поля — без значений).
Пустое или замаскированное (`EAAY…ab12`) значение секрета в PUT сохраняет
прежний токен. Клиент не может менять `access_token`, `management_token`
и блок `llm` — только админ.

Деплой: volume на путь из `CLIENTS_DIR` (например `/data/clients`), в
переменные — `ADMIN_TOKEN`; панель открывается на `https://<домен>/admin`
(токен хранится в localStorage браузера, авторизация — заголовком в каждом
запросе, куки и сессии не используются).

## Профиль WhatsApp в панели

Блок «Профиль WhatsApp» в редакторе клиента правит то, что видно рядом с
именем номера в WhatsApp: короткое «о компании», email, сайт и фото номера.
Правки уходят напрямую в Meta через Graph API (узел
`whatsapp_business_profile`) и **не сохраняются в `clients/*.yaml`** — там
профиля нет, источник истины в WhatsApp. Кнопка «Сохранить профиль» в панели
отдельна от «Сохранить изменения» (конфиг бота).

**Что можно:** `about` (до 139 символов), `description`, `email`, `websites`
(1–2 ссылки, обязательно с `https://`), `address`, аватар (jpg/png/webp,
до 5 МБ, квадратная картинка ~640×640 читается лучше всего). Панель при
заходе в редактор показывает текущие значения профиля и текущий аватар
(`profile_picture_url` — Meta отдаёт его только у номера, у которого фото
уже загружено; иначе блок пишет, что аватар не задан). После загрузки нового
фото профиль перечитывается; если картинка в блоке сменилась не сразу — это
CDN Meta, он обновляется с задержкой в минуту-две.

**Чего нельзя:** название бизнеса (display name) через API не меняется — оно
задаётся при регистрации номера и правится в WhatsApp Manager
(дашборд Meta), для произвольного названия нужен review. Каталог товаров,
отзывы, рассылки и смена номера — вне объёма панели.

**Права токена:** тот же `access_token`, что и у отправки сообщений, но ему
нужно разрешение `whatsapp_business_management` на WABA клиента (у System
User выдаётся вместе с `whatsapp_business_messaging`). Без него правка
профиля вернёт 502 с текстом Meta — в панели он показывается как есть.

**Как это устроено:** чтение и правка идут через Meta-клиент клиента
(`MetaWhatsAppClient`, тот же, что шлёт сообщения); кэша нет — источник
истины Meta. Ошибка Meta уходит в панель как 502 с её текстом, таймаут —
как 504. Пустое значение поля очищает его. Файл аватара **нигде у нас не
хранится**: ушёл в Meta — и всё (в `clients/.audit.jsonl` попадает только
имя действия, без содержимого). Успешные правки профиля пишут в тот же
`.audit.jsonl` строкой с `action: "profile"` / `"profile_photo"` и именами
полей, без их значений.

Клиент с `management_token` правит профиль **своего** номера; чужие профили
для него недоступны (403), как и прежде. Новых переменных окружения и volume
не требуется; в `requirements.txt` добавлен `python-multipart` — он нужен
FastAPI для приёма файла аватара.

## Онбординг нового клиента (модель партнёрства)

Схема: одно Meta-приложение и один BM у вас, у клиента — свой BM со своей WABA,
ваш BM добавлен к нему как партнёр. Один App Secret на все деплои, токен — на каждого клиента (System User в вашем BM, которому назначена его WABA).

1. Клиент: business.facebook.com → создаёт BM → WhatsApp → WABA → добавляет номер
   (подтверждение кодом; номер должен быть отвязан от WhatsApp-приложения на телефоне).
2. Клиент: Business Settings → **Partners** → Add partner → ваш BM ID →
   ассет **WhatsApp Account** → Full control.
3. Вы: проверяете доступ и подписываете WABA на вебхуки с override на его деплой.
   Для этого есть отдельный автономный скрипт **`wa_onboard.py`** (один файл,
   только стандартная библиотека — можно копировать куда угодно, проект не нужен):
   ```bash
   python wa_onboard.py --list --token $YOUR_SYSTEM_USER_TOKEN
   python wa_onboard.py \
       --waba-id 4434067880143233 \
       --webhook-url https://<деплой-клиента>.up.railway.app/webhooks/meta \
       --verify-token <verify-токен его деплоя> \
       --pin 123456 \
       --token $YOUR_SYSTEM_USER_TOKEN --send-test +77081178202
   ```
   Скрипт: устанавливает пин двухфакторки номера (`POST /{phone_number_id}/register`
   — удобно, если номер новый/мигрируется), подписывает WABA
   (`POST /{waba}/subscribed_apps` + override на URL деплоя), проверяет
   GET-верификацию деплоя, при `--send-test` отправляет контрольное сообщение
   и печатает готовый `.env` для нового деплоя. Работает без pip install.
1. Клиент: business.facebook.com → создаёт BM → WhatsApp → WABA → добавляет номер
   (подтверждение кодом; номер должен быть отвязан от WhatsApp-приложения на телефоне).
2. Клиент: Business Settings → **Partners** → Add partner → ваш BM ID →
   ассет **WhatsApp Account** → Full control.
3. Вы: проверяете доступ и подписываете WABA на вебхуки. Для этого есть
   отдельный автономный скрипт **`wa_onboard.py`** (один файл,
   только стандартная библиотека — можно копировать куда угодно, проект не нужен):
   ```bash
   python wa_onboard.py --list --token $YOUR_SYSTEM_USER_TOKEN
   python wa_onboard.py \
       --waba-id 4434067880143233 \
       --webhook-url https://<деплой>.up.railway.app/webhooks/meta \
       --verify-token <verify-токен деплоя> \
       --pin 123456 \
       --token $YOUR_SYSTEM_USER_TOKEN --send-test +77081178202
   ```
   Скрипт: устанавливает пин двухфакторки номера (`POST /{phone_number_id}/register`
   — удобно, если номер новый/мигрируется), подписывает WABA
   (`POST /{waba}/subscribed_apps`), проверяет GET-верификацию деплоя,
   при `--send-test` отправляет контрольное сообщение и печатает готовый `.env`.
   Работает без pip install.
   В мультитенанте все клиенты подписываются на **один** вебхук одного деплоя:
   `--webhook-url https://<общий-деплой>.up.railway.app/webhooks/meta`,
   verify-token один на всех.
4. Создаёте `clients/<phone_number_id>.yaml` по образцу `clients/_example.yaml`
   (база знаний, цены, тон; токен — в локальную копию файла или в глобальный
   `WHATSAPP_ACCESS_TOKEN`). Файл подхватывается без рестарта.
5. Клиент пишет номеру → бот отвечает.

Мессаджинг-лимиты у новых номеров стартуют с 250 уникальных собеседников/сутки
и растут с качеством. Бизнес-верификация клиента может понадобиться для масштаба.

## Отличия от chat-bot-demo (Telegram)

- Вместо long polling — вебхук с проверкой подписи (Meta или Bird),
  переключение провайдера через `MESSAGING_PROVIDER` без изменения логики.
- Владелец получает уведомления в WhatsApp (номер в `owner_whatsapp_phone`).
- `start` словом (не `/start`), нетекстовый контент → просьба написать текстом.
- Логика (история, `[HANDOFF]`, fallback, LLM-клиент, YAML-конфиги) перенесена
  без изменений.
