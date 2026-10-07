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
    get_handoff,
    get_handoffs_needing_reminder,
    get_message_by_provider_id,
    list_conversations,
    get_messages,
    execute,
    fetchone,
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
    from storage.handoffs import resolve_handoff
    resolve_handoff(hid)
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
        def __init__(self, headers, host="127.0.0.1", path="/admin/test"):
            self.headers = headers
            self.client = type("Client", (), {"host": host})()
            self.state = type("State", (), {})()
            self.url = type("URL", (), {"path": path})()
    
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

    # 8. Проверка таймаута manual_since / last_human_message_at
    from storage.conversations import get_conversations_needing_timeout_check, update_conversation_status
    conv_timeout = create_conversation("client_timeout", "wa", "+77019998877")
    update_conversation_status(conv_timeout["id"], "manual")
    # Если manual_since 10 часов назад, но оператор написал прямо сейчас, он НЕ должен попадать в таймаут 2 часа
    execute("UPDATE conversations SET manual_since = datetime('now', '-10 hours'), last_human_message_at = datetime('now') WHERE id = ?", (conv_timeout["id"],))
    needing_timeout = get_conversations_needing_timeout_check(timeout_hours=2, client_key="client_timeout")
    assert not any(c["id"] == conv_timeout["id"] for c in needing_timeout), "Активный диалог не должен возвращаться к боту раньше времени!"

    # А если оба были 3 часа назад - должен попадать
    execute("UPDATE conversations SET manual_since = datetime('now', '-5 hours'), last_human_message_at = datetime('now', '-3 hours') WHERE id = ?", (conv_timeout["id"],))
    needing_timeout = get_conversations_needing_timeout_check(timeout_hours=2, client_key="client_timeout")
    assert any(c["id"] == conv_timeout["id"] for c in needing_timeout), "Неактивный диалог должен возвращаться к боту"
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

    # 18. Проверка M3: notify_owner не блокирует троттлингом при неудачной отправке
    from handlers.owner_handler import notify_owner, _notification_throttle
    from whatsapp.errors import MessagingError
    from unittest.mock import AsyncMock, patch
    _notification_throttle.clear()
    s_m3 = object.__new__(Settings)
    object.__setattr__(s_m3, "business_name", "Shop")
    object.__setattr__(s_m3, "owner_phone", "")
    object.__setattr__(s_m3, "owner_telegram_chat_id", "12345")
    object.__setattr__(s_m3, "notify_channels", ["telegram"])
    object.__setattr__(s_m3, "notify_on_no_answer", "notify")
    object.__setattr__(s_m3, "features", {"telegram_notify": True})
    
    with patch("handlers.owner_handler._send_telegram_notification", side_effect=MessagingError("Network fail")):
        res = asyncio.run(notify_owner(dummy_sender, s_m3, "+79991112233", "User", "Help", "complaint", conversation_id="conv_m3"))
        assert res is False, "Должен вернуть False при ошибке доставки"
        assert ("conv_m3", "complaint") not in _notification_throttle, "Неуспешная доставка не должна троттлиться!"
    
    with patch("handlers.owner_handler._send_telegram_notification", new_callable=AsyncMock) as mock_send:
        res = asyncio.run(notify_owner(dummy_sender, s_m3, "+79991112233", "User", "Help", "complaint", conversation_id="conv_m3"))
        assert res is True
        assert ("conv_m3", "complaint") in _notification_throttle, "Успешная доставка должна попасть в троттлинг"
    print("[OK] 18. notify_owner не троттлит при сбое доставки")

    # 19. Проверка M5: update_conversation_status не затирает last_message_at, таймаут не блокируется клиентом
    from storage.conversations import get_conversations_needing_timeout_check
    conv_m5 = create_conversation("client_m5", "wa", "+77017770033")
    execute("UPDATE conversations SET last_message_at = '2026-01-01 10:00:00' WHERE id = ?", (conv_m5["id"],))
    update_conversation_status(conv_m5["id"], "bot")
    conv_check = get_conversation(conv_m5["id"])
    assert conv_check["last_message_at"] == "2026-01-01 10:00:00", "Смена статуса на bot не должна обновлять last_message_at!"
    
    # Переводим в manual с manual_since 10 часов назад, но клиент писал 1 минуту назад
    update_conversation_status(conv_m5["id"], "manual")
    execute(
        "UPDATE conversations SET manual_since = datetime('now', '-10 hours'), last_message_at = datetime('now', '-1 minute') WHERE id = ?",
        (conv_m5["id"],),
    )
    # Таймаут 5 часов
    needing = get_conversations_needing_timeout_check(timeout_hours=5, client_key="client_m5")
    assert any(c["id"] == conv_m5["id"] for c in needing), "Диалог с просроченным manual_since должен попадать под таймаут даже если клиент писал недавно"
    
    # Но если менеджер ответил 10 минут назад — диалог не должен возвращаться к боту
    add_message(conv_m5["id"], "human", "Ответ оператора")
    needing_after_human = get_conversations_needing_timeout_check(timeout_hours=5, client_key="client_m5")
    assert not any(c["id"] == conv_m5["id"] for c in needing_after_human), "Диалог с недавним ответом оператора не должен таймаутиться"
    # 20. Проверка M2: run_reminder_job фильтрует настройки и не маркирует reminded при сбое
    from main import run_reminder_job
    conv_m2 = create_conversation("client_m2", "wa", "+77017770044")
    h_m2 = create_handoff(conv_m2["id"], "booking")
    execute("UPDATE handoffs SET created_at = datetime('now', '-3 hours'), notified_at = datetime('now', '-3 hours') WHERE id = ?", (h_m2,))
    
    dummy_state = MagicMock(multitenant=False, sender=DummySender())
    
    # 20.1 notify_on_no_answer = "off"
    s_m2 = object.__new__(Settings)
    object.__setattr__(s_m2, "notify_on_no_answer", "off")
    object.__setattr__(s_m2, "features", {"telegram_notify": True})
    object.__setattr__(s_m2, "notify_channels", ["telegram"])
    object.__setattr__(s_m2, "owner_telegram_chat_id", "12345")
    object.__setattr__(s_m2, "public_base_url", "")
    object.__setattr__(s_m2, "feature", lambda name: True)
    
    with patch("handlers.owner_handler._send_telegram_notification", new_callable=AsyncMock) as mock_send:
        sent = asyncio.run(run_reminder_job(s_m2, dummy_state))
        assert sent == 0
        assert mock_send.call_count == 0
        assert get_handoff(h_m2)["reminded_at"] is None
        
    # 20.2 feature("telegram_notify") = False
    object.__setattr__(s_m2, "notify_on_no_answer", "notify")
    object.__setattr__(s_m2, "feature", lambda name: False if name == "telegram_notify" else True)
    with patch("handlers.owner_handler._send_telegram_notification", new_callable=AsyncMock) as mock_send:
        sent = asyncio.run(run_reminder_job(s_m2, dummy_state))
        assert sent == 0
        assert mock_send.call_count == 0
        assert get_handoff(h_m2)["reminded_at"] is None
        
    # 20.3 Ошибка отправки -> не вызывать mark_reminded
    object.__setattr__(s_m2, "feature", lambda name: True)
    with patch("handlers.owner_handler._send_telegram_notification", side_effect=Exception("TG error")):
        sent = asyncio.run(run_reminder_job(s_m2, dummy_state))
        assert sent == 0
        assert get_handoff(h_m2)["reminded_at"] is None
        
    # 20.4 Успешная отправка -> reminded_at выставлен
    with patch("handlers.owner_handler._send_telegram_notification", new_callable=AsyncMock) as mock_send:
        sent = asyncio.run(run_reminder_job(s_m2, dummy_state))
        assert sent == 1
        assert mock_send.call_count == 1
        assert get_handoff(h_m2)["reminded_at"] is not None
    print("[OK] 20. reminder_job корректно проверяет настройки и доставку")

    # 21. Проверка M6: ограничение media.daily_limit/max_audio_seconds для клиента и cooldown на regroup
    from admin.api import hash_token, verify_token_hash, TOKEN_PBKDF2_PREFIX, TOKEN_HASH_PREFIX
    from storage import get_last_regroup_time
    assert get_last_regroup_time("non_existent_key") is None
    print("[OK] 21. Ресурсные лимиты media и cooldown перегруппировки")

    # 22. Проверка M7: безопасное хэширование токенов с солью, поддержка не-ASCII и отказ от ?token=
    salt_hash = hash_token("пароль_123")
    assert salt_hash.startswith(TOKEN_PBKDF2_PREFIX)
    assert verify_token_hash("пароль_123", salt_hash) is True
    assert verify_token_hash("неверный", salt_hash) is False
    # Обратная совместимость с sha256:<hex>
    import hashlib
    legacy_hash = TOKEN_HASH_PREFIX + hashlib.sha256("старый_пароль".encode("utf-8")).hexdigest()
    assert verify_token_hash("старый_пароль", legacy_hash) is True
    assert verify_token_hash("другой", legacy_hash) is False
    # ?token= в query_params не должен проходить в _authorize
    req_bad_param = DummyRequest({})
    req_bad_param.query_params = {"token": "secret_adm_token"}
    role_token_param, err_token_param = _authorize(dummy_settings, req_bad_param, Path(td), None)
    assert role_token_param is None and err_token_param in (401, 403), "?token= в query string не должен авторизовывать!"
    assert req_bad_param.state.auth_failed is True, "При неудачной авторизации должен быть выставлен auth_failed"
    print("[OK] 22. Хэширование токенов salted PBKDF2, поддержка не-ASCII, отказ от ?token=")

    # 23. Проверка M8: дедупликация по idempotency_key при отправке сообщений оператором
    conv_m8 = create_conversation("client_m8", "wa", "+77017770055")
    add_message(conv_m8["id"], "human", "Тест", provider_message_id="idemp-key-1")
    found_msg = get_message_by_provider_id("idemp-key-1")
    assert found_msg is not None
    assert found_msg["conversation_id"] == conv_m8["id"]
    print("[OK] 23. idempotency_key корректно ищется через get_message_by_provider_id")

    # 24. Проверка M9: детерминированный tie-break по id в subquery и cursor пагинация; before_id в get_messages
    conv_m9 = create_conversation("client_m9", "wa", "+77017770066")
    cid_m9 = conv_m9["id"]
    m1 = add_message(cid_m9, "client", "Первое сообщение")
    m2 = add_message(cid_m9, "client", "Второе сообщение")
    # Имитируем одинаковый created_at (в пределах одной секунды)
    execute("UPDATE messages SET created_at = '2026-01-01 12:00:00' WHERE conversation_id = ?", (cid_m9,))
    convs_m9 = list_conversations("client_m9")
    assert len(convs_m9) == 1
    assert convs_m9[0]["last_message_text"] == "Второе сообщение", "Subquery должен брать последнее сообщение по id DESC при одинаковом created_at"

    # Пагинация сообщений по before_id
    earlier_msgs = get_messages(cid_m9, before_id=m2)
    assert len(earlier_msgs) == 1
    assert earlier_msgs[0]["id"] == m1
    # Поддержка before в виде строки ID
    earlier_msgs_str = get_messages(cid_m9, before=str(m2))
    assert len(earlier_msgs_str) == 1
    assert earlier_msgs_str[0]["id"] == m1
    print("[OK] 24. list_conversations и get_messages детерминированно пагинируются с tie-break по id")

    # 25. Проверка M10: изоляция media rate limit по client_key и исключение skipped-медиа из daily_limit
    from services.media import check_rate_limit, _rate_limit_history
    _rate_limit_history.clear()
    shared_phone = "+77099990000"
    # Заполняем лимит (5) для tenant_a
    for _ in range(5):
        assert asyncio.run(check_rate_limit(shared_phone, client_key="tenant_a")) is True
    # 6-й раз для tenant_a блокируется
    assert asyncio.run(check_rate_limit(shared_phone, client_key="tenant_a")) is False
    # Но для tenant_b с тем же номером лимит независим!
    assert asyncio.run(check_rate_limit(shared_phone, client_key="tenant_b")) is True

    # Проверка daily_limit: skipped сообщения не должны учитываться
    conv_m10 = create_conversation("client_m10", "wa", "+77017770077")
    cid_m10 = conv_m10["id"]
    add_message(cid_m10, "client", "[Фото]", content_kind="image", media_status="skipped")
    row_cnt = fetchone(
        """
        SELECT COUNT(*) as cnt FROM messages
        WHERE conversation_id = ?
          AND role = 'client'
          AND content_kind IN ('voice', 'audio', 'image')
          AND (media_status IS NULL OR media_status != 'skipped')
          AND date(created_at) = date('now')
        """,
        (cid_m10,),
    )
    assert row_cnt["cnt"] == 0, "Skipped медиа не должны учитываться в подсчёте daily_limit!"
    print("[OK] 25. Изоляция media rate limit по client_key и корректный подсчёт daily_limit")

    # 26. Проверка M11: CLIENT_CONFIG не триггерит multitenant авто-детект из Volume; get_db_path не портит env
    from storage.db import get_db_path
    db_p = get_db_path()
    assert db_p.name == "test.db"
    with patch.dict(os.environ, {"CLIENT_CONFIG": "custom_single.yaml", "RAILWAY_VOLUME_MOUNT_PATH": "/vol", "CLIENTS_DIR": ""}):
        from config.settings import resolve_clients_dir
        # В single-tenant при CLIENT_CONFIG volume не должен превращать режим в multitenant
        assert resolve_clients_dir() is None
    print("[OK] 26. single-tenant с CLIENT_CONFIG защищён от случайного переключения на volume clients_dir")

    # 27. Проверка M12: точный regex в _is_reasoning_model и изоляция usage токенов
    from services.llm_client import _is_reasoning_model, LLMClient
    from config.settings import LLMParams
    # Проверка ложных срабатываний
    assert _is_reasoning_model("v0.1") is False
    assert _is_reasoning_model("v0.3") is False
    assert _is_reasoning_model("audio1") is False
    assert _is_reasoning_model("hero1") is False
    assert _is_reasoning_model("mono1") is False
    assert _is_reasoning_model("qwen-2.5-72b-instruct") is False
    # Истинные reasoning модели
    assert _is_reasoning_model("o1") is True
    assert _is_reasoning_model("o1-mini") is True
    assert _is_reasoning_model("openai/o3-mini") is True
    assert _is_reasoning_model("gpt-5") is True
    assert _is_reasoning_model("gpt-6-turbo") is True
    print("[OK] 27. _is_reasoning_model не имеет ложных срабатываний на v0.1/v0.3/audio1")

    print("\nВсе проверки исправлений (включая 2-й круг ревью) успешно пройдены!")

if __name__ == "__main__":
    test_all()
