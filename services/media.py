"""services/media.py — скачивание, транскрибация аудио и распознавание изображений.

Компоненты:
1. Защищённое скачивание медиа по URL (HTTPS, разрешённые хосты, streaming с отсечкой размера, таймаут 15 с).
2. Rate-limiter (не более 5 медиа в минуту на номер).
3. transcribe_audio(data, mime, language, hint, settings): обращение к OpenAI Whisper.
   - multipart POST {transcribe_base_url}/audio/transcriptions
   - model: whisper-1, response_format: verbose_json
   - промпт с названием бизнеса и услугами
   - расчёт стоимости: $0.006 за минуту аудио
4. describe_image(data, mime, caption, business, settings): обращение к Vision API.
   - ресайз до 1600px по длинной стороне через PIL
   - передача data URL (base64) в vision-модель
   - промпт с описанием в 1-3 предложениях на русском и чтением текста/прайсов
"""

import asyncio
import base64
import io
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    Image = None
    HAS_PIL = False

from config.settings import Settings

logger = logging.getLogger(__name__)

# Тариф Whisper: $0.006 за минуту
WHISPER_PRICE_PER_MINUTE = 0.006

# Разрешённые хосты для скачивания вложений (Zernio, CDN и облачные хранилища)
ALLOWED_MEDIA_HOSTS = (
    "zernio.com",
    "api.zernio.com",
    "app.zernio.com",
    "cdn.zernio.com",
    "media.zernio.com",
    "whatsapp.com",
    "fbcdn.net",
    "fbsbx.com",
    "facebook.com",
    "amazonaws.com",
    "cloudfront.net",
    "cloudflarestorage.com",
    "r2.dev",
    "googleapis.com",
    "googleusercontent.com",
    "digitaloceanspaces.com",
)

# Ограничение частоты: номер -> список таймстемпов последних медиа (за 60 сек)
_rate_limit_lock = asyncio.Lock()
_rate_limit_history: dict[str, list[float]] = {}
RATE_LIMIT_PER_MINUTE = 5


@dataclass
class MediaResult:
    """Результат обработки медиафайла."""
    text: str
    duration_s: float
    model: str
    cost: float
    status: str            # 'ok' | 'failed' | 'skipped'
    size_bytes: int
    mime: str
    local_path: str = ""
    error: str = ""


class MediaError(Exception):
    """Ошибка обработки медиа."""


class MediaLimitError(MediaError):
    """Превышение лимитов размера или частоты медиа."""


async def check_rate_limit(phone: str) -> bool:
    """Проверяет скользящий лимит: не более 5 медиа в минуту с одного номера."""
    now = time.monotonic()
    async with _rate_limit_lock:
        # Очистка устаревших ключей для предотвращения утечки памяти
        if len(_rate_limit_history) > 200:
            stale_keys = [k for k, v in _rate_limit_history.items() if not v or (now - v[-1] >= 60.0)]
            for k in stale_keys:
                _rate_limit_history.pop(k, None)

        timestamps = _rate_limit_history.get(phone, [])
        # Очистить записи старше 60 секунд
        timestamps = [t for t in timestamps if now - t < 60.0]
        if len(timestamps) >= RATE_LIMIT_PER_MINUTE:
            _rate_limit_history[phone] = timestamps
            return False
        timestamps.append(now)
        _rate_limit_history[phone] = timestamps
        return True


def is_url_allowed(url: str) -> bool:
    """Проверяет безопасность URL: только https и разрешённые домены."""
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() != "https":
            return False
        host = (parsed.hostname or "").lower()
        if not host:
            return False
        return any(host == allowed or host.endswith("." + allowed) for allowed in ALLOWED_MEDIA_HOSTS)
    except Exception:
        return False


async def download_media(
    url: str,
    max_bytes: int,
    timeout_s: float = 15.0,
    headers: dict[str, str] | None = None,
    api_key: str = "",
) -> tuple[bytes, str]:
    """
    Потоковая безопасная загрузка медиафайла с ограничением размера.
    Возвращает (данные, mime_тип).
    """
    if not is_url_allowed(url):
        raise MediaError(f"URL не входит в список разрешённых доменов: {url}")

    target_url = url
    max_redirects = 3

    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as client:
        try:
            for _ in range(max_redirects + 1):
                if not is_url_allowed(target_url):
                    raise MediaError(f"URL не входит в список разрешённых доменов: {target_url}")

                req_headers = dict(headers or {})
                parsed = urlparse(target_url)
                host = (parsed.hostname or "").lower()
                token = api_key or os.getenv("ZERNIO_API_KEY", "").strip()
                if token and (host == "zernio.com" or host.endswith(".zernio.com")):
                    if "authorization" not in {k.lower() for k in req_headers}:
                        req_headers["Authorization"] = f"Bearer {token}"

                stream_kwargs = {}
                if req_headers:
                    stream_kwargs["headers"] = req_headers

                async with client.stream("GET", target_url, **stream_kwargs) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise MediaError("Редирект без заголовка Location")
                        from urllib.parse import urljoin
                        target_url = urljoin(target_url, location)
                        continue

                    if response.status_code != 200:
                        raise MediaError(f"Ошибка загрузки медиа: HTTP {response.status_code}")

                    content_type = response.headers.get("Content-Type", "").split(";")[0].strip().lower()
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            raise MediaLimitError(f"Размер файла превышает лимит ({len(data)} > {max_bytes} байт)")
                    return bytes(data), content_type or "application/octet-stream"

            raise MediaError("Слишком много редиректов при скачивании медиа")
        except httpx.TimeoutException as exc:
            raise MediaError(f"Таймаут скачивания медиа ({timeout_s} с)") from exc
        except httpx.HTTPError as exc:
            raise MediaError(f"Сетевая ошибка при скачивании медиа: {exc}") from exc


def resize_image_if_needed(image_bytes: bytes, max_side: int = 1600) -> tuple[bytes, str]:
    """
    Уменьшает изображение до max_side по длинной стороне через PIL.
    Возвращает (новые_байты, mime_type: image/jpeg).
    """
    if not HAS_PIL or Image is None:
        logger.warning("Pillow (PIL) не установлен, отправляем картинку без сжатия")
        return image_bytes, "image/jpeg"

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            # Преобразуем RGBA/P в RGB для JPEG
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")

            width, height = img.size
            if max(width, height) > max_side:
                if width > height:
                    new_w = max_side
                    new_h = int(height * (max_side / width))
                else:
                    new_h = max_side
                    new_w = int(width * (max_side / height))
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85, optimize=True)
            return buf.getvalue(), "image/jpeg"
    except Exception as exc:
        logger.warning("Не удалось сжать картинку через PIL, оставляем оригинал: %s", exc)
        return image_bytes, "image/jpeg"


def convert_audio_to_mp3(audio_bytes: bytes, input_format: str = "ogg") -> bytes:
    """Конвертирует аудио в MP3 через ffmpeg, если провайдер не принял OGG."""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        logger.warning("ffmpeg не найден в системе, отдаём исходные байты")
        return audio_bytes

    with tempfile.TemporaryDirectory() as tmp_dir:
        in_path = os.path.join(tmp_dir, f"input.{input_format}")
        out_path = os.path.join(tmp_dir, "output.mp3")
        with open(in_path, "wb") as f:
            f.write(audio_bytes)

        cmd = [ffmpeg_bin, "-y", "-i", in_path, "-vn", "-ar", "16000", "-ac", "1", "-b:a", "32k", out_path]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
            if res.returncode == 0 and os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    return f.read()
        except Exception as e:
            logger.warning("Ошибка конвертации аудио через ffmpeg: %s", e)
    return audio_bytes


async def transcribe_audio(
    data: bytes,
    mime: str,
    language: str,
    hint: str,
    settings: Settings,
    retry: bool = True,
) -> MediaResult:
    """
    Транскрибирует аудио через OpenAI Whisper API.
    language: 'ru', 'kk' или 'auto' (при auto не передаём language).
    hint: название бизнеса + ключевые услуги из базы знаний.
    """
    api_key = settings.openai_api_key or os.getenv("OPENAI_API_KEY", "").strip() or os.getenv("LLM_API_KEY", "").strip()
    if not api_key:
        raise MediaError("OPENAI_API_KEY не задан для транскрибации аудио")

    base_url = settings.transcribe_base_url.rstrip("/")
    endpoint = f"{base_url}/audio/transcriptions"
    model = settings.transcribe_model or "whisper-1"

    # Формируем имя файла и multipart payload
    filename = "voice.ogg" if "ogg" in mime or "opus" in mime else "voice.mp3"
    files = {"file": (filename, data, mime or "audio/ogg")}
    payload: dict[str, str] = {
        "model": model,
        "response_format": "verbose_json",
    }
    if language and language.lower() not in ("auto", "none"):
        payload["language"] = language.lower()
    if hint:
        payload["prompt"] = hint[:400]

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}"},
                data=payload,
                files=files,
            )
        except httpx.TimeoutException as exc:
            if retry:
                logger.warning("Таймаут транскрибации Whisper — повтор запроса")
                return await transcribe_audio(data, mime, language, hint, settings, retry=False)
            raise MediaError("Таймаут обращения к Whisper API") from exc
        except httpx.HTTPError as exc:
            if retry:
                logger.warning("Сетевая ошибка Whisper API (%s) — повтор запроса", exc)
                return await transcribe_audio(data, mime, language, hint, settings, retry=False)
            raise MediaError(f"Сетевая ошибка Whisper API: {exc}") from exc

    # Если API отклонил формат, пробуем сконвертировать через ffmpeg
    if response.status_code == 400 and "format" in response.text.lower() and retry:
        logger.info("Whisper отклонил аудиоформат, пробуем конвертацию в mp3 через ffmpeg")
        mp3_data = convert_audio_to_mp3(data)
        return await transcribe_audio(mp3_data, "audio/mpeg", language, hint, settings, retry=False)

    if response.status_code != 200:
        if retry and response.status_code >= 500:
            logger.warning("Whisper API вернул %d — повтор через 1с", response.status_code)
            await asyncio.sleep(1.0)
            return await transcribe_audio(data, mime, language, hint, settings, retry=False)
        raise MediaError(f"Whisper API ошибка {response.status_code}: {response.text[:200]}")

    try:
        res_json = response.json()
    except Exception as exc:
        raise MediaError("Невалидный JSON от Whisper API") from exc

    transcript = str(res_json.get("text") or "").strip()
    duration_s = float(res_json.get("duration") or 0.0)
    # Расчёт стоимости: duration_s / 60 * 0.006
    cost = round((duration_s / 60.0) * WHISPER_PRICE_PER_MINUTE, 6)

    return MediaResult(
        text=transcript,
        duration_s=duration_s,
        model=model,
        cost=cost,
        status="ok",
        size_bytes=len(data),
        mime=mime,
    )


async def describe_image(
    data: bytes,
    mime: str,
    caption: str,
    business_name: str,
    settings: Settings,
    retry: bool = True,
) -> MediaResult:
    """
    Описывает изображение через Vision-модель.
    Картинка ресайзится до 1600px и передаётся как data URL base64.
    """
    api_url = settings.vision_api_url or settings.llm_api_url or os.getenv("VISION_API_URL", "").strip().rstrip("/") or os.getenv("LLM_API_URL", "").strip().rstrip("/")
    api_key = settings.vision_api_key or settings.llm_api_key or os.getenv("VISION_API_KEY", "").strip() or os.getenv("LLM_API_KEY", "").strip()
    model = settings.vision_model or settings.llm.model or os.getenv("VISION_MODEL", "").strip() or os.getenv("LLM_MODEL", "").strip()

    if not api_url or not api_key:
        raise MediaError("Не задан VISION_API_URL или VISION_API_KEY")

    endpoint = api_url.rstrip("/") + "/chat/completions"

    # Ресайз до 1600px
    proc_data, proc_mime = resize_image_if_needed(data, max_side=1600)
    b64_str = base64.b64encode(proc_data).decode("utf-8")
    data_url = f"data:{proc_mime};base64,{b64_str}"

    system_instruction = (
        f"Ты помощник бизнеса {business_name or 'нашей компании'}. "
        "Твоя задача — кратко и информативно описать изображение для менеджера и виртуального консультанта в 1–3 предложениях по-русски. "
        "ОБЯЗАТЕЛЬНО дословно перепиши весь видимый текст (прайс-листы, списки услуг, вывески, подписи, чеки). "
        "Если на фото дизайн ногтей, причёска, одежда или товар — опиши стиль, цвет, детали. "
        "Не пытайся определять личности людей по лицу."
    )
    user_prompt = "Опиши это изображение."
    if caption:
        user_prompt += f" Подпись клиента: {caption}"

    # Для моделей o-серии / gpt-5/6 часто требуется max_completion_tokens вместо max_tokens
    use_max_completion = any(k in model.lower() for k in ("gpt-5", "gpt-6", "o1", "o3", "o4"))
    limit_key = "max_completion_tokens" if use_max_completion else "max_tokens"

    payload = {
        "model": model,
        limit_key: 600,
        "messages": [
            {"role": "system", "content": system_instruction},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ],
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.TimeoutException as exc:
            if retry:
                logger.warning("Таймаут Vision API — повтор запроса")
                return await describe_image(data, mime, caption, business_name, settings, retry=False)
            raise MediaError("Таймаут обращения к Vision API") from exc
        except httpx.HTTPError as exc:
            if retry:
                logger.warning("Сетевая ошибка Vision API (%s) — повтор запроса", exc)
                return await describe_image(data, mime, caption, business_name, settings, retry=False)
            raise MediaError(f"Сетевая ошибка Vision API: {exc}") from exc

        # Если 400 из-за параметра max_tokens/max_completion_tokens — адаптируем и повторяем
        if response.status_code == 400:
            err_text = response.text
            if "max_completion_tokens" in err_text and "max_tokens" in payload:
                logger.info("Vision API требует max_completion_tokens вместо max_tokens — повторяем")
                payload.pop("max_tokens", None)
                payload["max_completion_tokens"] = 600
                response = await client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            elif "max_tokens" in err_text and "max_completion_tokens" in payload:
                logger.info("Vision API требует max_tokens вместо max_completion_tokens — повторяем")
                payload.pop("max_completion_tokens", None)
                payload["max_tokens"] = 600
                response = await client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )

    if response.status_code != 200:
        if retry and response.status_code >= 500:
            logger.warning("Vision API вернул %d — повтор через 1с", response.status_code)
            await asyncio.sleep(1.0)
            return await describe_image(data, mime, caption, business_name, settings, retry=False)
        raise MediaError(f"Vision API ошибка {response.status_code}: {response.text[:200]}")

    try:
        data_json = response.json()
        choice_msg = (data_json.get("choices") or [{}])[0].get("message") or {}
        content_val = choice_msg.get("content")
        description = str(content_val or "").strip()
    except Exception as exc:
        raise MediaError("Невалидный ответ Vision API") from exc

    # Оценка стоимости токенов (примерно)
    usage = data_json.get("usage") or {}
    tokens_in = int(usage.get("prompt_tokens") or 0)
    tokens_out = int(usage.get("completion_tokens") or 0)
    cost = round((tokens_in * 0.10 + tokens_out * 0.50) / 1_000_000, 6)

    return MediaResult(
        text=description,
        duration_s=0.0,
        model=model,
        cost=cost,
        status="ok",
        size_bytes=len(data),
        mime=proc_mime,
    )


def save_media_file(
    data: bytes,
    media_dir: str,
    client_key: str,
    conversation_id: str,
    message_id: str,
    mime: str,
) -> str:
    """
    Сохраняет файл на локальный том в MEDIA_DIR/{client_key}/{conv_id}/{message_id}.{ext}.
    Возвращает относительный путь или путь относительно media_dir.
    """
    ext = "bin"
    if "ogg" in mime or "opus" in mime:
        ext = "ogg"
    elif "mp3" in mime or "mpeg" in mime:
        ext = "mp3"
    elif "jpeg" in mime or "jpg" in mime:
        ext = "jpg"
    elif "png" in mime:
        ext = "png"
    elif "webp" in mime:
        ext = "webp"
    elif "mp4" in mime:
        ext = "mp4"

    safe_client = re.sub(r"[^\w\-]", "_", str(client_key or "default"))
    safe_conv = re.sub(r"[^\w\-]", "_", str(conversation_id or "default"))
    safe_msg = re.sub(r"[^\w\-]", "_", str(message_id or f"msg_{int(time.time()*1000)}"))

    base_dir = Path(media_dir)
    target_dir = base_dir / safe_client / safe_conv
    target_dir.mkdir(parents=True, exist_ok=True)

    target_path = target_dir / f"{safe_msg}.{ext}"
    target_path.write_bytes(data)

    return str(target_path)
