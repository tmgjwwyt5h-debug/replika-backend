"""Голосовой бот: STT через Groq Whisper (бесплатно) + TTS через edge-tts (бесплатно)."""
import os, asyncio, tempfile, logging
import httpx
import edge_tts

logger = logging.getLogger(__name__)

# Доступные русские голоса edge-tts
VOICES = {
    "svetlana": "ru-RU-SvetlanaNeural",  # женский, нейтральный
    "dmitry":   "ru-RU-DmitryNeural",    # мужской, нейтральный
}


async def stt_groq(audio_bytes: bytes, filename: str = "voice.ogg") -> str:
    """Распознавание речи через Groq Whisper. Бесплатно: 500 мин/мес."""
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "GROQ_API_KEY не задан.\n"
            "Получите бесплатный ключ на https://console.groq.com → API Keys.\n"
            "Добавьте GROQ_API_KEY в Variables на Railway."
        )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.groq.com/openai/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            files={"file": (filename, audio_bytes, "audio/ogg")},
            data={"model": "whisper-large-v3", "language": "ru", "response_format": "text"},
        )
        resp.raise_for_status()
        return resp.text.strip()


async def tts_edge(text: str, voice: str = "svetlana") -> bytes:
    """Синтез речи через Microsoft Edge TTS. Абсолютно бесплатно, без API ключа."""
    voice_name = VOICES.get(voice, VOICES["svetlana"])
    communicate = edge_tts.Communicate(text, voice_name)
    
    # Собираем аудио чанки
    audio_chunks = []
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_chunks.append(chunk["data"])
    
    return b"".join(audio_chunks)


async def stt_yandex(audio_bytes: bytes) -> str:
    """Альтернатива: Yandex SpeechKit STT. Нужен YANDEX_SPEECHKIT_KEY."""
    api_key = os.getenv("YANDEX_SPEECHKIT_KEY", "")
    if not api_key: raise RuntimeError("YANDEX_SPEECHKIT_KEY не задан")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize",
            headers={"Authorization": f"Api-Key {api_key}"},
            params={"lang": "ru-RU", "format": "oggopus"},
            content=audio_bytes,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("result", "")
