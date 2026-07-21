"""Импорт ПУД -> структура расчёта (контур 1).

Хорошая новость по «машиночитаемости ПУД» (открытый вопрос плана, раздел 14):
Конструктор ПУД ВШЭ (dp.hse.ru) выдаёт ФОРМУЛУ ОЦЕНИВАНИЯ явным текстом, напр.:
  "Активность * 0.1 + Блиц * 0.2 + Тест ... : Викторина * 0.2 + Контрольная работа * 0.5"
Такую формулу разбираем детерминированно, без LLM. LLM понадобится только для
«грязных» ПУД (скан/произвольный формат) — как запасной путь.
"""
from __future__ import annotations

import re
import unicodedata

from core.services.grading_service import Element, Scheme

# слагаемое вида "<название> * <вес>"
_TERM_RE = re.compile(r"^\s*(?P<name>.+?)\s*\*\s*(?P<weight>[0-9]+(?:[.,][0-9]+)?)\s*$")


def _slug(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode() or name
    s = re.sub(r"[^0-9a-zA-Zа-яА-Я]+", "_", name.lower()).strip("_")
    return s[:40] or "el"


def parse_formula(formula: str, rounding: str = "arithmetic") -> Scheme:
    """Разбирает строку формулы в Scheme. Веса должны суммироваться ~1.0."""
    elements: list[Element] = []
    for i, term in enumerate(formula.split("+")):
        m = _TERM_RE.match(term)
        if not m:
            continue
        name = m.group("name").strip()
        weight = float(m.group("weight").replace(",", "."))
        # single = одна оценка за элемент, правка перезаписывает (latest-wins). Усреднение
        # нескольких контролей (Блиц×2, семинары) — отдельный режим, задаётся вручную.
        elements.append(Element(key=f"{_slug(name)}_{i}", name=name, weight=weight,
                                aggregation="single"))
    if not elements:
        raise ValueError(f"Не удалось разобрать формулу: {formula!r}")
    return Scheme(elements=elements, rounding=rounding)


def extract_formula_from_html_text(text: str) -> str | None:
    """Достаёт строку формулы из текста страницы Конструктора ПУД."""
    lines = [l.strip() for l in text.splitlines()]
    for i, l in enumerate(lines):
        if l.startswith("Формула оценивания") and i + 1 < len(lines):
            cand = lines[i + 1]
            if "*" in cand:
                return cand
    return None
