"""Админ-API: правка клиентских конфигов clients/*.yaml без деплоя и SSH.

Модульный интерфейс админ-панели и API.
Маршруты вынесены в модули admin/routers/:
- auth: авторизация, страница панели, whoami
- clients: список, просмотр, обновление, профиль, привязка Zernio
- chats: живой чат, сообщения, медиа, переключение режимов
- analytics: статистика, экспорт CSV, вопросы без ответа, привязка Telegram
- playground: тестирование промптов и LLM
"""

import logging
from pathlib import Path

from config.settings import Settings
from admin.routers.common import (
    ADMIN_TOKEN_MIN_LENGTH,
    HEADER_ADMIN,
    HEADER_CLIENT,
    RateLimitMiddleware,
    CSPMiddleware,
    _authorize,
    _merge_incoming,
    tokens_equal,
    mask_secret,
    hash_token,
    verify_token_hash,
    TOKEN_HASH_PREFIX,
    TOKEN_PBKDF2_PREFIX,
    mask_token_in_log,
    _failed_auth,
    check_rate_limit,
    record_failed_auth,
    get_client_ip,
    _client_yaml_path,
    _client_pids,
    _read_cfg,
    _management_token_of,
    _client_provider,
    _atomic_write,
    _backup,
    _audit,
    _incoming_differs,
    _client_editable_cfg,
    _conversation_client_key,
    _conversation_client,
    _check_conv_ownership,
)
from admin.routers.clients import (
    _setup_telegram_webhook,
    _looks_like_email,
    _validate_profile_fields,
    _profile_client,
    _profile_meta_client,
    _profile_zernio_client,
    _make_meta_client,
    _profile_failure,
    _zernio_api_client,
    _validate_redirect_url,
)
from admin.routers.analytics import (
    _kb_has_question,
    _append_answers_to_kb,
    _parse_groups_json,
)
from admin.routers import (
    register_auth_routes,
    register_clients_routes,
    register_chats_routes,
    register_analytics_routes,
    register_playground_routes,
)

logger = logging.getLogger(__name__)


def register_admin_api(app, settings: Settings, state) -> None:
    """Регистрирует админ-роуты и страницу /admin.

    Отключено (роуты не существуют -> 404), если нет ADMIN_TOKEN или сервис
    работает вне мультитенанта — админ-поверхность просто не появляется.
    Медиа-роуты проверяют media_path на path traversal через is_relative_to.
    Все эндпоинты валидируют JSON-тело: isinstance(incoming, dict).
    """
    if not state.multitenant:
        if settings.admin_token:
            logger.warning(
                "ADMIN_TOKEN задан, но админ-API работает только в мультитенанте "
                "(clients/) — роуты отключены"
            )
        return
    if not settings.admin_token:
        logger.warning("ADMIN_TOKEN не задан — админ-API и панель /admin отключены")
        return
    if len(settings.admin_token) < ADMIN_TOKEN_MIN_LENGTH:
        logger.warning(
            "ADMIN_TOKEN короче %d символов — для продакшена сгенерируйте длиннее",
            ADMIN_TOKEN_MIN_LENGTH,
        )

    clients_dir = Path(settings.clients_dir)

    # Регистрация модулей роутеров
    register_auth_routes(app, settings, clients_dir)
    register_clients_routes(app, settings, state, clients_dir)
    register_chats_routes(app, settings, state, clients_dir)
    register_analytics_routes(app, settings, state, clients_dir)
    register_playground_routes(app, settings, state, clients_dir)
