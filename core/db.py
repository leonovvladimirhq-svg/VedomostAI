"""Слой доступа к БД. SQLite сейчас, Managed PostgreSQL позже — меняется только
DATABASE_URL в .env, ORM (SQLAlchemy) остаётся тем же (раздел 8 плана: адаптерный
подход, чтобы переключение не затрагивало бизнес-логику)."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    """Создать таблицы. Для прототипа достаточно; в бою — Alembic-миграции."""
    from core import models  # noqa: F401  (регистрация моделей)

    Base.metadata.create_all(engine)
