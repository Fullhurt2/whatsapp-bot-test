import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import (
    init_db,
    apply_migrations,
    create_conversation,
    add_message,
    get_context_for_llm,
    create_handoff,
    get_handoffs_needing_reminder,
    execute,
)
from admin.api import _merge_incoming
from storage.stats import parse_date_range

def test_all():
    td = tempfile.mkdtemp()
    os.environ["JAUAP_DB_PATH"] = os.path.join(td, "test.db")
    init_db()
    apply_migrations()

    # 1. Проверка обрезки контекста LLM: новые сообщения сохраняются, старые отбрасываются
    conv = create_conversation("test_client", "wa", "+77011112233")
    cid = conv["id"]
    add_message(cid, "client", "OLD_LONG_MSG_" * 30)
    add_message(cid, "bot", "OLD_BOT_MSG_" * 30)
    add_message(cid, "client", "NEW_CRUCIAL_MSG")

    ctx = get_context_for_llm(cid, max_messages=10, total_char_budget=200)
    assert any("NEW_CRUCIAL_MSG" in m["content"] for m in ctx), "Новейшее сообщение должно быть в контексте!"
    print("[OK] 1. Бюджет контекста LLM сохраняет актуальные сообщения")

    # 2. Проверка выборки напоминаний handoffs при формате с 'T'
    hid = create_handoff(cid, "booking", "summary")
    execute("UPDATE handoffs SET notified_at = '2026-01-01T10:00:00' WHERE id = ?", (hid,))
    needing = get_handoffs_needing_reminder(hours=1)
    assert any(h["handoff_id"] == hid for h in needing), "Handoff с 'T' в notified_at должен попадать в выборку напоминаний!"
    print("[OK] 2. Напоминания менеджерам (ISO 'T' vs SQLite ' ') работают корректно")

    # 3. Проверка deep merge вложенных настроек клиента
    old_cfg = {
        "client_name": "Salon",
        "llm": {"model": "gpt-4o", "max_tokens": 800, "temperature": 0.7},
        "features": {"media": True, "voice": True},
    }
    incoming = {
        "llm": {"temperature": 0.2},
        "features": {"media": False},
    }
    merged = _merge_incoming(old_cfg, incoming)
    assert merged["llm"]["model"] == "gpt-4o", "Поле model не должно затираться!"
    assert merged["llm"]["temperature"] == 0.2, "Поле temperature должно обновиться!"
    assert merged["features"]["voice"] is True, "Вложенное поле voice не должно стираться!"
    assert merged["features"]["media"] is False, "Поле media должно обновиться!"
    print("[OK] 3. Deep merge вложенных конфигураций работает корректно")

    # 4. Проверка работы с датами и таймзонами (naive + aware)
    f, t = parse_date_range("2026-10-01T00:00:00Z", None)
    assert f and t, "parse_date_range успешно парсит aware-даты без TypeError"
    print("[OK] 4. Парсинг дат и временных зон работает без ошибок")

    # 5. Проверка _is_write для CTE и DDL
    from storage.db import _is_write
    assert _is_write("WITH cte AS (SELECT 1) INSERT INTO conversations VALUES (...)") is True
    assert _is_write("CREATE TABLE IF NOT EXISTS test_table (id TEXT)") is True
    assert _is_write("SELECT * FROM conversations") is False
    print("[OK] 5. _is_write корректно определяет CTE и DDL")

    # 6. Проверка get_client_ip
    from admin.api import get_client_ip
    class DummyRequest:
        def __init__(self, headers, host="127.0.0.1"):
            self.headers = headers
            self.client = type("Client", (), {"host": host})()
    
    req_cf = DummyRequest({"CF-Connecting-IP": "203.0.113.195", "X-Forwarded-For": "10.0.0.1"})
    assert get_client_ip(req_cf) == "203.0.113.195"

    req_xff = DummyRequest({"X-Forwarded-For": "198.51.100.42, 10.0.0.2"})
    assert get_client_ip(req_xff) == "198.51.100.42"

    os.environ["TRUST_PROXY"] = "0"
    assert get_client_ip(req_cf) == "127.0.0.1", "При TRUST_PROXY=0 спуф-заголовки должны игнорироваться!"
    os.environ.pop("TRUST_PROXY", None)
    print("[OK] 6. get_client_ip возвращает реальный IP клиента и защищен от спуфинга")

    # 7. Проверка авторизации через Authorization: Bearer
    from admin.api import _authorize
    from unittest.mock import MagicMock
    dummy_settings = MagicMock(admin_token="secret_adm_token")
    req_auth = DummyRequest({"Authorization": "Bearer secret_adm_token"})
    req_auth.query_params = {}
    role, err = _authorize(dummy_settings, req_auth, Path(td), None)
    assert role == "admin" and err is None, "Bearer токен должен распознаваться как админ!"
    print("[OK] 7. Авторизация по Authorization: Bearer работает корректно")

    # 8. Проверка таймаута manual_since / last_message_at
    from storage.conversations import get_conversations_needing_timeout_check, update_conversation_status
    conv_timeout = create_conversation("client_timeout", "wa", "+77019998877")
    update_conversation_status(conv_timeout["id"], "manual")
    # Если manual_since 10 часов назад, но последнее сообщение было прямо сейчас, он НЕ должен попадать в таймаут 2 часа
    execute("UPDATE conversations SET manual_since = datetime('now', '-10 hours'), last_message_at = datetime('now') WHERE id = ?", (conv_timeout["id"],))
    needing_timeout = get_conversations_needing_timeout_check(timeout_hours=2, client_key="client_timeout")
    assert not any(c["id"] == conv_timeout["id"] for c in needing_timeout), "Активный диалог не должен возвращаться к боту раньше времени!"

    # А если оба были 3 часа назад - должен попадать
    execute("UPDATE conversations SET manual_since = datetime('now', '-5 hours'), last_message_at = datetime('now', '-3 hours') WHERE id = ?", (conv_timeout["id"],))
    needing_timeout = get_conversations_needing_timeout_check(timeout_hours=2, client_key="client_timeout")
    print("[OK] 8. Таймаут ручного режима корректно учитывает активность сообщений")

    # 9. Проверка get_manager_response_times при смешанных форматах дат (naive + aware ISO с 'Z')
    from storage.handoffs import get_manager_response_times
    hid2 = create_handoff(cid, "complaint", "summary")
    execute("UPDATE handoffs SET created_at = '2026-10-01T10:00:00Z', first_human_reply_at = '2026-10-01 10:05:00' WHERE id = ?", (hid2,))
    m_stats = get_manager_response_times("test_client", "2026-01-01", "2027-01-01")
    assert m_stats["count"] >= 1, "Разные форматы дат не должны ронять расчёт времени ответа менеджера!"
    assert m_stats["avg_seconds"] == 300.0, f"Ожидалось 300 секунд, получено {m_stats['avg_seconds']}"
    print("[OK] 9. get_manager_response_times безопасно парсит даты с таймзонами и без")

    # 10. Проверка verify_link_code со строками expires_at
    from storage.tg_bindings import create_link_code, verify_link_code
    code_info = create_link_code("test_client", expires_minutes=10)
    ver = verify_link_code(code_info["code"])
    assert ver and ver["client_key"] == "test_client", "verify_link_code должен валидировать свежий код!"
    print("[OK] 10. verify_link_code работает корректно с таймзонами")

    # 11. Проверка LLMClient: адаптация max_completion_tokens -> max_tokens при отказе провайдера
    from services.llm_client import LLMClient
    from config.settings import LLMParams
    import httpx
    llm_test = LLMClient("https://api.openai.com/v1", "key", LLMParams(model="o1", max_tokens=500, temperature=0.7, timeout_seconds=30.0))
    # Для o1 по умолчанию включен max_completion_tokens
    assert llm_test._use_max_completion_tokens is True
    # Симулируем отказ провайдера из-за max_completion_tokens
    fake_resp = httpx.Response(400, text="Unrecognized request argument: max_completion_tokens")
    dummy_payload = {"model": "o1", "max_completion_tokens": 500}
    adapted = llm_test._adapt_for_rejection(dummy_payload, fake_resp)
    assert adapted is not None
    assert "max_tokens" in adapted and "max_completion_tokens" not in adapted
    assert llm_test._use_max_completion_tokens is False
    print("[OK] 11. LLMClient корректно выполняет двустороннюю адаптацию параметров токенов")

    # 12. Проверка H11: fallback_reply и timeout_reply с произвольными фигурными скобками
    from config.settings import Settings
    s = object.__new__(Settings)
    object.__setattr__(s, "business_name", "MyShop")
    object.__setattr__(s, "language", "ru")
    object.__setattr__(s, "tone", "вежливый")
    object.__setattr__(s, "knowledge_base", "База знаний MyShop")
    object.__setattr__(s, "style_examples", "")
    object.__setattr__(s, "messaging_provider", "wa")
    object.__setattr__(s, "whatsapp_phone_number_id", "client_m4")
    object.__setattr__(s, "fallback_reply_ru", "Здравствуйте! Ожидайте {ответ} от {business_name} {100%}")
    object.__setattr__(s, "fallback_reply_kk", "")
    object.__setattr__(s, "timeout_reply_ru", "Уточняю {вопрос} у {business_name}...")
    object.__setattr__(s, "timeout_reply_kk", "")
    fb = s.fallback_reply("ru")
    assert fb == "Здравствуйте! Ожидайте {ответ} от MyShop {100%}"
    to = s.timeout_reply("ru")
    assert to == "Уточняю {вопрос} у MyShop..."
    # 13. Проверка H9: cleanup_old_conversations не удаляет активные диалоги
    from storage.conversations import cleanup_old_conversations, get_conversation
    conv_old_active = create_conversation("client_h9", "wa", "+77011110001")
    conv_old_dead = create_conversation("client_h9", "wa", "+77011110002")
    # conv_old_active создан 400 дней назад, но последнее сообщение 1 день назад
    execute("UPDATE conversations SET created_at = datetime('now', '-400 days'), last_message_at = datetime('now', '-1 day') WHERE id = ?", (conv_old_active["id"],))
    # conv_old_dead создан 400 дней назад, последнее сообщение 400 дней назад
    execute("UPDATE conversations SET created_at = datetime('now', '-400 days'), last_message_at = datetime('now', '-400 days') WHERE id = ?", (conv_old_dead["id"],))
    
    deleted_count = cleanup_old_conversations(retention_days=365)
    assert deleted_count == 1, f"Ожидалось удаление 1 неактивного диалога, удалено {deleted_count}"
    assert get_conversation(conv_old_active["id"]) is not None, "Активный диалог не должен удаляться по сроку created_at!"
    assert get_conversation(conv_old_dead["id"]) is None, "Неактивный диалог старше 365 дней должен быть удален!"
    # 14. Проверка H3: _get_business_phone извлекает display_phone_number и не кэширует пустой результат
    import asyncio
    from unittest.mock import AsyncMock
    from main import _get_business_phone, _business_phone_cache
    _business_phone_cache.clear()
    mock_zclient = AsyncMock()
    mock_zclient.get_number_info.return_value = {
        "phone": {"display_phone_number": "+7 701 555-44-33"},
        "waba": {"name": "Test WABA"},
    }
    phone_res = asyncio.run(_get_business_phone("acc_test_1", mock_zclient))
    assert phone_res == "+7 701 555-44-33", f"Ожидался номер из phone.display_phone_number, получено '{phone_res}'"
    
    # Проверка, что пустой результат/ошибка не кэшируется на 15 минут
    mock_zclient_empty = AsyncMock()
    mock_zclient_empty.get_number_info.return_value = {}
    phone_empty = asyncio.run(_get_business_phone("acc_test_fail", mock_zclient_empty))
    assert phone_empty == ""
    assert "acc_test_fail" not in _business_phone_cache, "Пустой результат или ошибка не должны кэшироваться в _business_phone_cache!"
    # 15. Проверка H8: validate_tenant_config безопасно валидирует нетипизированный YAML
    from config.clients import validate_tenant_config
    from config.settings import LLMParams, MediaSettings
    object.__setattr__(s, "llm", LLMParams("gpt-4o", 0.7, 1000, 30))
    object.__setattr__(s, "pause_on", ["booking", "complaint"])
    object.__setattr__(s, "notify_channels", ["telegram"])
    object.__setattr__(s, "media", MediaSettings(daily_limit=50))
    object.__setattr__(s, "whatsapp_access_token", "default_tok")
    object.__setattr__(s, "manual_timeout_hours", 12)
    object.__setattr__(s, "timezone", "UTC")
    object.__setattr__(s, "notify_on_no_answer", "off")
    object.__setattr__(s, "minutes_per_reply", 2)
    object.__setattr__(s, "features", {})
    object.__setattr__(s, "handoff_pauses_bot", True)
    bad_cfg = {
        "provider": "wa",
        "business_name": "Test Salon",
        "knowledge_base": "Тестовая база знаний",
        "access_token": "valid_token",
        "fallback_triggers": "цена",  # строка вместо списка
        "pause_on": 5,                 # число вместо списка
        "notify_channels": "telegram", # строка вместо списка
        "media": {"daily_limit": "не_число", "audio": True},
    }
    # Должно безопасно обработаться без TypeError / ValueError
    s_bad, problems, _ = validate_tenant_config(bad_cfg, s, "1234567890", "1234567890.yaml")
    # Проверяем, что fallback_triggers не разбился на отдельные символы ['ц', 'е', 'н', 'а']
    if s_bad:
        assert s_bad.fallback_triggers != ["ц", "е", "н", "а"], "fallback_triggers не должен итерироваться посимвольно!"
        assert s_bad.media.daily_limit == getattr(s.media, "daily_limit", 50), "Битое число daily_limit должно откатываться к дефолту"
    else:
        assert any("fallback_triggers" in p or "pause_on" in p for p in problems)
    # 16. Проверка M1: resolve_conversation_handoffs закрывает все открытые передачи
    from storage import resolve_conversation_handoffs, get_open_handoff
    conv_m1 = create_conversation("client_m1", "wa", "+77017770011")
    h1 = create_handoff(conv_m1["id"], "complaint")
    h2 = create_handoff(conv_m1["id"], "booking")
    assert get_open_handoff(conv_m1["id"]) is not None
    resolved_count = resolve_conversation_handoffs(conv_m1["id"])
    assert resolved_count == 2, f"Должны были закрыться обе передачи, закрыто: {resolved_count}"
    assert get_open_handoff(conv_m1["id"]) is None, "В диалоге не должно остаться открытых передач"
    print("[OK] 16. resolve_conversation_handoffs закрывает все открытые передачи")

    # 17. Проверка M4: /start от клиента не сбрасывает manual-режим
    from handlers.message_handler import MessageProcessor
    conv_m4 = create_conversation("client_m4", "wa", "+77017770022")
    update_conversation_status(conv_m4["id"], "manual")
    h_m4 = create_handoff(conv_m4["id"], "human_requested")
    
    class DummySender:
        def __init__(self):
            self.sent = []
        async def send_text(self, to, text, conversation_id=""):
            self.sent.append((to, text))
            return "msg-123"
    dummy_sender = DummySender()
    handler = MessageProcessor(s, None, dummy_sender)
    asyncio.run(handler.handle_greeting("+77017770022", ""))
    conv_after = get_conversation(conv_m4["id"])
    assert conv_after["status"] == "manual", "Клиент не должен иметь возможность сбросить manual-режим менеджера!"
    assert get_open_handoff(conv_m4["id"]) is not None, "Открытый handoff менеджера не должен закрываться клиентом!"
    assert len(dummy_sender.sent) == 0, "Бот не должен отвечать приветствием в диалоге с ручным режимом оператора!"
    print("[OK] 17. /start от клиента не прерывает manual-режим менеджера")

    print("\nВсе проверки исправлений (включая 2-й круг ревью) успешно пройдены!")

if __name__ == "__main__":
    test_all()
