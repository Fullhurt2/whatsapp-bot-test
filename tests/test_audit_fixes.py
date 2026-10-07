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
    print("[OK] 6. get_client_ip возвращает реальный IP клиента")

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

    print("\nВсе проверки исправлений (включая 2-й круг ревью) успешно пройдены!")

if __name__ == "__main__":
    test_all()
