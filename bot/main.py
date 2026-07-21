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
from core.parsing.pud_ingest import extract_text, find_formula, find_title
from core.parsing.pud_parser import parse_formula
from core.parsing.text_parser import parse_grades
from core.parsing.voice import transcribe
from core.services import statement_service as svc
from seed.test_group import seed as seed_group

logging.basicConfig(level=logging.INFO)
dp = Dispatcher(storage=MemoryStorage())


class Flow(StatesGroup):
    waiting_pud = State()          # ожидание файла ПУД (или вставленной формулы)
    confirming = State()           # подтверждение распознанной структуры ПУД
    entering_value = State()       # ввод числовой оценки после выбора элемента+студента
    confirming_grades = State()    # подтверждение оценок, распознанных из текста/голоса


# --- Клавиатуры ---
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Новая ведомость", callback_data="new")],
        [InlineKeyboardButton(text="✍️ Ввести оценки", callback_data="enter")],
        [InlineKeyboardButton(text="👀 Показать ведомость", callback_data="show")],
        [InlineKeyboardButton(text="📊 Выгрузить Excel", callback_data="export")],
    ])


def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить и создать", callback_data="pud_ok")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="pud_cancel")],
    ])


def grades_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Записать", callback_data="grades_ok")],
        [InlineKeyboardButton(text="✖️ Отмена", callback_data="grades_cancel")],
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
async def on_new(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Flow.waiting_pud)
    await cb.message.answer(
        "Пришлите ваш <b>ПУД файлом</b> (PDF, DOCX или HTML из конструктора dp.hse.ru) — "
        "я извлеку элементы контроля и веса из вашей формулы оценивания.\n\n"
        "Можно также вставить раздел «Система оценивания» или формулу текстом.",
        parse_mode="HTML",
    )
    await cb.answer()


async def _present_detected(message: Message, state: FSMContext, text: str) -> None:
    """Показывает распознанную из ПУД структуру и просит подтверждение."""
    formula = find_formula(text)
    if not formula:
        await message.answer(
            "Не нашёл формулу оценивания в ПУД. Пришлите раздел «Система оценивания» "
            "текстом или другой файл (PDF/DOCX/HTML)."
        )
        return
    try:
        scheme = parse_formula(formula)
    except Exception:
        await message.answer("Формула найдена, но не разобралась. Пришлите её текстом.")
        return
    title = find_title(text)
    await state.update_data(formula=formula, title=title)
    await state.set_state(Flow.confirming)
    names = "\n".join(f"• {e.name} — вес {e.weight:g}" for e in scheme.elements)
    total_w = scheme.check_weights()
    warn = "" if total_w == 1.0 else f"\n⚠️ Сумма весов = {total_w:g} (обычно 1.0)"
    await message.answer(
        f"📋 Распознал ПУД: <b>{title}</b>\nЭлементы контроля:\n{names}{warn}\n\nВсё верно?",
        reply_markup=confirm_kb(), parse_mode="HTML",
    )


@dp.message(Flow.waiting_pud, F.document)
async def on_pud_document(message: Message, state: FSMContext) -> None:
    doc = message.document
    suffix = Path(doc.file_name or "pud").suffix or ".bin"
    tmp = Path(tempfile.gettempdir()) / f"pud_{message.from_user.id}{suffix}"
    await message.bot.download(doc, destination=tmp)
    try:
        text = extract_text(str(tmp))
    except Exception as e:
        await message.answer(f"Не смог прочитать файл: {e}")
        return
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    await _present_detected(message, state, text)


@dp.message(Flow.waiting_pud, F.text)
async def on_pud_text(message: Message, state: FSMContext) -> None:
    await _present_detected(message, state, message.text)


@dp.callback_query(Flow.confirming, F.data == "pud_ok")
async def on_pud_ok(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    formula, title = data.get("formula"), data.get("title", "Ведомость по ПУД")
    await state.clear()
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, cb.from_user.id)
        group = seed_group(s)
        st = svc.create_statement_with_scheme(s, teacher, group, parse_formula(formula),
                                              course_name=title, module="")
        sid = st.id
    await cb.message.answer(
        f"✅ Ведомость #{sid} создана: <b>{title}</b>, статус «Заполняется».\n"
        f"Теперь вводите оценки — «✍️ Ввести оценки».",
        reply_markup=main_menu(), parse_mode="HTML",
    )
    await cb.answer()


@dp.callback_query(F.data == "pud_cancel")
async def on_pud_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.answer("Отменил. /start — меню.")
    await cb.answer()


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


async def _handle_grades(message: Message, state: FSMContext, text: str, source: str) -> None:
    """Общий конвейер для оценок из текста и голоса: разбор Qwen -> сопоставление с БД
    -> превью -> подтверждение (запись в on_grades_ok)."""
    from core.models import Group
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, message.from_user.id)
        st = svc.active_statement(s, teacher)
        if st is None:
            await message.answer("Сначала создайте ведомость — «📄 Новая ведомость».")
            return
        group = s.get(Group, st.group_id)
        roster_names = [x.full_name for x in svc.roster(s, group)]
        el_names = [e.name for e in svc.scheme_elements(s, st)]
    try:
        parsed = parse_grades(text, roster_names, el_names)
    except Exception as e:
        await message.answer(f"Не смог распознать оценки: {e}")
        return

    resolved, labels = [], []
    from core.models import Group as G
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, message.from_user.id)
        st = svc.active_statement(s, teacher)
        group = s.get(G, st.group_id)
        students = svc.roster(s, group)
        elements = svc.scheme_elements(s, st)
        for p in parsed:
            stu = svc.match_student(students, p.student)
            el = svc.match_element(elements, p.element)
            if stu and el and 0 <= p.value <= 10:
                resolved.append((stu.id, el.id, p.value))
                labels.append(f"• {stu.full_name.split()[0]} — {el.name} = {p.value:g}")

    if not resolved:
        await message.answer(
            f"Распознанный текст: «{text}»\nНо оценки не понял. "
            f"Пример: «за блиц Иванов 8, Петрова 3»."
        )
        return

    await state.update_data(pending=resolved, source=source)
    await state.set_state(Flow.confirming_grades)
    head = f"🗣 Распознал: «{text}»\n\n" if source == "voice" else ""
    await message.answer(head + "Записать эти оценки?\n" + "\n".join(labels),
                         reply_markup=grades_confirm_kb())


@dp.message(F.voice)
async def on_voice(message: Message, state: FSMContext) -> None:
    if not settings.ai_enabled:
        await message.answer("Голосовой ввод недоступен: не настроен YC_API_KEY.")
        return
    await message.answer("⏳ Распознаю голос…")
    file = await message.bot.get_file(message.voice.file_id)
    buf = await message.bot.download_file(file.file_path)
    try:
        text = transcribe(buf.read())
    except Exception as e:
        await message.answer(f"Не смог распознать голос: {e}")
        return
    if not text.strip():
        await message.answer("Пустая транскрипция — повторите чётче.")
        return
    await _handle_grades(message, state, text, "voice")


@dp.callback_query(Flow.confirming_grades, F.data == "grades_ok")
async def on_grades_ok(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    pending, source = data.get("pending", []), data.get("source", "text")
    await state.clear()
    from core.models import ControlElement, Student
    recorded = 0
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, cb.from_user.id)
        st = svc.active_statement(s, teacher)
        for sid, eid, val in pending:
            svc.add_grade_entry(s, st, s.get(Student, sid), s.get(ControlElement, eid),
                                val, source, teacher)
            recorded += 1
    await cb.message.answer(f"✅ Записано оценок: {recorded}.", reply_markup=main_menu())
    await cb.answer()


@dp.callback_query(F.data == "grades_cancel")
async def on_grades_cancel(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.answer("Отменил. /start — меню.")
    await cb.answer()


@dp.message(F.text)
async def on_text_fallback(message: Message, state: FSMContext) -> None:
    txt = message.text.strip()
    # эвристика: есть цифра + есть API-ключ -> пробуем разобрать как оценки
    if settings.ai_enabled and any(ch.isdigit() for ch in txt):
        await message.answer("⏳ Распознаю…")
        await _handle_grades(message, state, txt, "text")
        return
    await message.answer(
        "Наберите /start для меню. Оценки можно вводить кнопками, "
        "текстом («за блиц Иванов 8, Петрова 3») или голосом."
    )


async def _run() -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN в .env.")
    init_db()
    bot = Bot(token=settings.telegram_bot_token)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_run())
