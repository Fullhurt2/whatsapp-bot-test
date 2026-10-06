"""tests/test_media.py — тесты распознавания голосовых и фото (v1).

Покрывает:
1. Защищённое скачивание медиа (is_url_allowed, download_media, streaming, лимиты размера).
2. Rate-limiter (не более 5 медиа в минуту).
3. Ресайз картинок (resize_image_if_needed).
4. Whisper транскрибацию (transcribe_audio, расчёт стоимости $0.006/мин, retry).
5. Vision распознавание (describe_image, retry).
6. Сохранение файлов на диск и очистку устаревших (save_media_file, cleanup_expired_media).
7. MessageProcessor.handle_media:
   - фича выключена -> NON_TEXT_REPLY
   - видео/документы -> [видео]/[файл] в БД + NON_TEXT_REPLY
   - аудио -> транскрипция -> [Голосовое сообщение] в LLM
   - фото -> описание -> [Фото] в LLM
   - manual режим -> расшифровка в БД, бот молчит
   - rate limit / превышение размера -> skipped
   - сбой API -> failed
8. Админ-эндпоинты медиа (/media и /retry-media).
"""

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Изоляция БД

test_dir = tempfile.mkdtemp(prefix="jauap_media_test_")
os.environ["JAUAP_DB_PATH"] = os.path.join(test_dir, "test.db")
os.environ["OPENAI_API_KEY"] = "sk-test-openai-key"
os.environ["LLM_API_KEY"] = "test-key"
os.environ["ZERNIO_API_KEY"] = "test-zernio-key"
os.environ["ZERNIO_WEBHOOK_SECRET"] = "test-secret"
os.environ["ADMIN_TOKEN"] = "a" * 32

from PIL import Image

from config.settings import Settings, MediaSettings, LLMParams
from handlers.message_handler import MessageProcessor, NON_TEXT_REPLY
from services.media import (
    is_url_allowed,
    download_media,
    check_rate_limit,
    resize_image_if_needed,
    save_media_file,
    transcribe_audio,
    describe_image,
    MediaResult,
    MediaError,
    MediaLimitError,
)
from storage import init_db, apply_migrations, add_message, get_message, create_conversation
from storage.messages import cleanup_expired_media
from whatsapp.inbound import InboundMessage

passed = 0
failed = 0


def check(name: str, condition: bool, extra: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  OK   {name}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def make_test_settings(media_feature: bool = True) -> Settings:
    features = {"media": media_feature}
    media = MediaSettings(
        audio=True,
        image=True,
        max_audio_seconds=120,
        max_image_mb=8,
        daily_limit=50,
    )
    return Settings(
        messaging_provider="zernio",
        app_host="127.0.0.1",
        app_port=8000,
        llm_api_url="https://llm.test/v1",
        llm_api_key="test-key",
        zernio_api_key="test-key",
        zernio_webhook_secret="whsec_test",
        zernio_base_url="https://zernio.com/api/v1",
        zernio_account_id="acc_123",
        whatsapp_access_token="",
        whatsapp_phone_number_id="77011234567",
        meta_app_secret="",
        meta_verify_token="",
        meta_graph_version="v21.0",
        business_name="Салон Тест",
        tone="вежливый",
        language="ru",
        knowledge_base="Маникюр 5000 тенге. Стрижка 4000 тенге.",
        owner_phone="+77019998877",
        fallback_triggers=["жалоба", "отмена"],
        features=features,
        media=media,
        media_dir=os.path.join(test_dir, "media"),
        openai_api_key="sk-test-openai",
        llm=LLMParams(model="gpt-6-luna", temperature=0.6, max_tokens=1000, timeout_seconds=15, reasoning_effort="medium"),
    )



class DummySender:
    def __init__(self):
        self.sent = []

    async def send_text(self, phone: str, text: str, conversation_id: str = ""):
        self.sent.append({"phone": phone, "text": text, "conversation_id": conversation_id})


class DummyLLM:
    def __init__(self, reply: str = "Здравствуйте! Стрижка стоит 4000 тенге."):
        self.reply = reply
        self.calls = []

    async def chat(self, system_prompt: str, user_prompt: str, history: list = None):
        self.calls.append({"system": system_prompt, "prompt": user_prompt, "history": history})
        return self.reply


async def test_media_security_and_download():
    print("[1] Безопасность URL и потоковое скачивание")
    check("https zernio.com разрешён", is_url_allowed("https://zernio.com/media/file.ogg"))
    check("https api.zernio.com разрешён", is_url_allowed("https://api.zernio.com/v1/download/123"))
    check("https fbcdn.net разрешён", is_url_allowed("https://lookaside.fbsbx.com/file.jpg"))
    check("http отклонён", not is_url_allowed("http://zernio.com/file.ogg"))
    check("чужой хост отклонён", not is_url_allowed("https://evil-site.com/file.ogg"))

    # Mock download_media
    import httpx

    class MockStreamResponse:
        def __init__(self, chunks, status_code=200, headers=None):
            self.chunks = chunks
            self.status_code = status_code
            self.headers = headers or {"Content-Type": "audio/ogg"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def aiter_bytes(self):
            for c in self.chunks:
                yield c

    class MockClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def stream(self, method, url):
            if "too-large" in url:
                return MockStreamResponse([b"A" * 1024, b"B" * 2048])
            return MockStreamResponse([b"OggData12345"])

    with patch("httpx.AsyncClient", MockClient):
        # Нормальная загрузка
        data, mime = await download_media("https://zernio.com/audio.ogg", max_bytes=10000)
        check("download_media возвращает байты", data == b"OggData12345")
        check("download_media определяет mime", mime == "audio/ogg")

        # Превышение размера
        hit_limit = False
        try:
            await download_media("https://zernio.com/too-large.ogg", max_bytes=1500)
        except MediaLimitError:
            hit_limit = True
        check("download_media отсекает файлы больше max_bytes", hit_limit)


async def test_rate_limit_and_image_resize():
    print("[2] Rate-limiting и ресайз изображений")
    test_phone = "+77089990011"
    # Первые 5 запросов проходят
    results = [await check_rate_limit(test_phone) for _ in range(5)]
    check("5 запросов в минуту разрешены", all(results))

    # 6-й запрос блокируется
    blocked = await check_rate_limit(test_phone)
    check("6-й запрос в минуту заблокирован", not blocked)

    # Ресайз через PIL
    # Создаём картинку 2000x1000
    img = Image.new("RGB", (2000, 1000), color="blue")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    orig_bytes = buf.getvalue()

    resized_bytes, mime = resize_image_if_needed(orig_bytes, max_side=1600)
    with Image.open(io.BytesIO(resized_bytes)) as r_img:
        w, h = r_img.size
        check("Изображение уменьшено до max 1600px", max(w, h) == 1600)
        check("Пропорции сохранены (1600x800)", (w, h) == (1600, 800))


async def test_transcribe_and_describe_mocked():
    print("[3] Транскрибация Whisper и Vision (mock)")
    settings = make_test_settings()

    class MockWhisperResponse:
        status_code = 200

        def json(self):
            return {"text": "Здравствуйте, сколько стоит стрижка?", "duration": 15.5}

    class MockVisionResponse:
        status_code = 200

        def json(self):
            return {
                "choices": [{"message": {"content": "На фото прайс-лист: Маникюр 5000 тенге, Стрижка 4000 тенге."}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 25},
            }

    with patch("httpx.AsyncClient.post") as mock_post:
        # 1. Whisper
        mock_post.return_value = MockWhisperResponse()
        audio_res = await transcribe_audio(
            data=b"dummy_audio",
            mime="audio/ogg",
            language="ru",
            hint="Салон Тест",
            settings=settings,
        )
        check("Whisper текст получен", "стрижка" in audio_res.text)
        check("Whisper длительность 15.5с", audio_res.duration_s == 15.5)
        expected_cost = round((15.5 / 60.0) * 0.006, 6)
        check("Whisper расчёт стоимости правильный", audio_res.cost == expected_cost)

        # 2. Vision
        mock_post.return_value = MockVisionResponse()
        _buf = io.BytesIO()
        Image.new("RGB", (100, 100)).save(_buf, format="JPEG")
        test_img_bytes = _buf.getvalue()
        vision_res = await describe_image(
            data=test_img_bytes,
            mime="image/jpeg",
            caption="Вот прайс",
            business_name="Салон Тест",
            settings=settings,
        )


        check("Vision описание получено", "прайс-лист" in vision_res.text)
        check("Vision модель", vision_res.model == "gpt-6-luna")
        check("Vision статус ok", vision_res.status == "ok")


async def test_save_and_cleanup_media():
    print("[4] Хранилище файлов и очистка старых медиа (retention)")
    media_dir = os.path.join(test_dir, "media_store")
    local_path = save_media_file(
        data=b"TestVoiceData",
        media_dir=media_dir,
        client_key="acc_123",
        conversation_id="conv_abc",
        message_id="msg_999",
        mime="audio/ogg",
    )
    check("Файл сохранён на диск", os.path.exists(local_path))
    check("Расширение ogg", local_path.endswith(".ogg"))

    # Создаём диалог и сообщение с датой 35 дней назад
    conv = create_conversation(
        client_key="acc_123",
        channel="zernio",
        contact_phone="+77081112233",
        contact_name="Старый Клиент",
        zernio_conversation_id="conv_abc",
    )
    cid = conv["id"]
    msg_id = add_message(
        conversation_id=cid,
        role="client",
        text="[Голосовое сообщение] старое",
        content_kind="voice",
        media_path=local_path,
        media_status="ok",
    )
    # Искусственно сдвигаем created_at назад на 35 дней
    from storage.db import execute
    execute("UPDATE messages SET created_at = datetime('now', '-35 days') WHERE id = ?", (msg_id,))

    # Запускаем очистку (срок 30 дней)
    cleaned = cleanup_expired_media(days=30)
    check("cleanup_expired_media нашёл и обработал запись", cleaned >= 1)
    check("Файл физически удалён с диска", not os.path.exists(local_path))

    m_after = get_message(msg_id)
    check("Статус обновлён на expired", m_after["media_status"] == "expired")
    check("Текст сообщения сохранён", "[Голосовое сообщение]" in m_after["text"])


async def test_message_processor_media_flows():
    print("[5] MessageProcessor: полный пайплайн медиа")
    settings = make_test_settings(media_feature=True)
    sender = DummySender()
    llm = DummyLLM(reply="Стрижка стоит 4000 тенге, записать вас?")
    processor = MessageProcessor(settings, llm, sender)

    # 1. features.media = False -> NON_TEXT_REPLY
    settings_off = make_test_settings(media_feature=False)
    p_off = MessageProcessor(settings_off, llm, sender)
    inbound_audio = InboundMessage(
        phone="+77085551122",
        display_name="Алиса",
        text="",
        content_kind="voice",
        message_id="mid_1",
        conversation_id="z_conv_1",
        media_url="https://zernio.com/voice.ogg",
    )
    await p_off.handle_media("+77085551122", "Алиса", inbound_audio, conversation_id="z_conv_1")
    check("features.media=False -> NON_TEXT_REPLY", len(sender.sent) == 1 and NON_TEXT_REPLY in sender.sent[-1]["text"])

    # 2. Видео -> [видео] в БД и NON_TEXT_REPLY
    inbound_video = InboundMessage(
        phone="+77085551122",
        display_name="Алиса",
        text="",
        content_kind="video",
        message_id="mid_v1",
        conversation_id="z_conv_1",
        media_url="https://zernio.com/video.mp4",
    )
    await processor.handle_media("+77085551122", "Алиса", inbound_video, conversation_id="z_conv_1")
    check("Видео -> клиенту ответ текстом", len(sender.sent) == 2 and NON_TEXT_REPLY in sender.sent[-1]["text"])
    from storage.db import fetchone
    v_msg = fetchone("SELECT * FROM messages WHERE content_kind = 'video' ORDER BY id DESC LIMIT 1")
    check("Видео сохранено в БД со статусом skipped", v_msg and v_msg["text"] == "[видео]" and v_msg["media_status"] == "skipped")

    # 3. Аудио голосовое -> транскрибация -> передача в LLM -> ответ бота
    with patch("services.media.download_media", AsyncMock(return_value=(b"OggBytes", "audio/ogg"))), \
         patch("services.media.transcribe_audio", AsyncMock(return_value=MediaResult(
             text="Сколько стоит стрижка?",
             duration_s=5.2,
             model="whisper-1",
             cost=0.0005,
             status="ok",
             size_bytes=8,
             mime="audio/ogg",
         ))):

        await processor.handle_media("+77085553344", "Алиса", inbound_audio, conversation_id="z_conv_1")
        check("LLM вызвана для голосового", len(llm.calls) == 1)
        check("LLM получила синтезированный текст [Голосовое сообщение]", "[Голосовое сообщение] Сколько стоит стрижка?" in llm.calls[0]["prompt"])
        check("Клиент получил ответ бота", any("Стрижка стоит 4000 тенге" in s["text"] for s in sender.sent))

        # Проверка записи в БД
        db_audio_msg = fetchone("SELECT * FROM messages WHERE content_kind = 'voice' AND media_status = 'ok' ORDER BY id DESC LIMIT 1")
        check("Запись аудио в БД role=client", db_audio_msg and db_audio_msg["role"] == "client")
        check("Запись аудио содержит media_cost и media_model", db_audio_msg["media_model"] == "whisper-1" and db_audio_msg["media_cost"] > 0)

    # 4. Фото с подписью -> описание -> LLM
    inbound_photo = InboundMessage(
        phone="+77085554455",
        display_name="Алиса",
        text="Смотрите мой чек",
        media_caption="Смотрите мой чек",
        content_kind="image",
        message_id="mid_img1",
        conversation_id="z_conv_2",
        media_url="https://zernio.com/photo.jpg",
    )
    with patch("services.media.download_media", AsyncMock(return_value=(b"JpgBytes", "image/jpeg"))), \
         patch("services.media.describe_image", AsyncMock(return_value=MediaResult(
             text="Кассовый чек на сумму 5000 тенге за маникюр.",
             duration_s=0.0,
             model="gpt-6-luna",
             cost=0.0002,
             status="ok",
             size_bytes=8,
             mime="image/jpeg",
         ))):

        await processor.handle_media("+77085554455", "Алиса", inbound_photo, conversation_id="z_conv_2")
        check("LLM вызвана для фото", len(llm.calls) == 2)
        check("LLM получила синтезированный текст [Фото]", "[Фото] Смотрите мой чек. Описание: Кассовый чек" in llm.calls[1]["prompt"])

    # 5. Ручной режим (manual): бот молчит, но в БД расшифровка сохранена
    from storage import update_conversation_status, get_conversation_by_client_and_phone
    conv = get_conversation_by_client_and_phone(processor._client_key(), "+77085554455")
    update_conversation_status(conv["id"], "manual")
    sent_count_before = len(sender.sent)

    with patch("services.media.download_media", AsyncMock(return_value=(b"OggBytes", "audio/ogg"))), \
         patch("services.media.transcribe_audio", AsyncMock(return_value=MediaResult(
             text="Я жду ответ мастера!",
             duration_s=3.0,
             model="whisper-1",
             cost=0.0003,
             status="ok",
             size_bytes=8,
             mime="audio/ogg",
         ))):

        await processor.handle_media("+77085554455", "Алиса", inbound_audio, conversation_id="z_conv_2")
        check("В manual режиме бот молчит (ничего не отправлено)", len(sender.sent) == sent_count_before)
        manual_msg = fetchone("SELECT * FROM messages WHERE conversation_id = ? AND text LIKE '%жду ответ мастера%'", (conv["id"],))
        check("В manual режиме расшифровка сохранена в БД для оператора", bool(manual_msg))


async def test_admin_api_media_endpoints():
    print("[6] Админ-API: выдача медиафайла и повторная расшифровка")
    from admin.api import register_admin_api
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    clients_dir = Path(test_dir) / "clients"
    clients_dir.mkdir(parents=True, exist_ok=True)
    # Создаём клиента
    client_yaml = clients_dir / "77011234567.yaml"
    client_yaml.write_text(
        "business_name: Салон Тест\n"
        "provider: zernio\n"
        "zernio_account_id: acc_123\n"
        "management_token: abc123def456\n",
        encoding="utf-8",
    )

    import dataclasses
    settings = dataclasses.replace(
        make_test_settings(),
        clients_dir=str(clients_dir),
        admin_token="a" * 32,
    )


    # Создаём мок state
    class MockState:
        multitenant = True
        tenants = {}

    app = FastAPI()
    register_admin_api(app, settings, MockState())
    client = TestClient(app)

    # Создаём тестовое сообщение с локальным файлом
    dummy_file = Path(test_dir) / "test_audio.ogg"
    dummy_file.write_bytes(b"OGG_TEST_AUDIO_BYTES")

    conv = create_conversation("acc_123", "zernio", "+77089991122", "Тест", "z_c_test")
    cid = conv["id"]
    mid = add_message(
        conversation_id=cid,
        role="client",
        text="[Голосовое: ошибка]",
        content_kind="voice",
        media_path=str(dummy_file),
        media_mime="audio/ogg",
        media_status="failed",
    )

    # Запрос с неверным токеном -> 401/403
    r401 = client.get(f"/admin/clients/77011234567/conversations/{cid}/messages/{mid}/media")
    check("Без токена -> 401", r401.status_code == 401)

    # Запрос с правильным токеном через query параметр ?token=
    r_ok = client.get(
        f"/admin/clients/77011234567/conversations/{cid}/messages/{mid}/media?token={'a'*32}"
    )
    check("С токеном -> 200 FileResponse", r_ok.status_code == 200)
    check("Тело файла совпадает", r_ok.content == b"OGG_TEST_AUDIO_BYTES")

    # Повторная расшифровка retry-media
    with patch("services.media.transcribe_audio", AsyncMock(return_value=MediaResult(
        text="Успешная повторная расшифровка!",
        duration_s=4.0,
        model="whisper-1",
        cost=0.0004,
        status="ok",
        size_bytes=20,
        mime="audio/ogg",
    ))):
        r_retry = client.post(
            f"/admin/clients/77011234567/conversations/{cid}/messages/{mid}/retry-media",
            headers={"X-Admin-Token": "a" * 32},
        )
        check("retry-media -> 200 ok", r_retry.status_code == 200 and r_retry.json().get("ok"))
        m_updated = get_message(mid)
        check("В БД статус обновлён на ok", m_updated["media_status"] == "ok")
        check("Текст сообщения обновлён", "Успешная повторная расшифровка" in m_updated["text"])


async def main():
    print("=== Старт тестов распознавания голосовых и фото (v1) ===")
    init_db()
    apply_migrations()

    await test_media_security_and_download()
    await test_rate_limit_and_image_resize()
    await test_transcribe_and_describe_mocked()
    await test_save_and_cleanup_media()
    await test_message_processor_media_flows()
    await test_admin_api_media_endpoints()

    print(f"\nИтог: passed={passed}, failed={failed}")
    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
