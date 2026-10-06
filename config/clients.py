"""Реестр клиентов мультитенантного режима: папка clients/, один YAML на клиента.

Имя файла — ключ клиента: у Meta-клиентов это phone_number_id бизнес-номера
в Graph API (clients/1354249714436396.yaml), у Telegram-клиентов — id бота из
токена (clients/123456789.yaml, поле provider: tg), у Zernio-клиентов —
произвольный slug (clients/nails-studio.yaml, поле provider: zernio), а
accountId задаётся полем zernio_account_id внутри. Из файла собирается
полноценный Settings: бизнес-поля и токен — из yaml, глобальные вещи
(LLM-ключ, app secret, verify token, ключ Zernio, версия Graph API) — из env.
Так MessageProcessor и notify_owner работают с per-tenant Settings без
изменений в коде обработчиков.

Требования к файлу клиента:
  - имя файла — цифры (Meta/TG) или slug (Zernio);
  - business_name и knowledge_base обязательны;
  - llm.model — в yaml или глобальный LLM_MODEL из env;
  - provider: wa (по умолчанию), tg или zernio;
  - wa: access_token — в yaml или глобальный WHATSAPP_ACCESS_TOKEN (fallback
    для схемы «все номера под партнёрством и одним общим токеном»);
  - tg: telegram_bot_token обязателен, имя файла = id бота из токена;
  - zernio: zernio_account_id обязателен (24 hex), ключ маршрутизации = он.
Битый/неполный файл не роняет сервис: клиент пропускается с warning-логом.

Hot-reload: снапшот папки (имя, mtime, размер) сверяется при каждом входящем
событии — новый/изменённый/удалённый yaml подхватывается без рестарта.
"""

import logging
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

import yaml

from config.settings import LLMParams, MediaSettings, Settings, normalize_phone
from whatsapp.telegram_token import bot_id_from_token

logger = logging.getLogger(__name__)

# phone_number_id в Graph API — числовой идентификатор; у Meta-клиента
# (provider: wa) имя файла = он же. У Telegram-клиента (tg) — id бота (тоже
# цифры). У Zernio-клиента (zernio) имя файла — произвольный slug, а
# accountId (24 hex) задаётся полем zernio_account_id внутри yaml.
_PHONE_NUMBER_ID_RE = re.compile(r"\d{1,20}")

# Slug имени файла клиента Zernio: буквы/цифры/точка/дефис/подчёркивание,
# без разделителей пути. accountId — тоже валидный slug, можно использовать его.
_CLIENT_ID_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# accountId подключённого аккаунта Zernio — 24 hex-символа (Mongo ObjectId).
_ZERNIO_ACCOUNT_ID_RE = re.compile(r"[0-9a-f]{24}")

# Файлы-образцы (префикс "_") в реестр не попадают.
EXAMPLE_PREFIX = "_"

YAML_SUFFIXES = (".yaml", ".yml")


def is_client_file(path: Path) -> bool:
    """Похоже ли имя файла на ключ клиента: <цифры|slug>.yaml (не _example.yaml)."""
    return (
        path.suffix.lower() in YAML_SUFFIXES
        and not path.name.startswith(EXAMPLE_PREFIX)
        and bool(
            _PHONE_NUMBER_ID_RE.fullmatch(path.stem)
            or _CLIENT_ID_SLUG_RE.fullmatch(path.stem)
        )
    )


def _int_or(value, default: int) -> int:
    """Число из yaml клиента; мусор или пустое значение = базовое."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _features_of(cfg: dict, base) -> dict:
    """Флаги фич клиента поверх базовых (неизвестные ключи игнорируем)."""
    features = dict(getattr(base, "features", {}) or {})
    own = cfg.get("features")
    if isinstance(own, dict):
        for key, flag in own.items():
            if isinstance(flag, bool):
                features[str(key)] = flag
    return features


def is_valid_phone_number_id(value: str) -> bool:
    """Строка похожа на phone_number_id: только цифры (ключ Meta/TG-клиента)."""
    return bool(_PHONE_NUMBER_ID_RE.fullmatch(value))


def is_valid_client_id(value: str) -> bool:
    """Ключ клиента для имён файлов и админ-API: цифры (Meta/TG) или slug (Zernio)."""
    return bool(
        _PHONE_NUMBER_ID_RE.fullmatch(value)
        or _CLIENT_ID_SLUG_RE.fullmatch(value)
    )


def is_valid_zernio_account_id(value: str) -> bool:
    """accountId Zernio: 24 hex-символа (например 66b2e19d8c3f5a7e9d0b1c2d)."""
    return bool(_ZERNIO_ACCOUNT_ID_RE.fullmatch(value.strip().lower()))


def resolve_clients_dir(base_dir: Path) -> Path | None:
    """Папка реестра клиентов или None — работать по-старому (single-tenant).

    CLIENTS_DIR в .env задаёт папку явно (относительные пути — от корня
    проекта). Без переменной мультитенант включается автоматически, если
    рядом есть папка clients/ хотя бы с одним клиентским yaml — так текущий
    single-tenant деплой (клиент в CLIENT_CONFIG) продолжает работать, пока
    владельцы не перенесут его в clients/.
    """
    raw = os.getenv("CLIENTS_DIR", "").strip()
    if raw:
        path = Path(raw)
        return path if path.is_absolute() else base_dir / path
    default_dir = base_dir / "clients"
    if _has_client_file(default_dir):
        return default_dir
    return None


def _has_client_file(clients_dir: Path) -> bool:
    """Есть ли в папке хотя бы один файл вида <phone_number_id>.yaml."""
    try:
        for entry in os.scandir(clients_dir):
            if not entry.is_file():
                continue
            candidate = Path(entry.name)
            if is_client_file(candidate):
                return True
    except OSError:
        return False
    return False


def validate_tenant_config(
    cfg, base: Settings, tenant_key: str, file_name: str = "",
) -> tuple[Settings | None, list[str], list[str]]:
    """Проверяет конфиг клиента и собирает его Settings.

    Возвращает (settings, problems, warnings): problems — фатальные причины
    (клиент не регистрируется и не записывается), warnings — предупреждения,
    не блокирующие работу. Один и тот же валидатор используется реестром при
    загрузке yaml и админ-API перед записью файла — битый конфиг не попадёт
    никуда.

    Провайдер клиента — поле provider ("wa" по умолчанию, "tg" или "zernio"):
      - wa: обязателен access_token (или глобальный WHATSAPP_ACCESS_TOKEN),
        tenant_key — phone_number_id (цифры);
      - tg: обязателен telegram_bot_token, tenant_key должен совпадать с id
        бота из токена (id бота — ключ клиента в реестре);
      - zernio: zernio_account_id (24 hex) — ключ маршрутизации, но он
        появляется только после подключения номера. Пока аккаунт не подключён,
        поле можно не задавать (warning): клиент создаётся, ссылка Embedded
        Signup выдаётся кнопкой в панели, а accountId подставляет
        «Проверить и сохранить». Имя файла — произвольный slug; глобальный
        ZERNIO_API_KEY один на всех клиентов.
    """
    if not isinstance(cfg, dict):
        return None, ["ожидается словарь с полями бизнеса"], []

    provider = str(cfg.get("provider") or "wa").strip().lower() or "wa"
    llm_block = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
    model = str(llm_block.get("model") or "").strip() or base.llm.model
    business_name = str(cfg.get("business_name") or "").strip()

    problems: list[str] = []
    warnings: list[str] = []
    if provider not in ("wa", "tg", "zernio"):
        problems.append(
            f"provider={provider!r} не поддерживается (ожидается 'wa', 'tg' или 'zernio')"
        )
    if not business_name:
        problems.append("business_name не задан")
    if not str(cfg.get("knowledge_base") or "").strip():
        problems.append("knowledge_base не задан")
    if not model:
        problems.append("llm.model не задан (yaml или LLM_MODEL в .env)")

    # Провайдер и его обязательный секрет/идентификатор. У WA есть глобальный
    # fallback-токен, у Telegram — нет, у Zernio ключ общий; accountId у Zernio
    # появляется после подключения номера, поэтому до него это лишь warning.
    access_token = ""
    telegram_bot_token = ""
    zernio_account_id = ""
    if provider == "tg":
        telegram_bot_token = str(cfg.get("telegram_bot_token") or "").strip()
        if not telegram_bot_token:
            problems.append("telegram_bot_token не задан (токен бота от @BotFather)")
        else:
            bot_id = bot_id_from_token(telegram_bot_token)
            if not bot_id:
                problems.append(
                    "telegram_bot_token не похож на токен бота (ожидается «123456789:AA…»)"
                )
            elif bot_id != tenant_key:
                problems.append(
                    f"ключ клиента {tenant_key} не совпадает с id бота {bot_id} из токена"
                )
    elif provider == "zernio":
        zernio_account_id = str(cfg.get("zernio_account_id") or "").strip().lower()
        if zernio_account_id and not is_valid_zernio_account_id(zernio_account_id):
            problems.append(
                "zernio_account_id не похож на id аккаунта Zernio "
                "(ожидается 24 hex-символа, например 66b2e19d8c3f5a7e9d0b1c2d)"
            )
        elif not zernio_account_id:
            warnings.append(
                "Zernio-аккаунт ещё не подключён: откройте клиента в панели и "
                "нажмите «Сгенерировать ссылку подключения», затем «Проверить и "
                "сохранить» — accountId подставится сам"
            )
    else:
        # Имя файла Meta-клиента — phone_number_id (цифры): это ключ маршрутизации.
        if not is_valid_phone_number_id(tenant_key):
            problems.append(
                f"для provider: wa имя файла должно быть phone_number_id (цифры), "
                f"сейчас {tenant_key!r}"
            )
        access_token = str(cfg.get("access_token") or "").strip() or base.whatsapp_access_token
        if not access_token:
            problems.append("access_token не задан (yaml или WHATSAPP_ACCESS_TOKEN в .env)")

    # Номер владельца в WhatsApp — только для WA; для TG используется chat id.
    if provider == "tg":
        owner_phone = None
        owner_chat, owner_warning = _resolve_owner_chat(cfg)
    else:
        owner_phone, owner_warning = _resolve_owner_phone(cfg, file_name)
        owner_chat = ""
    if owner_warning:
        warnings.append(owner_warning)
    if problems:
        return None, problems, warnings

    try:
        llm = LLMParams(
            model=model,
            temperature=float(llm_block.get("temperature", base.llm.temperature)),
            max_tokens=int(llm_block.get("max_tokens", base.llm.max_tokens)),
            timeout_seconds=int(llm_block.get("timeout_seconds", base.llm.timeout_seconds)),
            reasoning_effort=str(llm_block.get("reasoning_effort") or "").strip() or None,
        )
    except (TypeError, ValueError) as exc:
        return None, [f"некорректный блок llm: {exc}"], warnings

    settings = replace(
        base,
        # Транспорт тенанта задаётся его provider, а не глобальным MESSAGING_PROVIDER.
        messaging_provider={"tg": "telegram", "zernio": "zernio"}.get(provider, "meta"),
        # Ключ маршрутизации тенанта: у WA — phone_number_id (имя файла),
        # у TG — id бота из токена, у Zernio — accountId аккаунта.
        whatsapp_phone_number_id=zernio_account_id or tenant_key,
        whatsapp_access_token=access_token,
        zernio_account_id=zernio_account_id,
        telegram_bot_token=telegram_bot_token,
        telegram_webhook_secret=str(cfg.get("telegram_webhook_secret") or "").strip(),
        business_name=business_name,
        tone=str(cfg.get("tone") or "").strip(),
        language=str(cfg.get("language") or "ru").strip().lower(),
        knowledge_base=str(cfg.get("knowledge_base") or "").strip(),
        owner_phone=owner_phone,
        owner_telegram_chat_id=owner_chat,
        owner_template_name=str(cfg.get("owner_template_name") or "").strip(),
        owner_template_language=str(cfg.get("owner_template_language") or "").strip(),
        fallback_triggers=[
            str(t).strip() for t in (cfg.get("fallback_triggers") or []) if str(t).strip()
        ],
        llm=llm,
        style_examples=str(cfg.get("style_examples") or "").strip(),
        config_file=file_name,
        # Ручной режим, уведомления и флаги фич — из yaml клиента, с откатом
        # на базовые настройки сервиса, если ключа нет.
        pause_on=[str(p).strip() for p in (cfg.get("pause_on") or base.pause_on) if str(p).strip()],
        handoff_pauses_bot=bool(cfg.get("handoff_pauses_bot", base.handoff_pauses_bot)),
        manual_timeout_hours=_int_or(cfg.get("manual_timeout_hours"), base.manual_timeout_hours),
        timezone=str(cfg.get("timezone") or base.timezone or "").strip(),
        notify_channels=[str(c).strip() for c in (cfg.get("notify_channels") or base.notify_channels)],
        notify_on_no_answer=str(cfg.get("notify_on_no_answer") or base.notify_on_no_answer or "").strip(),
        minutes_per_reply=_int_or(cfg.get("minutes_per_reply"), base.minutes_per_reply),
        features=_features_of(cfg, base),
        fallback_reply_ru=str(cfg.get("fallback_reply_ru") or "").strip(),
        fallback_reply_kk=str(cfg.get("fallback_reply_kk") or "").strip(),
        timeout_reply_ru=str(cfg.get("timeout_reply_ru") or "").strip(),
        timeout_reply_kk=str(cfg.get("timeout_reply_kk") or "").strip(),
        media=MediaSettings(
            audio=bool((cfg.get("media") or {}).get("audio", getattr(base.media, "audio", True))),
            image=bool((cfg.get("media") or {}).get("image", getattr(base.media, "image", True))),
            max_audio_seconds=int((cfg.get("media") or {}).get("max_audio_seconds", getattr(base.media, "max_audio_seconds", 120))),
            max_image_mb=int((cfg.get("media") or {}).get("max_image_mb", getattr(base.media, "max_image_mb", 8))),
            daily_limit=int((cfg.get("media") or {}).get("daily_limit", getattr(base.media, "daily_limit", 50))),
        ),
        # Per-tenant объект не указывает на реестр — иначе create_app уйдёт в цикл.
        clients_dir="",
    )
    return settings, [], warnings


def load_tenant(path: Path, base: Settings) -> Settings | None:
    """YAML одного клиента -> его Settings (глобальные env уже внутри базы).

    Возвращает None, если файл не годится: битый yaml, не цифры в имени,
    пустые обязательные поля. Сервис при этом продолжает работать —
    один сломанный клиент не должен ронять остальных. Вся валидация —
    в validate_tenant_config, её же использует админ-API перед записью.
    """
    tenant_key = path.stem
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Клиент %s пропущен: не удалось прочитать yaml (%s)", path.name, exc)
        return None
    if not isinstance(cfg, dict):
        logger.warning("Клиент %s пропущен: ожидается YAML-словарь с полями бизнеса", path.name)
        return None

    settings, problems, warnings = validate_tenant_config(cfg, base, tenant_key, path.name)
    for warning in warnings:
        logger.warning("Клиент %s: %s", path.name, warning)
    if settings is None:
        logger.warning("Клиент %s пропущен: %s", path.name, "; ".join(problems))
        return None

    logger.info(
        "Клиент загружен | ключ=%s | провайдер=%s | бизнес=%s | файл=%s",
        tenant_key, settings.messaging_provider, settings.business_name, path.name,
    )
    return settings


def _resolve_owner_phone(cfg: dict, file_name: str) -> tuple[str | None, str | None]:
    """(номер владельца, предупреждение) из yaml клиента.

    OWNER_WHATSAPP_NUMBER здесь не применяется — глобальный override направил
    бы уведомления всех клиентов одному человеку. Опечатка — предупреждение,
    а не отказ: клиент без уведомлений лучше неработающего сервиса.
    """
    raw = str(cfg.get("owner_whatsapp_phone") or "").strip()
    if not raw:
        return None, None
    phone = normalize_phone(raw)
    if not re.fullmatch(r"\+\d{8,15}", phone):
        return None, (
            f"owner_whatsapp_phone={raw!r} не похож на номер телефона. "
            "Укажите его в формате +77770001122 (плюс, код страны, номер) — "
            "иначе уведомления владельцу доходить не будут"
        )
    return phone, None


def _resolve_owner_chat(cfg: dict) -> tuple[str, str | None]:
    """(chat id владельца в Telegram, предупреждение) из yaml TG-клиента.

    chat id — числовой (у личных чатов положительный, у групп отрицательный),
    поэтому допускаем ведущий минус. Опечатка — предупреждение, не отказ.
    """
    raw = str(cfg.get("owner_telegram_chat_id") or "").strip()
    if not raw:
        return "", None
    if not re.fullmatch(r"-?\d{5,20}", raw):
        return "", (
            f"owner_telegram_chat_id={raw!r} не похож на chat id Telegram. "
            "Это числовой id (например 123456789); узнать его можно у бота "
            "@userinfobot — иначе уведомления владельцу доходить не будут"
        )
    return raw, None


@dataclass(frozen=True)
class _FileInfo:
    """Снапшот одного клиентского файла для дешёвой проверки изменений."""

    mtime_ns: int
    size: int


class ClientRegistry:
    """Реестр клиентов из папки clients/: phone_number_id -> Settings.

    Снапшот папки (имя файла, mtime, размер) сверяется при каждом входящем
    событии — для 20+ файлов это дёшево. Изменился снапшот -> перечитываем
    yaml'ы; кто именно изменился, решает владелец бандлов (сравнение Settings).
    """

    def __init__(self, clients_dir: Path, base_settings: Settings) -> None:
        self.dir = clients_dir
        self.base = base_settings
        self.tenants: dict[str, Settings] = {}
        self._snapshot: dict[str, _FileInfo] | None = None
        self.maybe_reload()

    def maybe_reload(self) -> bool:
        """True, если реестр перечитан (папка менялась с прошлой проверки).

        Битый yaml, файл не по формату имени, дубли phone_number_id — клиент
        пропускается с warning; остальные клиенты продолжают работать.
        """
        snapshot = self._scan()
        if self._snapshot is not None and snapshot == self._snapshot:
            return False
        self._snapshot = snapshot
        self.tenants = self._load_all(snapshot)
        logger.info(
            "Реестр клиентов: %d клиент(ов) в %s",
            len(self.tenants), self.dir,
        )
        return True

    def _scan(self) -> dict[str, _FileInfo]:
        """Дешёвый срез папки: только клиентские yaml (имя -> mtime/размер)."""
        snapshot: dict[str, _FileInfo] = {}
        try:
            entries = list(os.scandir(self.dir))
        except OSError:
            return snapshot
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                name = entry.name
                if not name.lower().endswith((".yaml", ".yml")):
                    continue
                if name.startswith(EXAMPLE_PREFIX):
                    continue
                if not is_valid_client_id(Path(name).stem):
                    logger.warning(
                        "Файл %s в clients/ пропущен: имя файла — цифры "
                        "(Meta phone_number_id / Telegram bot id) или slug (Zernio)",
                        name,
                    )
                    continue
                stat = entry.stat()
                snapshot[name] = _FileInfo(mtime_ns=stat.st_mtime_ns, size=stat.st_size)
            except OSError:
                continue
        return snapshot

    def _load_all(self, snapshot: dict[str, _FileInfo]) -> dict[str, Settings]:
        tenants: dict[str, Settings] = {}
        for name in sorted(snapshot):
            settings = load_tenant(self.dir / name, self.base)
            if settings is None:
                continue
            pid = settings.whatsapp_phone_number_id
            if pid in tenants:
                logger.warning(
                    "Клиент %s пропущен: phone_number_id=%s уже зарегистрирован", name, pid
                )
                continue
            tenants[pid] = settings
        return tenants

