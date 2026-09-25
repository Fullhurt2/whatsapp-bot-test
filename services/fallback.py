"""Логика определения «передать человеку» (fallback).

Два источника сигнала:
1. Ключевые слова из конфига клиента (fallback_triggers) в тексте сообщения.
2. Токен [HANDOFF] в ответе модели — инструкция об этом зашита в system prompt;
   модель начинает с него ответ, когда не может ответить из базы знаний.

Ответ модели по [HANDOFF] может начинаться со сводки «ЗАПИСЬ: …» —
она описывает запись (услуга, время) для мастера и уходит владельцу
вместо сырого последнего сообщения клиента.
"""

# Токен, который модель ставит в начале ответа при неуверенности.
HANDOFF_TOKEN = "[HANDOFF]"

# Префикс структурированной сводки по записи/бронированию (сразу после [HANDOFF]).
BOOKING_PREFIX = "ЗАПИСЬ:"


def find_trigger(message_text: str, triggers: list[str]) -> str | None:
    """Возвращает первый сработавший триггер из списка или None."""
    if not message_text or not triggers:
        return None
    lowered = message_text.casefold()
    for trigger in triggers:
        if trigger.casefold() in lowered:
            return trigger
    return None


def response_is_handoff(llm_response: str) -> bool:
    """True, если модель сигнализирует о неуверенности токеном [HANDOFF].

    По ТЗ токен ставится в начале ответа; для надёжности проверяем его
    в любом месте ответа — так «техническая» часть гарантированно
    не уйдёт клиенту.
    """
    return HANDOFF_TOKEN in llm_response


def extract_booking_summary(llm_response: str) -> str | None:
    """Достаёт сводку «ЗАПИСЬ: …» из ответа модели с [HANDOFF].

    Сводка должна идти сразу после [HANDOFF] (в той же строке или следующей
    непустой строкой), например:

        [HANDOFF]
        ЗАПИСЬ: услуга — маникюр, желаемое время — завтра 15:00

    Возвращает всю строку сводки как есть или None, если её нет
    (обычный handoff, например по жалобе — уходит сырой текст клиента).
    """
    if not llm_response:
        return None

    lines = llm_response.splitlines()
    for i, line in enumerate(lines):
        if HANDOFF_TOKEN not in line:
            continue
        # [HANDOFF] и сводка могут оказаться в одной строке.
        tail = line.split(HANDOFF_TOKEN, 1)[1].strip()
        if _is_booking_summary(tail):
            return tail
        # Иначе смотрим первую непустую строку после токена: если это не
        # сводка — значит её нет, и объяснение для оператора идёт сразу.
        for next_line in lines[i + 1:]:
            candidate = next_line.strip()
            if candidate:
                return candidate if _is_booking_summary(candidate) else None
    return None


def _is_booking_summary(line: str) -> bool:
    """True, если строка начинается с маркера сводки «ЗАПИСЬ:»."""
    return line.casefold().startswith(BOOKING_PREFIX.casefold())
