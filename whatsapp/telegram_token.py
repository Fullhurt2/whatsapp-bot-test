"""Разбор токена Telegram-бота: чистые функции без сети и зависимостей.

Токен BotFather выглядит как `123456789:AAHh...` — цифровой id бота, двоеточие
и секретная часть. id бота уникален и состоит из цифр, поэтому он же служит
ключом клиента в реестре clients/ (имя файла <bot_id>.yaml) — ровно как
phone_number_id у WhatsApp-клиентов.
"""

import re

# id бота (5–15 цифр) : секрет (base64url-подобный, обычно длиннее 30 символов).
_TOKEN_RE = re.compile(r"^(\d{5,15}):([A-Za-z0-9_-]{20,})$")


def parse_bot_token(token: str) -> tuple[str, str] | None:
    """(bot_id, secret) из токена бота или None, если строка не похожа на токен."""
    match = _TOKEN_RE.fullmatch(str(token or "").strip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def bot_id_from_token(token: str) -> str:
    """id бота (цифры до ':') или "" — ключ клиента Telegram в реестре."""
    parsed = parse_bot_token(token)
    return parsed[0] if parsed else ""


def is_valid_bot_token(token: str) -> bool:
    """Похож ли токен на настоящий токен бота Telegram."""
    return parse_bot_token(token) is not None
