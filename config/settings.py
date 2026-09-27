"""Загрузка настроек: .env (секреты, инфраструктура) + конфиг клиента (данные бизнеса).

Порядок применения:
  1. .env          — ключи Bird, номера, LLM (никогда не хардкодятся в код).
  2. config/client_config*.yaml — всё, что относится к конкретному бизнесу.
Под нового клиента правится только конфиг в config/; какой из них грузить —
переменная CLIENT_CONFIG в .env (по умолчанию client_config.yaml).
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DEFAULT_CONFIG_FILE = "client_config.yaml"

# Подхватываем .env из корня проекта (не хардкодим секреты)
load_dotenv(BASE_DIR / ".env")


def resolve_config_path() -> Path:
    """Путь к конфигу клиента, выбранному переменной CLIENT_CONFIG в .env.

    По умолчанию — config/client_config.yaml. Значение может быть с именем
    файла или без расширения (.yaml подставляется автоматически), чтобы в .env
    можно было писать просто `CLIENT_CONFIG=client_config_nails_atyrau`.
    Берётся только имя файла: конфиг всегда ищется в каталоге config/.
    """
    raw = os.getenv("CLIENT_CONFIG", "").strip() or DEFAULT_CONFIG_FILE
    name = raw if raw.lower().endswith((".yaml", ".yml")) else f"{raw}.yaml"
    if Path(name).name != name:
        logger.warning(
            "CLIENT_CONFIG=%r содержит путь — используется только имя файла %r "
            "(конфиг берётся из каталога config/)",
            raw, Path(name).name,
        )
    return CONFIG_DIR / Path(name).name


def derive_bird_api_url(api_key: str) -> str | None:
    """Регион Bird из префикса API-ключа: bk_eu1_* -> eu1, bk_us1_* -> us1.

    Оба дата-плейна — https://{region}.platform.bird.com; неправильный регион
    отвечает 421 Misdirected Request, поэтому надёжнее выводить URL из ключа.
    """
    if api_key.startswith("bk_eu1_"):
        return "https://eu1.platform.bird.com"
    if api_key.startswith("bk_us1_"):
        return "https://us1.platform.bird.com"
    return None


def normalize_phone(raw: str) -> str:
    """Приводит номер к E.164 без '+7 777 (123) 45-67' -> '+77771234567'.

    Возвращает строку как есть, если номер не похож на телефон, — валидность
    проверяется на стороне Bird, здесь только косметика.
    """
    digits = re.sub(r"[^\d+]", "", str(raw))
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    return digits


@dataclass(frozen=True)
class LLMParams:
    """Параметры вызова LLM — всё настраивается в client_config.yaml."""

    model: str
    temperature: float
    max_tokens: int
    timeout_seconds: int
    reasoning_effort: str | None = None


@dataclass(frozen=True)
class Settings:
    """Сводные настройки бота: секреты из .env + бизнес-конфиг из YAML."""

    # из .env
    messaging_provider: str  # "bird" или "meta" (MESSAGING_PROVIDER)
    app_host: str
    app_port: int
    llm_api_url: str
    llm_api_key: str
    # --- провайдер Bird (используется при messaging_provider="bird") ---
    bird_api_key: str
    bird_webhook_secret: str
    bird_api_url: str
    whatsapp_sender_number: str  # бизнес-номер (поле "from" при отправке)
    # из .env — Meta Cloud API (при messaging_provider="meta")
    whatsapp_access_token: str   # токен System User с правами whatsapp_business_messaging
    whatsapp_phone_number_id: str  # ID бизнес-номера отправителя в Graph API
    meta_app_secret: str         # App Secret приложения — проверка X-Hub-Signature-256
    meta_verify_token: str       # произвольная строка для привязки вебхука в дашборде
    meta_graph_version: str      # версия Graph API (по умолчанию v21.0)
    # из client_config.yaml
    business_name: str
    tone: str
    language: str
    knowledge_base: str
    owner_phone: str | None
    fallback_triggers: list[str] = field(default_factory=list)
    llm: LLMParams = field(default_factory=lambda: LLMParams("", 0.6, 3500, 15))
    # Примеры тёплого/дружеского ответа (few-shot) — необязательное поле.
    style_examples: str = ""
    # Имя загруженного конфига клиента (для логов старта).
    config_file: str = ""
    # Папка реестра клиентов (мультитенант, Meta): задана -> один деплой
    # обслуживает clients/*.yaml, маршрутизация по phone_number_id.
    # Пусто = single-tenant (один клиент из CLIENT_CONFIG, как раньше).
    clients_dir: str = ""
    # Токен админ-API (заголовок X-Admin-Token в /admin/*). Не задан ->
    # админ-роуты и панель /admin отключены.
    admin_token: str = ""
    # Служебные ответы бота (передача/таймаут) с переопределением из yaml.
    # Пустая строка = встроенный текст для этого языка; {business_name} подставится.
    fallback_reply_ru: str = ""
    fallback_reply_kk: str = ""
    timeout_reply_ru: str = ""
    timeout_reply_kk: str = ""

    # Встроенные служебные ответы (lang — язык сообщения клиента: "ru"/"kk").
    _FALLBACK_TEMPLATES = {
        "ru": "Передаю ваш вопрос команде {business_name} — скоро ответят лично. 🙌",
        "kk": "Сұрағыңызды {business_name} командасына жеткіземін — жақында өздері жауап береді. 🙌",
    }
    _TIMEOUT_TEMPLATES = {
        "ru": "Секунду, уточняю у {business_name}… ⏳",
        "kk": "Бір сәт, {business_name} нақтылаймын… ⏳",
    }

    def fallback_reply(self, lang: str = "ru") -> str:
        """Ответ клиенту при передаче человеку на его языке."""
        override = self.fallback_reply_kk if lang == "kk" else self.fallback_reply_ru
        template = override or self._FALLBACK_TEMPLATES.get(lang, self._FALLBACK_TEMPLATES["ru"])
        return template.format(business_name=self.business_name)

    def timeout_reply(self, lang: str = "ru") -> str:
        """Сообщение клиенту при таймауте/ошибке LLM на его языке."""
        override = self.timeout_reply_kk if lang == "kk" else self.timeout_reply_ru
        template = override or self._TIMEOUT_TEMPLATES.get(lang, self._TIMEOUT_TEMPLATES["ru"])
        return template.format(business_name=self.business_name)


def _load_config() -> tuple[dict, str]:
    """Читает YAML-конфиг клиента; отсутствие файла — фатальная ошибка запуска.

    Возвращает (данные конфига, имя файла).
    """
    path = resolve_config_path()
    if not path.exists():
        raise FileNotFoundError(
            f"Не найден конфиг клиента: {path}. "
            f"Создайте его по образцу из README или укажите другой файл "
            f"в переменной CLIENT_CONFIG (сейчас: {os.getenv('CLIENT_CONFIG', '') or DEFAULT_CONFIG_FILE})."
        )
    logger.info("Конфиг клиента: %s (выбор через CLIENT_CONFIG)", path.name)
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data, path.name


def _resolve_owner_phone(cfg: dict) -> str | None:
    """Номер владельца: OWNER_WHATSAPP_NUMBER из .env перекрывает yaml.

    Номер нормализуется к E.164. Владелец может отсутствовать — бот всё
    равно отвечает клиентам, но уведомления будут пропущены с предупреждением.
    Явно заданное (override) значение с опечаткой — ошибка конфигурации.
    """
    override = os.getenv("OWNER_WHATSAPP_NUMBER", "").strip()
    raw = override or str(cfg.get("owner_whatsapp_phone") or "").strip()
    if not raw:
        return None
    phone = normalize_phone(raw)
    if not re.fullmatch(r"\+\d{8,15}", phone):
        if override:
            # Явно заданное значение с опечаткой — падаем на старте.
            raise RuntimeError(
                f"OWNER_WHATSAPP_NUMBER={override!r} не похож на номер телефона "
                "(ожидается E.164, например +77770000000)"
            )
        logger.warning(
            "owner_whatsapp_phone=%r не похож на номер телефона (E.164) — "
            "уведомления владельцу работать не будут",
            raw,
        )
        return None
    if override:
        logger.info("OWNER_WHATSAPP_NUMBER задан — owner=%s (перекрывает yaml)", phone)
    return phone


def resolve_clients_dir() -> Path | None:
    """Папка реестра клиентов или None — значит single-tenant (как раньше).

    CLIENTS_DIR задаёт папку явно (относительный путь — от корня проекта).
    Без переменной мультитенант включается автоматически, если существует
    папка clients/ с хотя бы одним клиентским yaml (имя файла = цифры,
    файлы-образцы с префиксом "_" не считаются) — так текущий single-tenant
    деплой не переключается, пока в clients/ нет ни одного клиента.
    Актуально только для MESSAGING_PROVIDER=meta; Bird всегда single-tenant.
    """
    raw = os.getenv("CLIENTS_DIR", "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else BASE_DIR / path
    default_dir = BASE_DIR / "clients"
    try:
        for entry in os.scandir(default_dir):
            name = entry.name
            if not name.lower().endswith((".yaml", ".yml")):
                continue
            if name.startswith("_"):
                continue
            if Path(name).stem.isdigit():
                return default_dir
    except OSError:
        return None
    return None


def _get_multitenant_settings(provider: str, clients_dir: Path) -> Settings:
    """Глобальные настройки мультитенант-режима (Meta): только общие env.

    Бизнес-поля и access_token у каждого клиента свои — они собираются
    в реестре из clients/*.yaml (см. config/clients.py), и валидация
    per-client происходит там: один битый клиент не роняет весь сервис.
    """
    if os.getenv("CLIENT_CONFIG", "").strip():
        logger.info("CLIENT_CONFIG задан, но включён мультитенант (clients/) — переменная игнорируется")
    settings = Settings(
        messaging_provider=provider,
        bird_api_key="",
        bird_webhook_secret="",
        bird_api_url="",
        whatsapp_sender_number="",
        # Глобальный токен — fallback для клиентов с пустым access_token
        # (случай «все номера под партнёрством и одним токеном»).
        whatsapp_access_token=os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip(),
        whatsapp_phone_number_id="",
        meta_app_secret=os.getenv("META_APP_SECRET", "").strip(),
        meta_verify_token=os.getenv("META_VERIFY_TOKEN", "").strip(),
        meta_graph_version=os.getenv("META_GRAPH_VERSION", "").strip() or "v21.0",
        app_host=os.getenv("APP_HOST", "0.0.0.0").strip() or "0.0.0.0",
        app_port=int(os.getenv("PORT") or os.getenv("APP_PORT") or "8000"),
        llm_api_url=os.getenv("LLM_API_URL", "").strip(),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        business_name="",
        tone="",
        language="ru",
        knowledge_base="",
        owner_phone=None,
        llm=LLMParams(
            model=os.getenv("LLM_MODEL", "").strip(),
            temperature=0.6,
            max_tokens=3500,
            timeout_seconds=15,
        ),
        clients_dir=str(clients_dir),
        # Админ-API (панель /admin): полный доступ к clients/*.yaml.
        # Не обязателен — без него админ-роуты просто не регистрируются.
        admin_token=os.getenv("ADMIN_TOKEN", "").strip(),
    )
    if 0 < len(settings.admin_token) < 32:
        logger.warning(
            "ADMIN_TOKEN короче 32 символов — для продакшена сгенерируйте длиннее "
            "(например python -c \"import secrets; print(secrets.token_urlsafe(32))\")"
        )
    # Обязательное — только то, без чего сервис в принципе не работает.
    # Per-client обязательные поля (business_name, knowledge_base, токен,
    # модель) проверяет реестр при загрузке каждого yaml.
    required = {
        "LLM_API_URL (.env)": settings.llm_api_url,
        "LLM_API_KEY (.env)": settings.llm_api_key,
        "META_APP_SECRET (.env)": settings.meta_app_secret,
        "META_VERIFY_TOKEN (.env)": settings.meta_verify_token,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Не заданы обязательные настройки: {', '.join(missing)}")
    return settings


def get_settings() -> Settings:
    """Собирает итоговые настройки из .env и конфига клиента (CLIENT_CONFIG).

    Бросает RuntimeError с понятным описанием, если не хватает обязательных полей.
    """
    provider = (os.getenv("MESSAGING_PROVIDER", "bird").strip().lower() or "bird")
    if provider not in ("bird", "meta"):
        raise RuntimeError(
            f"MESSAGING_PROVIDER={provider!r} не поддерживается: ожидается 'bird' или 'meta'"
        )

    # Мультитенант: CLIENTS_DIR задан или в clients/ есть хотя бы один клиент.
    if provider == "meta":
        clients_dir = resolve_clients_dir()
        if clients_dir is not None:
            logger.info("Режим мультитенант: реестр клиентов в %s", clients_dir)
            return _get_multitenant_settings(provider, clients_dir)

    cfg, config_file = _load_config()

    model = str((cfg.get("llm") or {}).get("model") or "").strip() or os.getenv("LLM_MODEL", "").strip()
    llm = LLMParams(
        model=model,
        temperature=float((cfg.get("llm") or {}).get("temperature", 0.6)),
        max_tokens=int((cfg.get("llm") or {}).get("max_tokens", 3500)),
        timeout_seconds=int((cfg.get("llm") or {}).get("timeout_seconds", 15)),
        reasoning_effort=(cfg.get("llm") or {}).get("reasoning_effort") or None,
    )

    bird_api_key = os.getenv("BIRD_API_KEY", "").strip()
    bird_api_url = (
        os.getenv("BIRD_API_URL", "").strip().rstrip("/")
        or derive_bird_api_url(bird_api_key)
        or ""
    )

    owner_phone = _resolve_owner_phone(cfg)

    settings = Settings(
        messaging_provider=provider,
        bird_api_key=os.getenv("BIRD_API_KEY", "").strip(),
        bird_webhook_secret=os.getenv("BIRD_WEBHOOK_SECRET", "").strip(),
        bird_api_url=bird_api_url or "",
        whatsapp_sender_number=normalize_phone(os.getenv("WHATSAPP_SENDER_NUMBER", "").strip()),
        whatsapp_access_token=os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip(),
        whatsapp_phone_number_id=os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip(),
        meta_app_secret=os.getenv("META_APP_SECRET", "").strip(),
        meta_verify_token=os.getenv("META_VERIFY_TOKEN", "").strip(),
        meta_graph_version=os.getenv("META_GRAPH_VERSION", "").strip() or "v21.0",
        # Порт: платформы-деплои (Railway/Render/Fly) подставляют PORT — он главный;
        # APP_PORT нужен для локального запуска, APP_HOST=0.0.0.0 обязателен в контейнере.
        app_host=os.getenv("APP_HOST", "0.0.0.0").strip() or "0.0.0.0",
        app_port=int(os.getenv("PORT") or os.getenv("APP_PORT") or "8000"),
        llm_api_url=os.getenv("LLM_API_URL", "").strip(),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        business_name=str(cfg.get("business_name") or "").strip(),
        tone=str(cfg.get("tone") or "").strip(),
        language=str(cfg.get("language") or "ru").strip().lower(),
        knowledge_base=str(cfg.get("knowledge_base") or "").strip(),
        owner_phone=owner_phone,
        fallback_triggers=[str(t).strip() for t in (cfg.get("fallback_triggers") or []) if str(t).strip()],
        llm=llm,
        style_examples=str(cfg.get("style_examples") or "").strip(),
        config_file=config_file,
        fallback_reply_ru=str(cfg.get("fallback_reply_ru") or "").strip(),
        fallback_reply_kk=str(cfg.get("fallback_reply_kk") or "").strip(),
        timeout_reply_ru=str(cfg.get("timeout_reply_ru") or "").strip(),
        timeout_reply_kk=str(cfg.get("timeout_reply_kk") or "").strip(),
    )

    # Проверяем обязательные поля до старта, чтобы бот падал сразу с внятной ошибкой.
    # Общие поля (LLM, бизнес) + свои у каждого провайдера: для Meta Bird-ключи
    # не требуются и наоборот — так можно держать оба набора в .env и переключаться.
    required = {
        "LLM_API_URL (.env)": settings.llm_api_url,
        "LLM_API_KEY (.env)": settings.llm_api_key,
        "business_name (клиентский yaml)": settings.business_name,
        "knowledge_base (клиентский yaml)": settings.knowledge_base,
        "llm.model (.env или клиентский yaml)": settings.llm.model,
    }
    if settings.messaging_provider == "meta":
        required.update({
            "WHATSAPP_ACCESS_TOKEN (.env, System User)": settings.whatsapp_access_token,
            "WHATSAPP_PHONE_NUMBER_ID (.env)": settings.whatsapp_phone_number_id,
            "META_APP_SECRET (.env)": settings.meta_app_secret,
            "META_VERIFY_TOKEN (.env)": settings.meta_verify_token,
        })
    else:  # bird
        required.update({
            "BIRD_API_KEY (.env)": settings.bird_api_key,
            "BIRD_WEBHOOK_SECRET (.env)": settings.bird_webhook_secret,
            "BIRD_API_URL (.env или авто по префиксу ключа)": settings.bird_api_url,
            "WHATSAPP_SENDER_NUMBER (.env)": settings.whatsapp_sender_number,
        })
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(f"Не заданы обязательные настройки: {', '.join(missing)}")

    return settings
