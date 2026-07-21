"""Голосовой ввод: аудио из Telegram (OGG/Opus) -> текст (Yandex SpeechKit STT v1).

Telegram голосовые приходят в oggopus — SpeechKit принимает их напрямую, без
перекодирования. Синхронное распознавание: до ~30с / 1 МБ (реплики оценок короткие).
Аудио временное, после транскрипции не хранится (раздел 11 плана, ПДн).
"""
from __future__ import annotations

import httpx

from config import settings

_ENDPOINT = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"


class STTError(Exception):
    pass


def transcribe(audio_bytes: bytes, *, lang: str = "ru-RU") -> str:
    if not settings.ai_api_key:
        raise STTError("Не задан YC_API_KEY.")
    params = {"folderId": settings.stt_folder_id, "lang": lang, "format": "oggopus"}
    r = httpx.post(_ENDPOINT, params=params,
                   headers={"Authorization": f"Api-Key {settings.ai_api_key}"},
                   content=audio_bytes, timeout=60)
    if r.status_code != 200:
        raise STTError(f"STT {r.status_code}: {r.text[:200]}")
    return r.json().get("result", "")
