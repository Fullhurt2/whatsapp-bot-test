# ТЗ: Мультитенантный режим WhatsApp-бота (Meta Cloud API)

> Документ для агента-исполнителя. Самодостаточен: не требует контекста прошлых сессий.
> Статус обсуждения: дизайн согласован с владельцем проекта. Реализация — по этому ТЗ.

## 1. Контекст: что за проект и что уже работает

**Проект:** `whatsapp-bot-test` — FAQ-бот для микро-бизнесов в WhatsApp (аналог
`chat-bot-demo` для Telegram). Python 3.11+, FastAPI + uvicorn, httpx, PyYAML,
python-dotenv. Логика: входящее сообщение клиента → keyword-fallback → LLM
(OpenAI-совместимый, system prompt с базой знаний) → токен `[HANDOFF]` при
неуверенности → вежливый ответ клиенту + WhatsApp-уведомление владельцу.

**Текущий статус:** прод-деплой на Railway работает (Meta Cloud API,
провайдер `meta`). Полный цикл подтверждён живыми тестами: приём вебхука →
LLM → отправка. 9 наборов тестов (157 проверок) зелёные.

**Сейчас деплой = один клиент (single-tenant):**
- env: `WHATSAPP_ACCESS_TOKEN` (один), `WHATSAPP_PHONE_NUMBER_ID` (один),
  `META_APP_SECRET`, `META_VERIFY_TOKEN`, `CLIENT_CONFIG=client_config_*.yaml`.
- Владелец проекта подключает клиентов по партнёрской модели: клиент создаёт
  свой BM → WABA → номер → добавляет BM разработчика в партнёры (Full control
  на ассет WhatsApp Account). Разработчик подписывает WABA клиента на своё
  приложение (`POST /{waba}/subscribed_apps`) — все события летят в один вебхук.
- Подключён первый клиент: Nails Studio, `phone_number_id=1354249714436396`,
  аккаунт в режиме LIVE, качество GREEN.

**Задача этого ТЗ:** перевести сервис в мультитенант — один деплой обслуживает
20+ клиентов, маршрутизация по `phone_number_id`.

## 2. Решённые архитектурные вопросы (менять не нужно)

1. **Номера клиентов — в их собственных BM/WABA.** Клиент регистрирует свой BM
   → WABA → номер → добавляет BM разработчика в партнёры (Full control на
   ассет WhatsApp Account). Причины: изоляция рисков (нарушение правил одним
   клиентом не банит остальных), клиент владеет номером, у каждого свой
   лимит 250 уникальных собеседников/24ч на старте.
2. **LLM — один общий ключ** (`LLM_API_URL`, `LLM_API_KEY`, `LLM_MODEL` из env),
   используется для всех клиентов. Per-client override модели/temperature —
   опционально через yaml клиента (поле `llm`, как в текущих конфигах).
3. **Конфиги — YAML-файлы** (не БД). Формат: папка `clients/`, один файл на
   клиента, **имя файла = phone_number_id** (например `clients/1354249714436396.yaml`).
4. **Hot-reload:** новый/изменённый yaml клиента подхватывается без рестарта
   (проверка mtime при каждом входящем событии). Рестарт нужен только при
   изменении кода.
5. **App Secret и verify token — глобальные** (одни на весь деплой): события
   всех клиентов приходят через одно приложение разработчика, подпись
   `X-Hub-Signature-256` одинаковая.
6. **Токены клиентов:** в yaml клиента поле `access_token` (System User токен
   с доступом к его WABA). Папка `clients/` — в .gitignore, кроме
   `clients/_example.yaml`. Допускается глобальный `WHATSAPP_ACCESS_TOKEN`
   из env как fallback, если в yaml токен пустой (случай «все номера под
   партнёрством и одним токеном»).
7. **БД пока не делается** (осознанно): конфиги и история — в памяти/файлах.
   Перенос в Postgres — отдельное будущее ТЗ.

## 3. Контракты Meta API (проверены на живом проде)

- **Graph API base:** `https://graph.facebook.com/{version}`; версия задаётся
  env `META_GRAPH_VERSION` (по умолчанию v21.0; на проде используется v26.0 —
  работает).
- **Отправка:** `POST {base}/{phone_number_id}/messages`,
  `Authorization: Bearer <access_token>`, body:
  `{"messaging_product":"whatsapp","recipient_type":"individual","to":"<номер без +>","type":"text","text":{"preview_url":false,"body":"..."}}`
  → 200 `{"messaging_product":"whatsapp","contacts":[...],"messages":[{"id":"wamid..."}]}`.
  Ошибки: `{"error":{...}}`, HTTP 4xx/5xx. Лимит текста — 4096 символов на
  сообщение (длинные ответы бьются на части).
- **Входящий вебхук:** POST JSON вида
  `{"object":"whatsapp_business_account","entry":[{"changes":[{"field":"messages",
  "value":{"metadata":{"phone_number_id":"..."},"contacts":[{"profile":{"name":...},"wa_id":"77081178202"}],
  "messages":[{"from":"77081178202","id":"wamid...","type":"text","text":{"body":"привет"}}]}}]}]}`.
  Блок `value.statuses` — статусы доставки, игнорируется. Ровно одна «ветка
  контента» заполнена (text/image/.../interactive_reply). Может быть несколько
  entry/changes/messages в одном POST.
- **Подпись:** `X-Hub-Signature-256: sha256=<hex(hmac-sha256(app_secret, raw_body))>`,
  app secret — ОДИН на всех клиентов (секрет приложения разработчика).
- **GET-верификация подписки:** Meta дёргает
  `GET /webhooks/meta?hub.mode=subscribe&hub.verify_token=...&hub.challenge=...` —
  при совпадении `META_VERIFY_TOKEN` вернуть `hub.challenge` как plain text.
- **Требование ack:** ответить 2xx за ~15 сек → подпись проверяем, отвечаем 200
  сразу, обработка (LLM + отправка) в фоновой задаче (BackgroundTasks).
- **Дедупликация:** at-least-once доставка, повторные сообщения различаются по
  `messages[].id` (wamid...) — уже реализовано (`WebhookState.seen_before`).
- **Ограничение лимит-тира:** на старте номер имеет 250 уникальных
  собеседников/24ч, растёт с качеством (для микро-бизнеса достаточно). Сервисные
  ответы в 24-часовом окне бесплатны.

## 4. Текущая структура репо (что уже есть)

```
main.py                    # FastAPI: /webhooks/meta, /webhooks/bird, /healthz, /privacy
                           # WebhookState: LLMClient + BirdWhatsAppClient/MetaWhatsAppClient
                           # + MessageProcessor + seen-кэш; create_app(settings)
config/
  settings.py              # get_settings(): env + один клиентский yaml (single-tenant)
  client_config*.yaml      # конфиги клиентов (шаблоны бизнесов)
handlers/
  message_handler.py       # MessageProcessor(settings, llm_client, sender) — один на процесс
  owner_handler.py         # notify_owner(sender, settings, ...) — уведомление владельцу
services/
  llm_client.py            # LLMClient (OpenAI-совместимый) — без изменений
  fallback.py, language.py # без изменений
whatsapp/
  errors.py                # MessagingError, MessagingTimeout (общие для провайдеров)
  meta_client.py           # MetaWhatsAppClient(access_token, phone_number_id, graph_version) — send_text(to, text), close()
  meta_security.py         # verify_meta_signature(app_secret, raw_body, signature), verify_subscription(...)
  meta_payload.py          # parse_meta_events(payload) -> list[InboundMessage]
  inbound.py               # dataclass InboundMessage(phone, display_name, text, content_kind, message_id)
  bird_client.py           # BirdWhatsAppClient (single-tenant legacy, не трогать)
  webhook_security.py      # подпись Bird (не трогать)
  webhook_payload.py       # разбор Bird-события (не трогать)
scripts/send_test.py       # тестовая отправка (учитывает MESSAGING_PROVIDER)
tests/                     # 9 наборов, 157 проверок, все зелёные; запуск python tests/<имя>.py
wa_onboard.py              # АВТОНОМНЫЙ онбординг клиента (пин 2-FA, подписка WABA, env) — не менять логику
```

**Важно:** не ломать Bird-режим (`MESSAGING_PROVIDER=bird` работает как сейчас,
single-tenant). Все новые фичи — только на meta-пути. Тесты нельзя ухудшать.

## 5. Целевой дизайн мультитенанта

### 5.1 Реестр клиентов: папка `clients/`

Каждый клиент — один YAML, **имя файла = phone_number_id**:

```
clients/
  _example.yaml               # образец (в git), без секретов
  1354249714436396.yaml       # Nails Studio (первый клиент, в git — без токена)
  <phone_number_id>.yaml      # остальные клиенты
```

Формат файла клиента:

```yaml
# clients/1354249714436396.yaml
business_name: "Nails Studio"
tone: "вежливый, дружелюбный, на «вы»"
language: "auto"                  # ru / kk / auto
knowledge_base: |
  Услуги и цены:
  - Комби маникюр: 3000 ₸
  ...
owner_whatsapp_phone: "+77081178202"
fallback_triggers: ["жалоб", "отмен", "предоплат"]
style_examples: |
  Клиент: «Сколько стоит наращивание?»
  Хороший ответ: «8000 ₸ 💅 ...»
llm:                               # опционально; пусто = глобальные LLM_*
  model: ""
  temperature: 1.0
  max_tokens: 3500
  timeout_seconds: 15
  reasoning_effort: "medium"
access_token: "EAAY..."            # ТОКЕН ЭТОЙ WABA (папка в .gitignore!)
```

Требования к реестру:
- имя файла = `phone_number_id` (цифры) — уникальный ключ клиента;
- `access_token` обязателен (либо в yaml, либо глобальный `WHATSAPP_ACCESS_TOKEN`
  из env как fallback для всех, у кого поле пустое);
- `owner_whatsapp_phone` — опционален (без него уведомления пропускаются с warning,
  как сейчас);
- битый/пустой yaml → клиент пропускается с понятным логом, сервис не падает;
- все секреты — только в gitignored файлах (`clients/` в .gitignore целиком,
  кроме `clients/_example.yaml` без секретов).

### 5.2 Настройки (config/settings.py)

Разделить на два уровня:

1. **Глобальные (env):** `META_APP_SECRET`, `META_VERIFY_TOKEN`,
   `META_GRAPH_VERSION`, `MESSAGING_PROVIDER=meta`, `LLM_API_URL/KEY/MODEL`
   (дефолты), `APP_HOST/PORT`, `LOG_LEVEL`.
2. **Клиентские** (из `clients/*.yaml`): всё бизнесовое + `access_token`
   (fallback: глобальный `WHATSAPP_ACCESS_TOKEN`, если поле пустое).

Валидация: при старте сервис поднимается, даже если `clients/` пуст (режим
«только healthz» — но логировать предупреждение). Клиент без обязательных
полей (business_name, knowledge_base, access_token или глобальный токен)
— в лог и в реестр не попадает (не ронять весь сервис).

### 5.3 Обработчики (handlers/)

- `MessageProcessor` становится **per-client**: создаётся по экземпляру на
  клиента (свой system prompt, свой `sender`, своя история). Сигнатура и
  внутренности почти не меняются — меняется владение.
- История: ключ `(phone_number_id, клиентский телефон)` — но т.к. процессор
  на клиента отдельный, достаточно `phone` внутри процессора (как сейчас).
- `notify_owner` — без изменений (sender передаётся).

### 5.4 main.py / WebhookState

- `WebhookState`: вместо одного sender/processor —
  `processors: dict[phone_number_id, MessageProcessor]`, собирается при старте
  из реестра клиентов; каждый процессор имеет свой `MetaWhatsAppClient`
  (токен клиента + его phone_number_id).
- `POST /webhooks/meta`: разобрать payload → для каждого сообщения взять
  `value.metadata.phone_number_id` → найти процессор:
  - не найден → лог `WARNING` («событие для незарегистрированного номера») + ack 200;
  - найден → дедуп по `messages[].id` → BackgroundTasks → `processor.handle_event`.
- GET-верификация: один общий `META_VERIFY_TOKEN` (один URL на всех).
- Маршруты `/healthz`, `/privacy` остаются (privacy можно оставить общий).
- Обратная совместимость: если `clients/` пуст/отсутствует, а env содержит
  одиночный клиент (`WHATSAPP_PHONE_NUMBER_ID` + `CLIENT_CONFIG`) — работать
  как сейчас, single-tenant (режим определяется наличием папки/переменной
  `CLIENTS_DIR`; НЕ ломать текущий деплой Nails Studio, пока он не перенесён
  в clients/).

### 5.5 Горячая перезагрузка (hot-reload)

- Реестр клиентов перечитывается при изменении папки `clients/` (сравнение
  mtime/размера файлов; дёшево — проверка на каждом входящем событии или таймер 30с).
- Новый yaml → новый клиент появляется **без рестарта** (требование владельца).
- Изменённый yaml → перечитать конфиг клиента (история диалогов остаётся,
  если бизнес-конфиг не поменялся структурно).
- Удалённый yaml → клиент перестаёт обрабатываться (ack, лог).

## 6. Ограничения и требования

- **Не ломать Bird-режим** (`MESSAGING_PROVIDER=bird` — текущее поведение).
- Не логировать текст ответов LLM (как сейчас) — только длину и задержки.
- Токены не попадают в git: `clients/` в .gitignore (кроме примера).
- Тесты: все существующие 9 наборов остаются зелёными; новые — на маршрутизацию
  (два клиента с разными phone_number_id → разные процессоры и разные токены
  отправки), hot-reload, unknown-number ack.
- Ошибки одного клиента не влияют на других (изоляция исключений уже есть в
  `WebhookState.handle_event` — сохранить).

## 7. Приёмка (acceptance criteria)

1. `python tests/...` — все 9 существующих наборов зелёные + новые тесты мультитенанта.
2. Два фейковых клиента с разными phone_number_id в тестах получают ответы
   от своих конфигов и своими токенами (мок-транспорт).
3. Событие с неизвестным phone_number_id → 200, без отправок, с предупреждением.
4. `python main.py` и `uvicorn main:app` поднимаются (env: только глобальные
   переменные + clients/ с одним клиентом).
5. `wa_onboard.py` не изменён и продолжает работать.

## 8. Вне скоупа (не делать)

- БД/Postgres для конфигов и истории (следующий этап по решению владельца).
- Embedded Signup / Tech Provider.
- Мультитенант для Bird-провайдера.
- Админ-панель.
