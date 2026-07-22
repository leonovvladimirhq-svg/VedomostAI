"""Расчётный движок — ДЕТЕРМИНИРОВАННОЕ ядро (раздел 5 плана). Без LLM.

Чистая функция: (Scheme + оценки по элементам) -> (разбивка, итог). Никаких side-effect.
Проверяется против реальной Excel-ведомости «число в число» (scripts/validate_engine.py).

Смоделировано по двум реальным примерам:
  * семинарская ведомость: Итог = ROUND(0.6*avg(занятия) + 0.4*Отчёт, 0),
    и Итог=0, если посещений нет (гейт по элементу);
  * ПУД «Драматургия в рекламе и PR»: Активность*0.1 + Блиц*0.2 + Викторина*0.2 + КР*0.5.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from statistics import mean


# --- Округление ---
def round_half_away(x: float, ndigits: int = 0) -> float:
    """Округление как Excel ROUND (половина — от нуля), НЕ банковское round() Python.
    Проверено: 4.5 -> 5, 4.44 -> 4, 2.5 -> 3."""
    factor = 10 ** ndigits
    y = math.floor(abs(x) * factor + 0.5) / factor
    return y if x >= 0 else -y


# --- Шкала оценок ВШЭ (10-балльная) ---
# Единый источник границ для валидации ввода и детектора аномалий «вне диапазона».
GRADE_MIN = 0.0
GRADE_MAX = 10.0


def element_max(element) -> float:
    """Максимальный балл за элемент контроля. Пока шкала едина (10 баллов); поле
    max_score у элемента — задел на будущее (разные шкалы), читается через getattr."""
    return float(getattr(element, "max_score", None) or GRADE_MAX)


ROUNDINGS = {
    # арифметическое (как в примере Excel)
    "arithmetic": lambda x: round_half_away(x, 0),
    # всегда вниз (курс «Управление стратегическими коммуникациями» из плана)
    "down": lambda x: float(math.floor(x)),
    # арифметическое, но результат < 4 округляется вниз (4 — проходной; правило из ПУД)
    "arithmetic_pass4": lambda x: (float(math.floor(x)) if x < 4 else round_half_away(x, 0)),
}


@dataclass
class Element:
    key: str
    name: str
    weight: float
    aggregation: str = "average"   # "average" (среднее по вводам) | "single" (одно значение)
    gates_total: bool = False      # если нет вводов по элементу -> итог = 0 (как посещаемость)
    is_blocking: bool = False      # итог не может превышать балл за этот элемент
    blocking_threshold: float | None = None


@dataclass
class Scheme:
    elements: list[Element]
    rounding: str = "arithmetic"

    def check_weights(self) -> float:
        return round(sum(e.weight for e in self.elements), 6)


@dataclass
class GradeResult:
    aggregated: dict[str, float]   # элемент -> агрегированное значение (среднее/одно)
    weighted_raw: float            # взвешенная сумма до округления
    total: float                   # итог с округлением и блокировками
    zeroed_by_gate: bool = False


def aggregate(element: Element, values: list[float]) -> float | None:
    if not values:
        return None
    if element.aggregation == "single":
        return float(values[-1])   # актуальное = последнее (append-only)
    return float(mean(values))     # среднее (Excel AVERAGE по заполненным)


def compute(scheme: Scheme, entries: dict[str, list[float]]) -> GradeResult:
    """entries: ключ элемента -> список введённых оценок (по датам/попыткам).
    Для 'single' берётся последняя; для 'average' — среднее."""
    aggregated: dict[str, float] = {}
    weighted = 0.0
    zeroed = False

    for el in scheme.elements:
        vals = entries.get(el.key, []) or []
        agg = aggregate(el, vals)
        if el.gates_total and agg is None:
            zeroed = True
        aggregated[el.key] = agg if agg is not None else 0.0
        weighted += el.weight * (agg if agg is not None else 0.0)

    if zeroed:
        return GradeResult(aggregated=aggregated, weighted_raw=weighted, total=0.0, zeroed_by_gate=True)

    total = ROUNDINGS[scheme.rounding](weighted)

    # Блокирующие элементы: итог не может превышать балл за блокирующий (раздел 5 плана).
    for el in scheme.elements:
        if el.is_blocking and el.key in aggregated:
            total = min(total, aggregated[el.key])

    return GradeResult(aggregated=aggregated, weighted_raw=weighted, total=total)


# --- Готовые схемы из реальных примеров ---
def seminar_scheme() -> Scheme:
    """Ведомость_3-4_модули.xlsx: 0.6*среднее(занятия) + 0.4*Отчёт, гейт по занятиям."""
    return Scheme(
        elements=[
            Element(key="seminars", name="Выполнение заданий (занятия)", weight=0.6,
                    aggregation="average", gates_total=True),
            Element(key="report", name="Отчёт о работе на семинаре", weight=0.4,
                    aggregation="single"),
        ],
        rounding="arithmetic",
    )
