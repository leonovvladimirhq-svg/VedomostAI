"""Модель данных ядра (раздел 4 плана).

Ключевые инварианты, заложенные в схему с самого начала (их дорого добавлять потом):
  * append-only оценок — GradeEntry никогда не UPDATE-ится; правка/пересдача = новая строка;
  * lineage ведомостей — Statement.parent_id для будущих ведомостей пересдачи (волна 2);
  * маркер «академ» и неудаляемость записей студента — StudentStatus;
  * версионируемая GradingScheme — «единый источник истины» расчёта.
Данные студентов минимальны (в прототипе — выдуманные), под ПДн вынесены отдельно.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.db import Base
from core.statuses import StatementStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Teacher(Base):
    """Преподаватель. В прототипе роль определяется по telegram_id (вход по /start)."""
    __tablename__ = "teachers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Group(Base):
    __tablename__ = "groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    students: Mapped[list["Student"]] = relationship(back_populates="group")


class Student(Base):
    """ПДн-минимум: ФИО + группа. В прототипе — выдуманные (см. seed/test_group.py)."""
    __tablename__ = "students"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"), index=True)
    full_name: Mapped[str] = mapped_column(String(200), index=True)
    group: Mapped["Group"] = relationship(back_populates="students")


class GradingScheme(Base):
    """Подтверждённая структура расчёта (веса, формула, округление, блокирующие).
    Версионируется — источник истины. Формула пересдачи из ПУД — поле на будущее."""
    __tablename__ = "grading_schemes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # rounding_mode: arithmetic | down | to_digit  (детали формул — ждём данные, раздел F)
    rounding_mode: Mapped[str] = mapped_column(String(20), default="arithmetic")
    # формула пересдачи из ПУД (может отличаться) — заполнится в волне 2
    retake_formula: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    elements: Mapped[list["ControlElement"]] = relationship(back_populates="scheme")


class ControlElement(Base):
    """Элемент контроля: тип, вес, блокирующий признак/порог, режим округления.
    Плановая дата — необязательна, вводится по модулю (для аномалий/напоминаний)."""
    __tablename__ = "control_elements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scheme_id: Mapped[int] = mapped_column(ForeignKey("grading_schemes.id"), index=True)
    name: Mapped[str] = mapped_column(String(150))
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    # single = одна оценка (правка = последняя актуальна); average = среднее по вводам
    aggregation: Mapped[str] = mapped_column(String(10), default="single")
    gates_total: Mapped[bool] = mapped_column(Boolean, default=False)  # нет вводов -> итог 0
    is_blocking: Mapped[bool] = mapped_column(Boolean, default=False)
    blocking_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    planned_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    scheme: Mapped["GradingScheme"] = relationship(back_populates="elements")


class Statement(Base):
    """Ведомость на курс×модуль. Статус — конечный автомат (statuses.py).
    parent_id — lineage для ведомостей пересдачи (волна 2)."""
    __tablename__ = "statements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("teachers.id"), index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id"))
    scheme_id: Mapped[int | None] = mapped_column(ForeignKey("grading_schemes.id"), nullable=True)
    course_name: Mapped[str] = mapped_column(String(200), default="")
    module: Mapped[str] = mapped_column(String(50), default="")
    status: Mapped[StatementStatus] = mapped_column(
        Enum(StatementStatus), default=StatementStatus.DRAFT
    )
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("statements.id"), nullable=True)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class GradeEntry(Base):
    """Запись оценки — APPEND-ONLY. Никогда не UPDATE: правка/повтор/пересдача = новая
    строка; исходные данные сохраняются (аудит, восстановление, заполнение СЭВ).
    Актуальная оценка элемента = последняя по created_at."""
    __tablename__ = "grade_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("statements.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    element_id: Mapped[int] = mapped_column(ForeignKey("control_elements.id"), index=True)
    value: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(20))  # buttons | text | voice | import
    author_teacher_id: Mapped[int] = mapped_column(ForeignKey("teachers.id"))
    raw_input: Mapped[str | None] = mapped_column(Text, nullable=True)  # исходная фраза/строка
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)


class StudentStatus(Base):
    """Пометки студента в ведомости (маркер «академ» и пр.). Не удаляется технически."""
    __tablename__ = "student_statuses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    statement_id: Mapped[int] = mapped_column(ForeignKey("statements.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    marker: Mapped[str] = mapped_column(String(30))  # напр. "academ"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ConsentRecord(Base):
    """Аудит согласий на обработку ПДн (152-ФЗ). APPEND-ONLY: каждое действие —
    новая строка. Актуальность = последняя запись со status='accepted' по текущей
    версии документа (см. core/services/consent_service.py)."""
    __tablename__ = "consent_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(Integer, index=True)
    doc_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # accepted | declined | revoked
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Feedback(Base):
    """Обратная связь преподавателя: 👍/👎 + необязательный комментарий.
    context — где оставлена (menu | pud | totals); ref_id — привязка (напр. statement_id)."""
    __tablename__ = "feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    teacher_id: Mapped[int | None] = mapped_column(ForeignKey("teachers.id"), nullable=True, index=True)
    telegram_id: Mapped[int] = mapped_column(Integer, index=True)
    context: Mapped[str] = mapped_column(String(32), default="menu")
    rating: Mapped[str] = mapped_column(String(8))  # up | down
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
