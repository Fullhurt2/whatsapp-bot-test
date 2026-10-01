# Тесты разбора токена Telegram-бота (чистые функции, без сети).
# Запуск: python tests/test_telegram_token.py

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from whatsapp.telegram_token import bot_id_from_token, is_valid_bot_token, parse_bot_token

TOKEN = "123456789:AAHhqwertyuiopasdfghjklzxcvbnm"

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed += 1
        print(f"  FAIL {name}")


def main():
    print("[1] валидный токен")
    check("parse вернул пару", parse_bot_token(TOKEN) is not None)
    check("id бота извлечён", bot_id_from_token(TOKEN) == "123456789")
    check("секрет отделён", parse_bot_token(TOKEN)[1] == "AAHhqwertyuiopasdfghjklzxcvbnm")
    check("is_valid=true", is_valid_bot_token(TOKEN))

    print("[2] пробелы по краям игнорируются")
    check("обрезка пробелов", bot_id_from_token("  " + TOKEN + "\n") == "123456789")

    print("[3] не-токены")
    check("пустая строка", not is_valid_bot_token(""))
    check("без двоеточия", not is_valid_bot_token("123456789AAHh"))
    check("короткий секрет", not is_valid_bot_token("123456789:short"))
    check("нецифровой id", not is_valid_bot_token("abc:AAHhqwertyuiopasdfghjklzxcvbnm"))
    check("bot_id пустой у мусора", bot_id_from_token("мусор") == "")

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
