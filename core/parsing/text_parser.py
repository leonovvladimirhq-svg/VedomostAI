"""Разбор текста-потока оценок через Qwen: «за блиц Иванов 8, Петров 3» ->
[(студент, элемент, оценка)]. Результат ВСЕГДА показывается преподавателю на
подтверждение (цена ошибки в ФИО/цифре высока — раздел 7 плана)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from core.services.llm import chat

_SYS = (
    "Ты извлекаешь оценки из реплики преподавателя. "
    "Верни ТОЛЬКО JSON-массив объектов вида "
    '{"student": <точное ФИО из списка студентов>, '
    '"element": <точное название элемента контроля из списка>, '
    '"value": <число оценки>}. '
    "Если элемент назван один раз для нескольких студентов — примени его ко всем. "
    "Если студента или элемента нет в списках — пропусти запись. Без пояснений, только JSON."
)


@dataclass
class ParsedGrade:
    student: str
    element: str
    value: float


def parse_grades(text: str, roster_names: list[str], element_names: list[str]) -> list[ParsedGrade]:
    usr = f"Студенты: {roster_names}\nЭлементы контроля: {element_names}\nРеплика: \"{text}\""
    content = chat([{"role": "system", "content": _SYS}, {"role": "user", "content": usr}])
    m = re.search(r"\[.*\]", content, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out: list[ParsedGrade] = []
    for x in data:
        try:
            out.append(ParsedGrade(str(x["student"]), str(x["element"]), float(x["value"])))
        except (KeyError, TypeError, ValueError):
            continue
    return out
