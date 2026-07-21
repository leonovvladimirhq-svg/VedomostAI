"""Операции над ведомостью и оценками (контуры 1–2).

Всё append-only: `add_grade_entry` только вставляет; актуальная оценка элемента —
последняя по времени. Ничего не перезаписываем и не удаляем.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import (
    ControlElement, GradeEntry, Group, Statement, Student, Teacher,
)
from core.statuses import StatementStatus, assert_transition


def get_or_create_teacher(session: Session, telegram_id: int, full_name: str = "") -> Teacher:
    t = session.scalar(select(Teacher).where(Teacher.telegram_id == telegram_id))
    if t is None:
        t = Teacher(telegram_id=telegram_id, full_name=full_name)
        session.add(t)
        session.commit()
    return t


def create_statement(
    session: Session, teacher: Teacher, group: Group,
    course_name: str = "", module: str = "", scheme_id: int | None = None,
) -> Statement:
    st = Statement(
        teacher_id=teacher.id, group_id=group.id, scheme_id=scheme_id,
        course_name=course_name, module=module, status=StatementStatus.DRAFT,
    )
    session.add(st)
    session.commit()
    return st


def set_status(session: Session, st: Statement, dst: StatementStatus) -> None:
    assert_transition(st.status, dst)  # бросит InvalidTransition при нарушении автомата
    st.status = dst
    session.commit()


def add_grade_entry(
    session: Session, statement: Statement, student: Student,
    element: ControlElement, value: float, source: str,
    author: Teacher, raw_input: str | None = None,
) -> GradeEntry:
    """APPEND-ONLY. Повторный ввод по тому же (student, element) — новая строка."""
    entry = GradeEntry(
        statement_id=statement.id, student_id=student.id, element_id=element.id,
        value=value, source=source, author_teacher_id=author.id, raw_input=raw_input,
    )
    session.add(entry)
    session.commit()
    return entry


def current_grades(session: Session, statement: Statement) -> dict[tuple[int, int], GradeEntry]:
    """Актуальный срез: последняя запись по каждой паре (student_id, element_id)."""
    rows = session.scalars(
        select(GradeEntry)
        .where(GradeEntry.statement_id == statement.id)
        .order_by(GradeEntry.created_at.asc())
    ).all()
    latest: dict[tuple[int, int], GradeEntry] = {}
    for e in rows:  # последний по времени перезаписывает в словаре => берём актуальный
        latest[(e.student_id, e.element_id)] = e
    return latest


def roster(session: Session, group: Group) -> list[Student]:
    return session.scalars(
        select(Student).where(Student.group_id == group.id).order_by(Student.full_name)
    ).all()
