"""Генерация демо-ведомостей для показа (цель по ТЗ: ~5 ведомостей как Яндекс-таблицы).

Берёт реальные формулы оценивания из выгрузки ПУД ОП «Интегрированные коммуникации»
(конструктор dp.hse.ru), строит Scheme движка, заполняет 20 выдуманными студентами
с правдоподобными оценками (10-балльная шкала ВШЭ, целые), считает Итог движком,
экспортирует .xlsx и (опц.) заливает на Яндекс.Диск как редактируемую таблицу.

Запуск (из app/):
  ./.venv/Scripts/python -m scripts.generate_demo_statements            # + выгрузка на Диск
  ./.venv/Scripts/python -m scripts.generate_demo_statements --no-upload # только локально
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

from core.parsing.pud_parser import parse_formula
from core.export.excel import build_ledger
from seed.test_group import STUDENTS

# 5 «чистых» дисциплин ОП ИК 2024/2025 (веса суммируются в 1.0), формулы — как в ПУД.
DISCIPLINES = [
    ("Внутренние коммуникации", "1 модуль 2024/2025",
     "Тест: Тест * 0.2 + Проект: Проект * 0.5 + Активность: Активность * 0.3"),
    ("Коммуникации бренда на маркетплейсах и в офлайн-магазинах", "1 модуль 2024/2025",
     "Активность: Активность * 0.25 + Домашнее задание: Домашнее задание * 0.25 "
     "+ Финальный проект: Презентация * 0.5"),
    ("Коммуникации в сфере моды", "2 модуль 2024/2025",
     "Активность на семинарах: Активность * 0.2 + Домашняя работа: Домашнее задание * 0.3 "
     "+ Итоговый командный проект: Проект * 0.5"),
    ("Продуктовый маркетинг", "1 модуль 2024/2025",
     "Индивидуальные проекты: Проект * 0.5 + Командный кейс-стади проект: Проект * 0.3 "
     "+ Финальный тест: Тест * 0.2"),
    ("Психология потребителя", "4 модуль 2024/2025",
     "Доклад на основании прочитанной научной статьи: Презентация * 0.3 "
     "+ Активность на семинарах: Активность * 0.3 + Экзамен: Устный опрос * 0.4"),
]

OUT_DIR = Path(__file__).resolve().parent.parent / "demo_out"


def _grades_for_group(scheme, rng: random.Random) -> dict[str, dict[str, list[float]]]:
    """Правдоподобные оценки: у студента есть «уровень», оценки элементов рядом с ним."""
    entries: dict[str, dict[str, list[float]]] = {}
    for fio in STUDENTS:
        level = rng.randint(4, 9)  # базовый уровень студента (0..10)
        stu: dict[str, list[float]] = {}
        for el in scheme.elements:
            g = max(0, min(10, level + rng.randint(-2, 2)))
            stu[el.key] = [float(g)]
        entries[fio] = stu
    return entries


def _safe_name(course: str, module: str) -> str:
    base = f"Ведомость_{course}_{module}"
    for ch in '\\/:*?"<>|':
        base = base.replace(ch, " ")
    return " ".join(base.split()) + ".xlsx"


def main(upload: bool = True) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    rng = random.Random(2026)  # воспроизводимость

    yadisk = None
    if upload:
        from core.export import yadisk as _y
        from config import settings
        if not settings.yadisk_enabled:
            print("YANDEX_DISK_TOKEN не задан — выгрузка отключена, делаю только локально.")
        else:
            yadisk = _y

    print(f"{'Дисциплина':52} | Σвесов | Итоги(мин/сред/макс) | Ссылка")
    print("-" * 110)
    for course, module, formula in DISCIPLINES:
        scheme = parse_formula(formula)
        wsum = scheme.check_weights()
        entries = _grades_for_group(scheme, rng)

        # итоги для сводки
        from core.services.grading_service import compute
        totals = [compute(scheme, entries[fio]).total for fio in STUDENTS]

        wb = build_ledger(scheme, STUDENTS, course_name=course, module=module, entries=entries)
        fname = _safe_name(course, module)
        local = OUT_DIR / fname
        wb.save(local)

        link = "(локально)"
        if yadisk is not None:
            try:
                link = yadisk.upload_xlsx(str(local), fname) or "(залито, без публикации)"
            except Exception as e:
                link = f"ОШИБКА: {type(e).__name__}: {e}"

        summary = f"{min(totals):.0f}/{sum(totals)/len(totals):.1f}/{max(totals):.0f}"
        print(f"{course[:52]:52} | {wsum:6.2f} | {summary:20} | {link}")

    print(f"\nЛокальные файлы: {OUT_DIR}")


if __name__ == "__main__":
    main(upload="--no-upload" not in sys.argv)
