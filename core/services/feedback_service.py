"""Обратная связь преподавателя: сохранение оценки 👍/👎 и комментария.

Паттерн из TutorAI: рейтинг фиксируется сразу, комментарий — опционально
следующим сообщением. Контекст (menu | pud | totals) и ref_id (напр. statement_id)
позволяют потом видеть, на что именно реагируют преподаватели.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import Feedback


def add_feedback(
    session: Session, *, telegram_id: int, rating: str,
    context: str = "menu", teacher_id: int | None = None, ref_id: int | None = None,
) -> Feedback:
    fb = Feedback(telegram_id=telegram_id, rating=rating, context=context,
                  teacher_id=teacher_id, ref_id=ref_id)
    session.add(fb)
    session.commit()
    return fb


def set_comment(session: Session, feedback_id: int, comment: str) -> None:
    fb = session.get(Feedback, feedback_id)
    if fb is not None:
        fb.comment = comment
        session.commit()


def summary(session: Session) -> dict[str, int]:
    """Агрегаты для дашборда/отладки: сколько 👍 и 👎, всего с комментариями."""
    rows = session.scalars(select(Feedback)).all()
    return {
        "total": len(rows),
        "up": sum(1 for r in rows if r.rating == "up"),
        "down": sum(1 for r in rows if r.rating == "down"),
        "with_comment": sum(1 for r in rows if r.comment),
    }
