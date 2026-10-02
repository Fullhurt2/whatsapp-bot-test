# Тесты пайплайна обработки (адаптация tests/test_history.py из chat-bot-demo):
# Telegram-фейки (FakeUpdate/FakeBot) заменены на FakeBird (фейковый sender) + handle_incoming.
# Запуск: python tests/test_pipeline.py

import asyncio
import dataclasses
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import logging
logging.basicConfig(level=logging.CRITICAL)

from config.settings import LLMParams, Settings
from handlers.message_handler import MessageProcessor
from services.fallback import extract_booking_summary
from services.llm_client import LLMTimeout
from whatsapp.errors import MessagingError

OWNER_PHONE = "+70000000000"
CLIENT_PHONE = "+77770000001"


# ---------- Фейки ----------
class FakeBird:
    """Подменяет ZernioWhatsAppClient: собирает отправки, умеет ломаться."""

    def __init__(self):
        self.sent = []          # список (to, text)
        self.fail_for = set()   # номера, для которых send_text бросает MessagingError

    async def send_text(self, to, text, conversation_id=""):
        if to in self.fail_for:
            # Тот же тип исключения, что у реального клиента: путь except MessagingError.
            raise MessagingError("fake zernio failure")
        self.sent.append((to, text))


class StubLLM:
    """Подменяет LLMClient: фиксирует вызовы и раздаёт заготовки ответов."""

    def __init__(self, replies, default="ответ", delay=0.0):
        self.replies = list(replies)
        self.default = default
        self.calls = []
        self.in_call = False
        self.overlap = False
        self._delay = delay

    async def chat(self, system_prompt, user_message, history=None):
        if self.in_call:
            self.overlap = True
        self.in_call = True
        try:
            if self._delay:
                await asyncio.sleep(self._delay)
            r = self.replies.pop(0) if self.replies else self.default
            if isinstance(r, Exception):
                raise r
            self.calls.append({"q": user_message, "history": list(history or [])})
            return r
        finally:
            self.in_call = False


SlowStubLLM = lambda replies: StubLLM(replies, delay=0.15)

# Настройки целиком из кода: тесты не зависят от .env и конфига клиента.
def _base_settings():
    return Settings(
        messaging_provider="zernio",
        whatsapp_access_token="",
        whatsapp_phone_number_id="",
        meta_app_secret="",
        meta_verify_token="",
        meta_graph_version="v21.0",
        zernio_api_key="test-key",
        zernio_webhook_secret="whsec_test",
        zernio_base_url="https://zernio.com/api/v1",
        zernio_account_id="66b2e19d8c3f5a7e9d0b1c2d",
        app_host="127.0.0.1",
        app_port=8000,
        llm_api_url="https://llm.test/v1",
        llm_api_key="test",
        business_name="Тестовый Бизнес",
        tone="вежливый",
        language="auto",
        knowledge_base="Часы работы: 10:00-20:00.",
        owner_phone=OWNER_PHONE,
        fallback_triggers=["жалоба", "хочу человека"],
        llm=LLMParams(model="test-model", temperature=0.6, max_tokens=100,
                      timeout_seconds=15, reasoning_effort=None),
    )


BASE_SETTINGS = _base_settings()


def make_processor(replies, cls=StubLLM, bird=None, sender=None):
    return MessageProcessor(BASE_SETTINGS, cls(replies), bird or sender or FakeBird())


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


async def main():
    print("[1] история: базовый сценарий")
    proc1 = make_processor(["Капучино 1200 ₸", "Латте 1300 ₸"])
    await proc1.handle_incoming(CLIENT_PHONE, "Аня", "Сколько стоит капучино?")
    await proc1.handle_incoming(CLIENT_PHONE, "Аня", "А латте сколько?")
    calls = proc1.llm.calls
    check("2 вызова LLM", len(calls) == 2)
    check("1-й вызов: история пуста", calls[0]["history"] == [])
    h = calls[1]["history"]
    check("2-й вызов: история = 2 записи", len(h) == 2)
    check("роли user/assistant", [m["role"] for m in h] == ["user", "assistant"])
    check("текст 1-го вопроса в истории", h[0]["content"] == "Сколько стоит капучино?")
    check("клиенту ушло 2 ответа", len([1 for to, _ in proc1.sender.sent if to == CLIENT_PHONE]) == 2)

    print("[2] HANDOFF не утекает в историю")
    proc2 = make_processor(["[HANDOFF] нет данных в базе"])
    await proc2.handle_incoming(CLIENT_PHONE, "Аня", "Кто победит в финале ЛЧ?")
    h2 = proc2._history_for(CLIENT_PHONE)
    check("в истории 2 записи", len(h2) == 2)
    check("fallback_reply в истории", "Передаю ваш вопрос" in h2[-1]["content"])
    check("[HANDOFF] не в истории", all("[HANDOFF]" not in m["content"] for m in h2))

    print("[3] keyword-fallback: без вызова LLM")
    proc3 = make_processor([])
    await proc3.handle_incoming(CLIENT_PHONE, "Аня", "Хочу человека")
    check("LLM не вызван", len(proc3.llm.calls) == 0)
    check("пара в истории", len(proc3._history_for(CLIENT_PHONE)) == 2)
    owner3 = [t for to, t in proc3.sender.sent if to == OWNER_PHONE]
    check("владелец уведомлён по ключевому слову", len(owner3) == 1)

    print("[4] обрезка длинных сообщений")
    proc4 = make_processor(["ок", "ок"])
    await proc4.handle_incoming(CLIENT_PHONE, "Аня", "А" * 5000)
    await proc4.handle_incoming(CLIENT_PHONE, "Аня", "ещё вопрос")
    first = proc4.llm.calls[1]["history"][0]["content"]
    check(f"длина обрезана ({len(first)} <= 702)", len(first) <= 702)
    check("многоточие", first.endswith("…"))

    print("[5] LRU-лимит диалогов")
    proc5 = make_processor([])
    phones = [f"+7777{1000 + i:04d}" for i in range(600)]
    for p in phones:
        await proc5.handle_incoming(p, "Клиент", "привет")
    check("словарь <= 500", len(proc5._histories) <= 500)
    check("самый старый чат вытеснен", phones[0] not in proc5._histories)
    check("последний чат на месте", phones[-1] in proc5._histories)

    print("[6] лок чата: гонка без перекрытий")
    proc6 = make_processor(["от1", "от2"], cls=SlowStubLLM)
    llm6 = proc6.llm
    await asyncio.gather(
        proc6.handle_incoming(CLIENT_PHONE, "Аня", "первое"),
        proc6.handle_incoming(CLIENT_PHONE, "Аня", "второе"),
    )
    check("нет перекрытия вызовов LLM", not llm6.overlap)
    roles6 = [m["role"] for m in proc6._history_for(CLIENT_PHONE)]
    check("порядок user/assistant/user/assistant",
          roles6 == ["user", "assistant", "user", "assistant"])

    print("[7] приветствие: сброс истории и ответ клиенту")
    proc7 = make_processor(["раз"])
    await proc7.handle_incoming(CLIENT_PHONE, "Аня", "привет")
    await proc7.handle_greeting(CLIENT_PHONE)
    check("история очищена", len(proc7._histories.get(CLIENT_PHONE, ())) == 0)
    greetings = [t for to, t in proc7.sender.sent if "Здравствуйте" in t]
    check("приветствие ушло клиенту", len(greetings) >= 1)

    print("[8] timeout-путь")
    proc8 = make_processor([LLMTimeout("t/o")])
    await proc8.handle_incoming(CLIENT_PHONE, "Аня", "привет")
    h8 = proc8._history_for(CLIENT_PHONE)
    check("timeout_reply в истории", "уточняю" in h8[-1]["content"])
    owner8 = [t for to, t in proc8.sender.sent if to == OWNER_PHONE]
    check("владелец уведомлён при таймауте", len(owner8) == 1)

    print("[9] чанкинг длинного ответа: не в процессоре, а в ZernioWhatsAppClient (см. test_zernio_client.py)")
    # Здесь проверяем только то, что длинный ответ целиком передан клиенту.
    proc9 = make_processor(["x" * 9000])
    await proc9.handle_incoming(CLIENT_PHONE, "Аня", "дай длинный ответ")
    client9 = [t for to, t in proc9.sender.sent if to == CLIENT_PHONE]
    check("ответ ушёл целиком одним вызовом send_text",
          len(client9) == 1 and client9[0] == "x" * 9000)

    print("[10] запись: владельцу уходит сводка «ЗАПИСЬ», а не сырое сообщение")
    proc10 = make_processor([
        "Отлично! На какой день и время вам удобно? 💅",
        "[HANDOFF]\nЗАПИСЬ: услуга — маникюр, желаемое время — завтра 15:00\n"
        "Передаю мастеру, она подтвердит время и адрес.",
    ])
    await proc10.handle_incoming(CLIENT_PHONE, "Бота", "хочу записаться на маникюр")
    await proc10.handle_incoming(CLIENT_PHONE, "Клиент", "завтра в 15")
    owner10 = [t for to, t in proc10.sender.sent if to == OWNER_PHONE]
    check("уведомление владельцу отправлено", len(owner10) == 1)
    check("владельцу ушла именно сводка",
          "Сообщение: ЗАПИСЬ: услуга — маникюр, желаемое время — завтра 15:00" in (owner10[-1] if owner10 else ""))
    check("сырое «завтра в 15» не ушло", all("Сообщение: завтра в 15" not in t for t in owner10))
    check("клиенту — вежливая передача",
          "Передаю ваш вопрос" in proc10._history_for(CLIENT_PHONE)[-1]["content"])

    print("[11] handoff без сводки: владельцу уходит исходный текст")
    proc11 = make_processor(["[HANDOFF] Не могу проверить статус заказа"])
    await proc11.handle_incoming(CLIENT_PHONE, "Валя", "где мой заказ")
    owner11 = [t for to, t in proc11.sender.sent if to == OWNER_PHONE][-1]
    check("сырой текст клиента", "Сообщение: где мой заказ" in owner11)
    check("сводки нет", "ЗАПИСЬ:" not in owner11)

    print("[12] сбой отправки клиенту: ответ не в истории, владелец уведомлён")
    bad_bird = FakeBird()
    bad_bird.fail_for.add(CLIENT_PHONE)
    proc12 = make_processor(["хороший ответ"], sender=bad_bird)
    await proc12.handle_incoming(CLIENT_PHONE, "Аня", "вопрос")
    check("недоставленный ответ не в истории", len(proc12._histories.get(CLIENT_PHONE, ())) == 1)
    proc12b = make_processor([LLMTimeout("t/o")], sender=bad_bird)
    await proc12b.handle_incoming(CLIENT_PHONE, "Аня", "расскажи про доставку")
    owner12 = [t for to, t in bad_bird.sent if to == OWNER_PHONE]
    check("владелец уведомлён, хотя ответ клиенту не ушёл", len(owner12) == 1)

    print("[13] extract_booking_summary: граничные случаи")
    check("сводка в следующей строке",
          extract_booking_summary("[HANDOFF]\nЗАПИСЬ: услуга — X\nпояснение") == "ЗАПИСЬ: услуга — X")
    check("сводка в той же строке",
          extract_booking_summary("[HANDOFF] ЗАПИСЬ: услуга — X") == "ЗАПИСЬ: услуга — X")
    check("пустая строка между токеном и сводкой",
          extract_booking_summary("[HANDOFF]\n\nЗАПИСЬ: услуга — X") == "ЗАПИСЬ: услуга — X")
    check("пояснение без сводки", extract_booking_summary("[HANDOFF]\nне знаю ответа") is None)
    check("без токена", extract_booking_summary("ЗАПИСЬ: услуга — X") is None)
    check("«ЗАПИСЬ» не в начале пояснения",
          extract_booking_summary("[HANDOFF]\nвот ЗАПИСЬ: услуга — X") is None)

    print(f"\nИТОГО: passed={passed}, failed={failed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
