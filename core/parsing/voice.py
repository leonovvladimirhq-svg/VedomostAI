"""Голосовой ввод: аудио из Telegram -> текст (Yandex SpeechKit) -> тот же NLP-разбор.

СТАТУС: заглушка. SpeechKit доступен через ваш Yandex Cloud (подтверждено: биллинг
активен). Для включения нужно:
  * роль ai.speechRecognition.user на сервисном аккаунте leonov-deployer
    (или отдельный API-ключ SpeechKit);
  * решение по формату: короткое распознавание (<30с, синхронное) достаточно для
    фразы «Иванов получил 5 баллов» — берём его для прототипа.
Аудиофайлы временные: удаляются сразу после транскрипции (раздел 11 плана, ПДн).
"""
from __future__ import annotations


def transcribe(audio_bytes: bytes, *, sample_rate: int = 48000, lang: str = "ru-RU") -> str:  # pragma: no cover
    raise NotImplementedError(
        "Транскрипция подключается через Yandex SpeechKit. См. список материалов."
    )
