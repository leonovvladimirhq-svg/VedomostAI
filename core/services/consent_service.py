"""Согласие на обработку ПДн (152-ФЗ): версия документа, краткое содержание,
проверка актуальности, аудит и право на забвение.

Единый источник правды по согласию. Актуальность = последняя запись в
``consent_records`` со статусом ``accepted`` для текущей версии документа.
Паттерн взят из TutorAI и адаптирован под синхронный слой Ведомость AI.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from core.models import (
    ConsentRecord, GradeEntry, Statement, StudentStatus, Teacher,
)

# Текущая версия документа. При изменении текста согласия — поднять версию,
# бот пере-запросит согласие у всех пользователей.
CONSENT_VERSION = "consent_vedomost_v1"
CONSENT_DOC_PATH = Path(__file__).resolve().parent.parent / "legal" / f"{CONSENT_VERSION}.md"

OPERATOR = "Школа коммуникаций НИУ ВШЭ"
STORAGE = "Yandex Cloud, регион РФ (152-ФЗ)"

STATUS_ACCEPTED = "accepted"
STATUS_DECLINED = "declined"
STATUS_REVOKED = "revoked"


def summary() -> dict[str, str]:
    """Поля для «краткого содержания» согласия (экран 2)."""
    return {
        "operator": OPERATOR,
        "processing": "профиль преподавателя, файлы ПУД, оценки и расчёт ведомостей",
        "storage": STORAGE,
        "retention": "до отзыва — команда /forget_me",
        "version": CONSENT_VERSION,
    }


def doc_sha256() -> str:
    """SHA-256 файла согласия — для целостности/аудита (показывается при выдаче .md)."""
    return hashlib.sha256(CONSENT_DOC_PATH.read_bytes()).hexdigest()


def latest_record(session: Session, telegram_id: int) -> ConsentRecord | None:
    return session.scalar(
        select(ConsentRecord).where(ConsentRecord.telegram_id == telegram_id)
        .order_by(ConsentRecord.id.desc())
    )


def needs_consent(session: Session, telegram_id: int) -> bool:
    """True, если у пользователя нет актуального согласия на текущую версию."""
    r = latest_record(session, telegram_id)
    if r is None:
        return True
    return not (r.status == STATUS_ACCEPTED and r.doc_version == CONSENT_VERSION)


def record_consent(session: Session, telegram_id: int, status: str) -> None:
    """Зафиксировать акт согласия/отказа/отзыва по текущей версии документа."""
    session.add(ConsentRecord(telegram_id=telegram_id, doc_version=CONSENT_VERSION, status=status))
    session.commit()


def consent_status(session: Session, telegram_id: int) -> tuple[str | None, str | None]:
    """(status, doc_version) последней записи или (None, None)."""
    r = latest_record(session, telegram_id)
    return (r.status, r.doc_version) if r else (None, None)


def forget_me(session: Session, telegram_id: int) -> None:
    """Право на забвение: удаляет данные преподавателя (профиль, ведомости, оценки).
    Согласие фиксируется отдельной записью 'revoked' (аудит остаётся)."""
    teacher = session.scalar(select(Teacher).where(Teacher.telegram_id == telegram_id))
    if teacher is not None:
        st_ids = list(session.scalars(
            select(Statement.id).where(Statement.teacher_id == teacher.id)
        ).all())
        if st_ids:
            session.execute(delete(GradeEntry).where(GradeEntry.statement_id.in_(st_ids)))
            session.execute(delete(StudentStatus).where(StudentStatus.statement_id.in_(st_ids)))
            session.execute(delete(Statement).where(Statement.id.in_(st_ids)))
        # оценки, авторства этого преподавателя в прочих ведомостях (на всякий случай)
        session.execute(delete(GradeEntry).where(GradeEntry.author_teacher_id == teacher.id))
        session.delete(teacher)
    session.commit()
