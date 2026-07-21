"""Централизованная конфигурация сервиса Ведомость AI.

Все секреты читаются из окружения (.env локально, Lockbox в бою) — в коде
ничего не хардкодим. Это же требование раздела 11 плана (безопасность/ПДн).
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Telegram
    telegram_bot_token: str = ""
    allowed_teacher_ids: str = ""  # "123,456"; пусто = любой по /start (режим прототипа)

    # БД
    database_url: str = "sqlite:///vedomost.db"

    # Yandex Cloud
    yc_folder_id: str = "b1gvtru3guuc1oipcs4p"
    yc_sa_key_file: str = ""

    # Интеграции ИИ (Qwen LLM + SpeechKit). API-ключ сервисного аккаунта — из окружения.
    ai_api_key: str = Field(default="", validation_alias="YC_API_KEY")  # Api-Key SA (Qwen+SpeechKit)
    # folder в URI модели ДОЛЖЕН совпадать с домашним каталогом SA (b1gvtru3guuc1oipcs4p)
    ai_model_uri: str = "gpt://b1gvtru3guuc1oipcs4p/qwen3.6-35b-a3b/latest"
    stt_folder_id: str = "b1gvtru3guuc1oipcs4p"  # folderId для SpeechKit (тоже дом. каталог SA)

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ai_api_key)

    @property
    def allowed_ids(self) -> set[int]:
        ids = {p.strip() for p in self.allowed_teacher_ids.split(",") if p.strip()}
        return {int(i) for i in ids if i.isdigit()}

    def teacher_allowed(self, telegram_id: int) -> bool:
        """В прототипе при пустом списке пускаем всех (вход по /start)."""
        allowed = self.allowed_ids
        return telegram_id in allowed if allowed else True


settings = Settings()
BASE_DIR = Path(__file__).resolve().parent
