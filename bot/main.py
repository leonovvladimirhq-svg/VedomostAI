"""Telegram-бот Ведомость AI — точка входа (aiogram 3).

Бот — ТОНКИЙ слой над core.services: разбирает апдейты и вызывает доменную логику.
Сквозной поток итерации 1: создать ведомость (пресет/формула ПУД) -> ввод оценок
(кнопки) -> расчёт движком -> показать -> выгрузить Excel. Голос/текст-поток
подключаются в тот же конвейер записи оценок следующим шагом.

Запуск: python -m bot.main (из папки app, при заполненном .env)
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from config import settings
from core.db import SessionLocal, init_db
from core.export.excel import build_ledger_from_statement
from core.parsing.pud_parser import parse_formula
from core.services import statement_service as svc
from core.services.grading_service import seminar_scheme
from seed.test_group import seed as seed_group

logging.basicConfig(level=logging.INFO)
dp = Dispatcher(storage=MemoryStorage())


class Flow(StatesGroup):
    waiting_formula = State()   # ручной ввод формулы ПУД
    entering_value = State()    # ввод числовой оценки после выбора элемента+студента


# --- Клавиатуры ---
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Новая ведомость", callback_data="new")],
        [InlineKeyboardButton(text="✍️ Ввести оценки", callback_data="enter")],
        [InlineKeyboardButton(text="👀 Показать ведомость", callback_data="show")],
        [InlineKeyboardButton(text="📊 Выгрузить Excel", callback_data="export")],
    ])


def preset_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎭 Драматургия (ПУД, 4 элемента)", callback_data="preset:dram")],
        [InlineKeyboardButton(text="📝 Семинарская (0.6 занятия + 0.4 отчёт)", callback_data="preset:sem")],
        [InlineKeyboardButton(text="⌨️ Ввести формулу вручную", callback_data="preset:manual")],
    ])


def elements_kb(elements) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{e.name} ({e.weight:g})", callback_data=f"el:{e.id}")]
            for e in elements]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def students_kb(students) -> InlineKeyboardMarkup:
    rows, row = [], []
    for i, s in enumerate(students, 1):
        surname = s.full_name.split()[0]
        row.append(InlineKeyboardButton(text=f"{i}. {surname}", callback_data=f"stu:{s.id}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


DRAMATURGY_FORMULA = ("Активность * 0.1 + Блиц * 0.2 + "
                      "Тест по лекционным материалам: Викторина * 0.2 + Контрольная работа * 0.5")


# --- Хендлеры ---
@dp.message(CommandStart())
async def on_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    if not settings.teacher_allowed(message.from_user.id):
        await message.answer("Доступ ограничен списком преподавателей.")
        return
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, message.from_user.id, message.from_user.full_name or "")
        group = seed_group(s)
        count = len(svc.roster(s, group))
    await message.answer(
        f"Здравствуйте, {teacher.full_name or 'преподаватель'}!\n"
        f"Это <b>Ведомость AI</b> — прототип.\n"
        f"Тестовая группа: <b>{group.name}</b> ({count} студентов).",
        reply_markup=main_menu(), parse_mode="HTML",
    )


@dp.callback_query(F.data == "new")
async def on_new(cb: CallbackQuery) -> None:
    await cb.message.answer("Выберите структуру ведомости (из ПУД):", reply_markup=preset_menu())
    await cb.answer()


async def _create(cb: CallbackQuery, engine_scheme, course: str, module: str) -> None:
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, cb.from_user.id)
        group = seed_group(s)
        st = svc.create_statement_with_scheme(s, teacher, group, engine_scheme,
                                              course_name=course, module=module)
        els = svc.scheme_elements(s, st)
        names = "\n".join(f"• {e.name} — вес {e.weight:g}" for e in els)
        sid = st.id
    await cb.message.answer(
        f"✅ Создана ведомость #{sid}: <b>{course}</b> ({module}), статус «Заполняется».\n"
        f"Элементы контроля:\n{names}\n\nТеперь можно вводить оценки — «✍️ Ввести оценки».",
        reply_markup=main_menu(), parse_mode="HTML",
    )


@dp.callback_query(F.data == "preset:dram")
async def on_preset_dram(cb: CallbackQuery) -> None:
    await _create(cb, parse_formula(DRAMATURGY_FORMULA), "Драматургия в рекламе и PR", "3 модуль")
    await cb.answer()


@dp.callback_query(F.data == "preset:sem")
async def on_preset_sem(cb: CallbackQuery) -> None:
    await _create(cb, seminar_scheme(), "Семинарская ведомость", "3–4 модули")
    await cb.answer()


@dp.callback_query(F.data == "preset:manual")
async def on_preset_manual(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Flow.waiting_formula)
    await cb.message.answer(
        "Пришлите формулу оценивания в формате:\n"
        "<code>Активность * 0.1 + Блиц * 0.2 + Контрольная * 0.7</code>",
        parse_mode="HTML",
    )
    await cb.answer()


@dp.message(Flow.waiting_formula, F.text)
async def on_formula(message: Message, state: FSMContext) -> None:
    try:
        scheme = parse_formula(message.text.strip())
    except Exception:
        await message.answer("Не удалось разобрать формулу. Пример: «Актив * 0.3 + Экзамен * 0.7».")
        return
    await state.clear()
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, message.from_user.id)
        group = seed_group(s)
        st = svc.create_statement_with_scheme(s, teacher, group, scheme,
                                              course_name="Ведомость по формуле", module="")
        els = svc.scheme_elements(s, st)
        names = "\n".join(f"• {e.name} — вес {e.weight:g}" for e in els)
        sid = st.id
    await message.answer(
        f"✅ Создана ведомость #{sid}. Элементы:\n{names}",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "enter")
async def on_enter(cb: CallbackQuery) -> None:
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, cb.from_user.id)
        st = svc.active_statement(s, teacher)
        if st is None:
            await cb.message.answer("Сначала создайте ведомость — «📄 Новая ведомость».")
            await cb.answer()
            return
        els = svc.scheme_elements(s, st)
    await cb.message.answer("Выберите элемент контроля:", reply_markup=elements_kb(els))
    await cb.answer()


@dp.callback_query(F.data.startswith("el:"))
async def on_pick_element(cb: CallbackQuery, state: FSMContext) -> None:
    element_id = int(cb.data.split(":")[1])
    await state.update_data(element_id=element_id)
    from core.models import Group
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, cb.from_user.id)
        st = svc.active_statement(s, teacher)
        group = s.get(Group, st.group_id)
        students = svc.roster(s, group)
    await cb.message.answer("Выберите студента:", reply_markup=students_kb(students))
    await cb.answer()


@dp.callback_query(F.data.startswith("stu:"))
async def on_pick_student(cb: CallbackQuery, state: FSMContext) -> None:
    student_id = int(cb.data.split(":")[1])
    await state.update_data(student_id=student_id)
    await state.set_state(Flow.entering_value)
    await cb.message.answer("Введите оценку (число 0–10):")
    await cb.answer()


@dp.message(Flow.entering_value, F.text)
async def on_value(message: Message, state: FSMContext) -> None:
    raw = message.text.strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        await message.answer("Нужно число 0–10. Повторите ввод.")
        return
    if not 0 <= value <= 10:
        await message.answer("Оценка вне шкалы 0–10. Повторите ввод.")
        return
    data = await state.get_data()
    element_id, student_id = data.get("element_id"), data.get("student_id")
    from core.models import ControlElement, Student
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, message.from_user.id)
        st = svc.active_statement(s, teacher)
        student = s.get(Student, student_id)
        element = s.get(ControlElement, element_id)
        svc.add_grade_entry(s, st, student, element, value, "buttons", teacher, raw_input=message.text)
        res = svc.student_total(s, st, student)
        surname = student.full_name.split()[0]
        elname = element.name
        total = res.total
        els = svc.scheme_elements(s, st)
    await state.set_state(None)
    await message.answer(
        f"✅ Записано: {surname} — {elname} = {value:g}. Текущий итог: <b>{total:g}</b>.\n"
        f"Продолжить ввод — выберите элемент:",
        reply_markup=elements_kb(els), parse_mode="HTML",
    )


@dp.callback_query(F.data == "show")
async def on_show(cb: CallbackQuery) -> None:
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, cb.from_user.id)
        st = svc.active_statement(s, teacher)
        if st is None:
            await cb.message.answer("Нет активной ведомости. Создайте — «📄 Новая ведомость».")
            await cb.answer()
            return
        from core.models import Group
        group = s.get(Group, st.group_id)
        students = svc.roster(s, group)
        lines = []
        for i, stu in enumerate(students, 1):
            res = svc.student_total(s, st, stu)
            surname = " ".join(stu.full_name.split()[:2])
            lines.append(f"{i}. {surname} — итог: {res.total:g}")
        course = st.course_name
    text = f"<b>{course}</b>\n" + "\n".join(lines)
    await cb.message.answer(text, parse_mode="HTML", reply_markup=main_menu())
    await cb.answer()


@dp.callback_query(F.data == "export")
async def on_export(cb: CallbackQuery) -> None:
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, cb.from_user.id)
        st = svc.active_statement(s, teacher)
        if st is None:
            await cb.message.answer("Нет активной ведомости для выгрузки.")
            await cb.answer()
            return
        wb = build_ledger_from_statement(s, st)
        course = st.course_name
    tmp = Path(tempfile.gettempdir()) / f"vedomost_{st.id}.xlsx"
    wb.save(tmp)
    await cb.message.answer_document(FSInputFile(tmp, filename=f"Ведомость — {course}.xlsx"))
    await cb.answer()
    try:
        tmp.unlink()
    except OSError:
        pass


@dp.message(F.voice)
async def on_voice(message: Message) -> None:
    await message.answer("🎙 Голосовой ввод подключается следующим шагом (SpeechKit + Qwen).")


@dp.message(F.text)
async def on_text_fallback(message: Message) -> None:
    await message.answer("Наберите /start для меню. Разбор текста-потока оценок — следующий шаг.")


async def _run() -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN в .env.")
    init_db()
    bot = Bot(token=settings.telegram_bot_token)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_run())
