"""Операции над ведомостью и оценками (контуры 1–2).

Всё append-only: `add_grade_entry` только вставляет; актуальная оценка элемента —
последняя по времени. Ничего не перезаписываем и не удаляем.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import (
    ControlElement, GradeEntry, GradingScheme, Group, Statement, Student, Teacher,
)
from core.services.grading_service import (
    Element as EngineElement, GradeResult, Scheme as EngineScheme, compute,
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


# --- Мост БД <-> расчётный движок (контур 1–2, генерация) ---
def create_statement_with_scheme(
    session: Session, teacher: Teacher, group: Group, engine_scheme: EngineScheme,
    *, course_name: str = "", module: str = "",
) -> Statement:
    """Создаёт ведомость + сохраняет структуру расчёта (GradingScheme + элементы) из
    движковой схемы (полученной, напр., парсингом ПУД). Статус сразу «Заполняется»."""
    scheme = GradingScheme(rounding_mode=engine_scheme.rounding)
    session.add(scheme)
    session.flush()
    for e in engine_scheme.elements:
        session.add(ControlElement(
            scheme_id=scheme.id, name=e.name, weight=e.weight, aggregation=e.aggregation,
            gates_total=e.gates_total, is_blocking=e.is_blocking,
            blocking_threshold=e.blocking_threshold,
        ))
    st = Statement(
        teacher_id=teacher.id, group_id=group.id, scheme_id=scheme.id,
        course_name=course_name, module=module, status=StatementStatus.FILLING,
    )
    session.add(st)
    session.commit()
    return st


def scheme_elements(session: Session, statement: Statement) -> list[ControlElement]:
    return session.scalars(
        select(ControlElement).where(ControlElement.scheme_id == statement.scheme_id)
        .order_by(ControlElement.id)
    ).all()


def build_engine_scheme(session: Session, statement: Statement) -> EngineScheme:
    """Собирает движковую схему из БД (ключ элемента = его id как строка)."""
    els = scheme_elements(session, statement)
    scheme = session.get(GradingScheme, statement.scheme_id)
    return EngineScheme(
        elements=[EngineElement(
            key=str(e.id), name=e.name, weight=e.weight, aggregation=e.aggregation,
            gates_total=e.gates_total, is_blocking=e.is_blocking,
            blocking_threshold=e.blocking_threshold,
        ) for e in els],
        rounding=scheme.rounding_mode,
    )


def entries_for_student(session: Session, statement: Statement, student: Student) -> dict[str, list[float]]:
    """Все вводы студента по элементам (append-only, в порядке времени)."""
    rows = session.scalars(
        select(GradeEntry).where(
            GradeEntry.statement_id == statement.id,
            GradeEntry.student_id == student.id,
        ).order_by(GradeEntry.created_at.asc())
    ).all()
    d: dict[str, list[float]] = {}
    for e in rows:
        d.setdefault(str(e.element_id), []).append(e.value)
    return d


def student_total(session: Session, statement: Statement, student: Student) -> GradeResult:
    return compute(build_engine_scheme(session, statement), entries_for_student(session, statement, student))


def active_statement(session: Session, teacher: Teacher) -> Statement | None:
    """Последняя ведомость преподавателя в статусе «Заполняется»."""
    return session.scalar(
        select(Statement).where(
            Statement.teacher_id == teacher.id,
            Statement.status == StatementStatus.FILLING,
        ).order_by(Statement.id.desc())
    )


# --- Сопоставление распознанного (текст/голос) с БД ---
def match_student(students: list[Student], query: str) -> Student | None:
    q = (query or "").strip().lower()
    if not q:
        return None
    for st in students:  # точное ФИО
        if st.full_name.lower() == q:
            return st
    surname = q.split()[0]  # по фамилии (первое слово)
    cands = [st for st in students if st.full_name.lower().split()[0] == surname]
    return cands[0] if len(cands) == 1 else None


def match_element(elements: list[ControlElement], query: str) -> ControlElement | None:
    q = (query or "").strip().lower()
    if not q:
        return None
    for e in elements:  # точное имя
        if e.name.lower() == q:
            return e
    for e in elements:  # частичное совпадение
        if q in e.name.lower() or e.name.lower() in q:
            return e
    return None
