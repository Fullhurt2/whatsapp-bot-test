"""Реестр клиентов мультитенантного режима: папка clients/, один YAML на клиента.

Имя файла = phone_number_id бизнес-номера клиента в Graph API, например
clients/1354249714436396.yaml. Из файла собирается полноценный Settings:
бизнес-поля и access_token — из yaml, глобальные вещи (LLM-ключ, app secret,
verify token, версия Graph API) — из env. Так MessageProcessor и notify_owner
работают с per-tenant Settings без изменений в коде обработчиков.

Требования к файлу клиента:
  - имя файла — цифры (phone_number_id), уникальный ключ клиента;
  - business_name и knowledge_base обязательны;
  - llm.model — в yaml или глобальный LLM_MODEL из env;
  - access_token — в yaml или глобальный WHATSAPP_ACCESS_TOKEN (fallback для
    схемы «все номера под партнёрством и одним общим токеном»).
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

from config.settings import LLMParams, Settings, normalize_phone

logger = logging.getLogger(__name__)

# phone_number_id в Graph API — числовой идентификатор; имя файла клиента = он же.
_PHONE_NUMBER_ID_RE = re.compile(r"\d{1,20}")

# Файлы-образцы (префикс "_") в реестр не попадают.
EXAMPLE_PREFIX = "_"

YAML_SUFFIXES = (".yaml", ".yml")


def is_client_file(path: Path) -> bool:
    """Похоже ли имя файла на ключ клиента: <цифры>.yaml (не _example.yaml)."""
    return (
        path.suffix.lower() in YAML_SUFFIXES
        and not path.name.startswith(EXAMPLE_PREFIX)
        and bool(_PHONE_NUMBER_ID_RE.fullmatch(path.stem))
    )


def is_valid_phone_number_id(value: str) -> bool:
    """Строка похожа на phone_number_id: только цифры (ключ клиента)."""
    return bool(_PHONE_NUMBER_ID_RE.fullmatch(value))


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
    cfg, base: Settings, phone_number_id: str, file_name: str = "",
) -> tuple[Settings | None, list[str], list[str]]:
    """Проверяет конфиг клиента и собирает его Settings.

    Возвращает (settings, problems, warnings): problems — фатальные причины
    (клиент не регистрируется и не записывается), warnings — предупреждения,
    не блокирующие работу. Один и тот же валидатор используется реестром при
    загрузке yaml и админ-API перед записью файла — битый конфиг не попадёт
    никуда.
    """
    if not isinstance(cfg, dict):
        return None, ["ожидается словарь с полями бизнеса"], []

    llm_block = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
    access_token = str(cfg.get("access_token") or "").strip() or base.whatsapp_access_token
    model = str(llm_block.get("model") or "").strip() or base.llm.model
    business_name = str(cfg.get("business_name") or "").strip()

    problems: list[str] = []
    if not business_name:
        problems.append("business_name не задан")
    if not str(cfg.get("knowledge_base") or "").strip():
        problems.append("knowledge_base не задан")
    if not access_token:
        problems.append("access_token не задан (yaml или WHATSAPP_ACCESS_TOKEN в .env)")
    if not model:
        problems.append("llm.model не задан (yaml или LLM_MODEL в .env)")

    owner_phone, owner_warning = _resolve_owner_phone(cfg, file_name)
    warnings = [owner_warning] if owner_warning else []
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
        whatsapp_phone_number_id=phone_number_id,
        whatsapp_access_token=access_token,
        business_name=business_name,
        tone=str(cfg.get("tone") or "").strip(),
        language=str(cfg.get("language") or "ru").strip().lower(),
        knowledge_base=str(cfg.get("knowledge_base") or "").strip(),
        owner_phone=owner_phone,
        fallback_triggers=[
            str(t).strip() for t in (cfg.get("fallback_triggers") or []) if str(t).strip()
        ],
        llm=llm,
        style_examples=str(cfg.get("style_examples") or "").strip(),
        config_file=file_name,
        fallback_reply_ru=str(cfg.get("fallback_reply_ru") or "").strip(),
        fallback_reply_kk=str(cfg.get("fallback_reply_kk") or "").strip(),
        timeout_reply_ru=str(cfg.get("timeout_reply_ru") or "").strip(),
        timeout_reply_kk=str(cfg.get("timeout_reply_kk") or "").strip(),
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
    phone_number_id = path.stem
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("Клиент %s пропущен: не удалось прочитать yaml (%s)", path.name, exc)
        return None
    if not isinstance(cfg, dict):
        logger.warning("Клиент %s пропущен: ожидается YAML-словарь с полями бизнеса", path.name)
        return None

    settings, problems, warnings = validate_tenant_config(cfg, base, phone_number_id, path.name)
    for warning in warnings:
        logger.warning("Клиент %s: %s", path.name, warning)
    if settings is None:
        logger.warning("Клиент %s пропущен: %s", path.name, "; ".join(problems))
        return None

    logger.info(
        "Клиент загружен | phone_number_id=%s | бизнес=%s | файл=%s",
        phone_number_id, settings.business_name, path.name,
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
            f"owner_whatsapp_phone={raw!r} не похож на номер (E.164) — "
            "уведомления владельцу работать не будут"
        )
    return phone, None


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
                if not _PHONE_NUMBER_ID_RE.fullmatch(Path(name).stem):
                    logger.warning("Файл %s в clients/ пропущен: имя файла должно быть phone_number_id (цифры)", name)
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

