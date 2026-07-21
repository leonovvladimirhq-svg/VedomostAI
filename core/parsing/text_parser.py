"""Разбор текста-потока: «Иванов 8, Петров 3» -> [(студент, элемент, оценка)].

СТАТУС: заглушка интерфейса. Реализация — через Yandex AI Studio (лёгкий LLM),
доступ есть через ваш Yandex Cloud. Для включения нужно:
  * подтвердить модель AI Studio (в плане — Qwen3-235B) и выдать роль ai.languageModels.user;
  * список студентов группы (для сопоставления ФИО, разрешения однофамильцев);
  * список элементов контроля (чтобы понять, за какой контроль оценка).
Результат ВСЕГДА показывается преподавателю на подтверждение (цена ошибки в ФИО/цифре).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ParsedGrade:
    student_query: str   # как распознано ФИО (для матчинга/уточнения однофамильцев)
    element_query: str   # к какому элементу контроля отнесено
    value: float
    raw: str             # исходный фрагмент


def parse_text(text: str, roster: list[str], elements: list[str]) -> list[ParsedGrade]:  # pragma: no cover
    raise NotImplementedError(
        "NLP-разбор ввода подключается через Yandex AI Studio. См. список материалов."
    )
