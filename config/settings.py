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
    bird_api_key: str
    bird_webhook_secret: str
    bird_api_url: str
    whatsapp_sender_number: str  # бизнес-номер (поле "from" при отправке)
    app_host: str
    app_port: int
    llm_api_url: str
    llm_api_key: str
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


def get_settings() -> Settings:
    """Собирает итоговые настройки из .env и конфига клиента (CLIENT_CONFIG).

    Бросает RuntimeError с понятным описанием, если не хватает обязательных полей.
    """
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
        bird_api_key=os.getenv("BIRD_API_KEY", "").strip(),
        bird_webhook_secret=os.getenv("BIRD_WEBHOOK_SECRET", "").strip(),
        bird_api_url=bird_api_url or "",
        whatsapp_sender_number=normalize_phone(os.getenv("WHATSAPP_SENDER_NUMBER", "").strip()),
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
    missing = [
        name
        for name, value in {
            "BIRD_API_KEY (.env)": settings.bird_api_key,
            "BIRD_WEBHOOK_SECRET (.env)": settings.bird_webhook_secret,
            "BIRD_API_URL (.env или авто по префиксу ключа)": settings.bird_api_url,
            "WHATSAPP_SENDER_NUMBER (.env)": settings.whatsapp_sender_number,
            "LLM_API_URL (.env)": settings.llm_api_url,
            "LLM_API_KEY (.env)": settings.llm_api_key,
            "business_name (клиентский yaml)": settings.business_name,
            "knowledge_base (клиентский yaml)": settings.knowledge_base,
            "llm.model (.env или клиентский yaml)": settings.llm.model,
        }.items()
        if not value
    ]
    if missing:
        raise RuntimeError(f"Не заданы обязательные настройки: {', '.join(missing)}")

    return settings
