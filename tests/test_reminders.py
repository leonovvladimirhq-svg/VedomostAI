"""Тесты контура 4 (напоминания) и валидации шкалы оценок (детектор «вне диапазона»)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from core.db import Base
from core.models import GradeEntry, Group, Statement, Teacher
from core.services import reminder_service as rem
from core.services.grading_service import GRADE_MAX, GRADE_MIN, element_max
from core.statuses import StatementStatus


@pytest.fixture()
def session():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    with Session(eng, expire_on_commit=False, future=True) as s:
        yield s


def _mk(session, *, status=StatementStatus.FILLING, created_ago_h=0.0, tg_id=555):
    t = Teacher(telegram_id=tg_id, full_name="Преп")
    g = Group(name="Гр")
    session.add_all([t, g]); session.commit()
    st = Statement(teacher_id=t.id, group_id=g.id, course_name="Курс", status=status)
    st.created_at = datetime.now(timezone.utc) - timedelta(hours=created_ago_h)
    session.add(st); session.commit()
    return t, st


def test_stale_when_no_activity_beyond_threshold(session):
    _mk(session, created_ago_h=30)  # создана 30ч назад, оценок нет
    now = datetime.now(timezone.utc)
    stale = rem.find_stale(session, now, inactivity_hours=24)
    assert len(stale) == 1
    assert stale[0].idle >= timedelta(hours=24)


def test_not_stale_with_recent_grade(session):
    _t, st = _mk(session, created_ago_h=100)
    e = GradeEntry(statement_id=st.id, student_id=1, element_id=1, value=8,
                   source="buttons", author_teacher_id=st.teacher_id)
    e.created_at = datetime.now(timezone.utc) - timedelta(hours=2)  # свежий «заход»
    session.add(e); session.commit()
    stale = rem.find_stale(session, datetime.now(timezone.utc), inactivity_hours=24)
    assert stale == []


def test_closed_statement_ignored(session):
    _mk(session, status=StatementStatus.CLOSED, created_ago_h=100)
    stale = rem.find_stale(session, datetime.now(timezone.utc), inactivity_hours=24)
    assert stale == []


def test_skip_months(session):
    _mk(session, created_ago_h=100)
    now = datetime.now(timezone.utc)
    cur_msk_month = now.astimezone(rem.MSK).month
    assert rem.find_stale(session, now, 24, skip_months={cur_msk_month}) == []


def test_grade_scale_bounds():
    assert (GRADE_MIN, GRADE_MAX) == (0.0, 10.0)
    assert element_max(object()) == 10.0            # нет max_score -> шкала 10

    class E:  # задел на будущее: у элемента свой максимум
        max_score = 5
    assert element_max(E()) == 5.0
