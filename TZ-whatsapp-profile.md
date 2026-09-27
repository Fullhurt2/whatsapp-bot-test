# ТЗ: Блок «Профиль WhatsApp» в админ-панели (аватар, about, контакты)

> Документ для агента-исполнителя. Самодостаточен: не требует контекста прошлых сессий.
> Статус: дизайн согласован с владельцем проекта. Реализация — по этому ТЗ.

## 1. Контекст: что уже работает

Проект: `whatsapp-bot-test` — FAQ-бот для микро-бизнесов в WhatsApp (Meta Cloud API),
FastAPI + uvicorn + httpx + PyYAML. Уже реализовано и работает:

- **Мультитенант**: реестр клиентов — папка `clients/<phone_number_id>.yaml`
  (config/clients.py, hot-reload по mtime при каждом входящем событии).
  Per-tenant объект — frozen `Settings` из `config.settings`, собирается в
  `validate_tenant_config(cfg, base, phone_number_id, file_name)`.
- **Админ-API** (`admin/api.py`): роуты под `create_app` в main.py при
  `state.multitenant` и заданном `ADMIN_TOKEN`. Роли:
  - `X-Admin-Token` (env `ADMIN_TOKEN`) — полный доступ;
  - `X-Client-Token` (поле `management_token` из yaml клиента) — доступ
    только к своему конфигу и только к бизнес-полям (`CLIENT_EDITABLE_FIELDS`).
  Аудит — `clients/.audit.jsonl` (actor/action/changed, без значений).
- **Панель** (`admin/static/index.html`): vanilla JS, токен в localStorage,
  заголовки `X-Admin-Token` + `X-Client-Token` (одно значение в оба заголовка),
  авторизация через `GET /admin/whoami`. Тесты — plain scripts
  `python tests/test_<имя>.py` (без pytest), стиль см. `tests/test_admin_api.py`.
- Транспорт Meta: `whatsapp/meta_client.py` — класс `MetaWhatsAppClient`
  с `httpx.AsyncClient` на `https://graph.facebook.com`, параметр `transport=`
  для тестов (MockTransport), исключения `MetaError`/`MetaTimeout`
  (наследники `MessagingError`/`MessagingTimeout` из `whatsapp/errors.py`).

**Задача этого ТЗ:** дать клиентам возможность управлять профилем своего
WhatsApp-номера (аватар, короткое описание, сайт, email) прямо из панели —
через Meta Graph API.

## 2. Контракты Meta API (базовые; точные лимиты сверить с docs)

- **Чтение профиля:** `GET https://graph.facebook.com/{version}/{phone_number_id}/whatsapp_business_profile?fields=about,description,email,websites,vertical,address`
  с `Authorization: Bearer <access_token клиента>` → `{"data": [{...}]}`.
  Поля могут отсутствовать — трактовать как пустые.
- **Изменение текстовых полей:** `PATCH` того же URL, JSON-тело с полями
  (`about`, `description`, `email`, `websites` — список URL, `vertical`)
  → `{"success": true}`.
- **Аватар:** `POST` того же URL, multipart/form-data: поле `photo` (binary)
  и `messaging_product: "whatsapp"` → `{"success": true}`. Форматы jpg/png,
  рекомендуемый размер ~640×640, лимит ~5 МБ — сверить с актуальными доками.
- **`about` ограничен ~139 символами** — проверить точное значение в доках
  и валидировать на бэкенде.
- **Название (display name) через API не меняется** — задаётся при регистрации
  номера и правится только в WhatsApp Manager. В UI это явно указать.
- Ошибки Meta приходят как `{"error": {"message": ..., "code": ...}}` —
  наружу отдавать 502 с текстом `error.message`.
- Версия Graph API — из `settings.meta_graph_version`.

## 3. Изменения по файлам

**1. `whatsapp/meta_client.py` — три новых метода** у `MetaWhatsAppClient`
(тот же `self._client`, базовый URL уже настроен):
- `async def get_business_profile(self) -> dict` — GET с полями
  `about,description,email,websites,vertical,address`; пустые поля → `""`.
- `async def update_business_profile(self, fields: dict) -> None` — PATCH.
- `async def upload_profile_photo(self, filename: str, content: bytes,
  content_type: str) -> None` — multipart POST (`photo` + `messaging_product=whatsapp`).
- Ошибки мапить в существующие `MetaError`/`MetaTimeout` (как `send_text`).

**2. `admin/api.py` — три роута** (внутри `register_admin_api`, роли как
у остального API: админ или клиент со своим pid; клиентский токен проверяется
как сейчас через `_authorize`):
- `GET /admin/clients/{pid}/profile` — текущий профиль из Meta
  (кэш не нужен, вызов прямой). Ответ: `{"about": ..., "description": ...,
  "email": ..., "websites": [...], "address": ...}`.
- `PATCH /admin/clients/{pid}/profile` — тело JSON с любым подмножеством
  полей. Валидация: `about` ≤ 139 символов; `websites` — 1–2 ссылки,
  начинающиеся с `https://`; `email` — простая проверка `@`. После PATCH —
  запись в `clients/.audit.jsonl` (actor как у PUT, `action: "profile"`,
  `changed` — имена полей).
- `POST /admin/clients/{pid}/profile/photo` — multipart (`file`), jpg/png/webp,
  ≤ 5 МБ; файл никуда не сохраняется на диск, уходит сразу в Meta; аудит
  (`action: "profile_photo"`, без содержимого).
- Роли и ошибки — те же конвенции, что у существующих роутов
  (`_authorize`, 401/403/404; ошибки Meta → 502 с текстом для показа в панели).
- Никаких новых env; Graph-версия и доступ — из существующих настроек.

**3. `admin/static/index.html` — блок «Профиль WhatsApp» в editorCard**
(после секции «О бизнесе», виден и клиенту, и админу):
- `about` — textarea с живым счётчиком «N / 139»;
- `email`, `сайт (https://…)` — inputs;
- кнопка «Обновить аватар» — `input type=file` (jpg/png), отправка на
  photo-эндпоинт, статус результата; превью текущего аватара — только если
  API его отдаёт (проверить живьём; если нет — показывать только статус);
- кнопка «Сохранить профиль» отдельная от сохранения конфига;
- пояснение в UI: «Название бизнеса в WhatsApp меняется только в дашборде
  Meta — через API не изменяется».
- Тексты полей — в стиле уже сделанных подсказок (никакого жаргона).

**4. Тесты** — новый `tests/test_admin_profile.py` в стиле существующих
(MockTransport, `check()`, tmp-папка):
- GET профиля: парсинг `{"data": [...]}` → поля; 401 без токена; 403 чужой pid;
- PATCH about: правильный payload в Meta, аудит-строка, лимит 139 → 400;
- photo: multipart-запрос уходит с messaging_product=whatsapp, успех/ошибка;
- ошибка Meta → 502 с текстом;
- клиентский токен правит только свой профиль;
- все 11 существующих наборов остаются зелёными.

**5. README** — раздел «Профиль WhatsApp в панели» (что можно, что нельзя,
нужные права токена).

## 4. Ограничения

- Фото не хранится у нас: ушло в Meta — и всё (нет роста volume).
- Токены не логируются; аудит — только имена полей.
- Railway: новых переменных и volume-изменений не требуется.
- Не ломать существующие роуты и тесты; Bird не трогать.

## 5. Вне скоупа

Название бизнеса (display name) — через API не меняется, только в дашборде
Meta (для произвольного названия нужен review). Каталог товаров, отзывы,
рассылки, смена номера — не делать.
