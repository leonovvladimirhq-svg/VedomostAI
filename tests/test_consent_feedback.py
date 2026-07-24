"""Тесты согласия на ПДн (152-ФЗ), права на забвение и обратной связи."""
from __future__ import annotations

import re

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from core.db import Base
from core.models import Feedback, GradeEntry, Group, Statement, Teacher
from core.services import consent_service as consent
from core.services import feedback_service as fb
from core.statuses import StatementStatus


@pytest.fixture()
def session():
    eng = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(eng)
    with Session(eng, expire_on_commit=False, future=True) as s:
        yield s


def test_needs_consent_lifecycle(session):
    tg = 100
    assert consent.needs_consent(session, tg) is True          # нет записей
    consent.record_consent(session, tg, consent.STATUS_ACCEPTED)
    assert consent.needs_consent(session, tg) is False         # согласие дано
    consent.record_consent(session, tg, consent.STATUS_REVOKED)
    assert consent.needs_consent(session, tg) is True          # отозвано → снова нужно


def test_declined_needs_consent(session):
    tg = 101
    consent.record_consent(session, tg, consent.STATUS_DECLINED)
    assert consent.needs_consent(session, tg) is True


def test_version_bump_requires_reconsent(session):
    tg = 102
    consent.record_consent(session, tg, consent.STATUS_ACCEPTED)
    assert consent.needs_consent(session, tg) is False
    # эмулируем «старую» версию: запись есть, но версия не текущая
    rec = consent.latest_record(session, tg)
    rec.doc_version = "consent_vedomost_v0"
    session.commit()
    assert consent.needs_consent(session, tg) is True


def test_doc_sha256_stable_and_hex():
    h1, h2 = consent.doc_sha256(), consent.doc_sha256()
    assert h1 == h2
    assert re.fullmatch(r"[0-9a-f]{64}", h1)
    assert consent.CONSENT_DOC_PATH.exists()


def test_forget_me_removes_teacher_data(session):
    t = Teacher(telegram_id=200, full_name="Преп")
    g = Group(name="Гр")
    session.add_all([t, g]); session.commit()
    st = Statement(teacher_id=t.id, group_id=g.id, course_name="Курс",
                   status=StatementStatus.FILLING)
    session.add(st); session.commit()
    session.add(GradeEntry(statement_id=st.id, student_id=1, element_id=1, value=8,
                           source="buttons", author_teacher_id=t.id))
    session.commit()

    consent.record_consent(session, 200, consent.STATUS_REVOKED)
    consent.forget_me(session, 200)

    assert session.get(Teacher, t.id) is None
    assert session.scalars(select(Statement).where(Statement.teacher_id == t.id)).all() == []
    assert session.scalars(select(GradeEntry).where(GradeEntry.statement_id == st.id)).all() == []


def test_feedback_add_comment_summary(session):
    e1 = fb.add_feedback(session, telegram_id=300, rating="up", context="menu")
    fb.add_feedback(session, telegram_id=300, rating="down", context="pud", ref_id=5)
    fb.set_comment(session, e1.id, "класс")
    s = fb.summary(session)
    assert s == {"total": 2, "up": 1, "down": 1, "with_comment": 1}
    stored = session.get(Feedback, e1.id)
    assert stored.comment == "класс"
