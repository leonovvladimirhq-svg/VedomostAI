"""Напоминания преподавателю по неактивным ведомостям (контур 4).

Правило (решено Владимиром): если по ведомости в статусе «Заполняется» преподаватель
N часов не вносил оценок (не «заходил»), присылаем ему напоминание в Telegram.
Прод-порог = 10 дней (240 ч), тест = 24 ч — задаётся через REMINDER_INACTIVITY_HOURS.

Детерминированный поиск, без side-effect: рассылка — на стороне бота (bot.reminders).
Все метки времени в БД трактуем как UTC (SQLite не хранит tz). Месяцы-исключения
(напр. лето) считаем по московскому времени.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import GradeEntry, Statement, Teacher
from core.statuses import StatementStatus

try:  # на VM (Ubuntu, tzdata) отработает ZoneInfo; иначе — фиксированный UTC+3
    from zoneinfo import ZoneInfo
    MSK = ZoneInfo("Europe/Moscow")
except Exception:  # pragma: no cover
    MSK = timezone(timedelta(hours=3))


@dataclass
class StaleStatement:
    statement_id: int
    course_name: str
    teacher_id: int
    telegram_id: int
    last_activity: datetime  # UTC, tz-aware
    idle: timedelta


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _last_activity(session: Session, st: Statement) -> datetime:
    """Последний «заход» — время последней внесённой оценки; иначе — создание ведомости."""
    last = session.scalar(
        select(GradeEntry.created_at)
        .where(GradeEntry.statement_id == st.id)
        .order_by(GradeEntry.created_at.desc())
    )
    return _as_utc(last or st.created_at)


def find_stale(
    session: Session, now: datetime, inactivity_hours: float,
    skip_months: frozenset[int] | set[int] = frozenset(),
) -> list[StaleStatement]:
    """Ведомости «Заполняется» без активности >= порога. now — tz-aware UTC."""
    if now.astimezone(MSK).month in skip_months:
        return []
    threshold = timedelta(hours=inactivity_hours)
    result: list[StaleStatement] = []
    stmts = session.scalars(
        select(Statement).where(
            Statement.status == StatementStatus.FILLING,
            Statement.archived.is_(False),
        )
    ).all()
    for st in stmts:
        last = _last_activity(session, st)
        idle = now - last
        if idle < threshold:
            continue
        teacher = session.get(Teacher, st.teacher_id)
        if teacher is None or not teacher.telegram_id:
            continue
        result.append(StaleStatement(
            statement_id=st.id,
            course_name=st.course_name or f"#{st.id}",
            teacher_id=teacher.id,
            telegram_id=teacher.telegram_id,
            last_activity=last,
            idle=idle,
        ))
    return result


def humanize_idle(idle: timedelta) -> str:
    days = idle.days
    if days >= 1:
        return f"{days} дн."
    hours = int(idle.total_seconds() // 3600)
    if hours >= 1:
        return f"{hours} ч."
    return f"{int(idle.total_seconds() // 60)} мин."
