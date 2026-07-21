"""Централизованная конфигурация сервиса Ведомость AI.

Все секреты читаются из окружения (.env локально, Lockbox в бою) — в коде
ничего не хардкодим. Это же требование раздела 11 плана (безопасность/ПДн).
"""
from __future__ import annotations

from pathlib import Path

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

    # Интеграции ИИ (по умолчанию выключены — включаем по мере готовности данных/прав)
    speechkit_enabled: bool = False
    ai_studio_enabled: bool = False
    ai_studio_model: str = ""

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
