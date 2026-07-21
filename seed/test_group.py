"""Выдуманная тестовая группа из 20 студентов (без ПДн — рандомные ФИО).

Чтобы в прототипе не упираться в ФЗ-152 (вопрос 11): все данные вымышлены.
Идемпотентно: повторный запуск не плодит дубли.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.models import Group, Student

GROUP_NAME = "МК-2026 (тестовая)"

STUDENTS = [
    "Иванов Иван Иванович", "Петров Пётр Петрович", "Смирнова Анна Сергеевна",
    "Кузнецова Мария Дмитриевна", "Соколов Артём Андреевич", "Попова Елизавета Игоревна",
    "Лебедев Максим Олегович", "Козлова Дарья Александровна", "Новиков Егор Викторович",
    "Морозова Софья Павловна", "Волков Никита Романович", "Алексеева Полина Кирилловна",
    "Фёдоров Даниил Антонович", "Михайлова Виктория Юрьевна", "Николаев Тимофей Сергеевич",
    "Орлова Ксения Максимовна", "Андреев Владислав Игоревич", "Макарова Алиса Денисовна",
    "Захаров Матвей Александрович", "Григорьева Ева Романовна",
]


def seed(session: Session) -> Group:
    group = session.scalar(select(Group).where(Group.name == GROUP_NAME))
    if group is None:
        group = Group(name=GROUP_NAME)
        session.add(group)
        session.commit()
    existing = {s.full_name for s in group.students}
    for name in STUDENTS:
        if name not in existing:
            session.add(Student(group_id=group.id, full_name=name))
    session.commit()
    return group
