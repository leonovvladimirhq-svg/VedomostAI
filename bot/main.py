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
from datetime import datetime, timezone, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from config import settings
from core.db import SessionLocal, init_db
from core.export.excel import build_ledger_from_statement
from core.export.yadisk import YaDiskError, upload_xlsx
from core.parsing.pud_ingest import extract_text, find_formula, find_title
from core.parsing.pud_parser import parse_formula
from core.parsing.text_parser import parse_grades
from core.parsing.voice import transcribe
from core.services import consent_service as consent
from core.services import feedback_service as fb
from core.services import reminder_service as rem
from core.services import statement_service as svc
from core.services.grading_service import GRADE_MIN, element_max
from seed.test_group import seed as seed_group

logging.basicConfig(level=logging.INFO)
dp = Dispatcher(storage=MemoryStorage())


class Flow(StatesGroup):
    waiting_pud = State()          # ожидание файла ПУД (или вставленной формулы)
    confirming = State()           # подтверждение распознанной структуры ПУД
    entering_value = State()       # ввод числовой оценки после выбора элемента+студента
    confirming_grades = State()    # подтверждение оценок, распознанных из текста/голоса
    feedback_comment = State()     # ожидание необязательного комментария обратной связи


# --- Тексты (на «Вы», принадлежность к ШК НИУ ВШЭ + ценность одним-двумя предложениями) ---
WELCOME = (
    "👋 Здравствуйте, {name}! Это <b>Ведомость AI</b> — помощник преподавателя "
    "<b>Школы коммуникаций НИУ ВШЭ</b>.\n\n"
    "Я избавляю от ручного счёта: пришлите свой <b>ПУД файлом</b> — извлеку формулу "
    "оценивания, соберу ведомость, посчитаю итоги и выгружу в Excel и Яндекс.Таблицы.\n\n"
    "Тестовая группа: <b>{group}</b> ({count} студентов)."
)

# Согласие (152-ФЗ) — двухэкранный флоу, как в TutorAI.
CONSENT_INTRO = (
    "👋 Здравствуйте! Это <b>Ведомость AI</b> — помощник преподавателя "
    "<b>Школы коммуникаций НИУ ВШЭ</b> для ведения ведомостей оценок.\n\n"
    "Прежде чем начать, мне нужно Ваше согласие на обработку персональных данных "
    "(этого требует 152-ФЗ). Это займёт минуту."
)
CONSENT_SUMMARY = (
    "📋 <b>Кратко о согласии</b> (полный текст — по кнопке ниже):\n\n"
    "• <b>Оператор:</b> {operator}\n"
    "• <b>Что обрабатываем:</b> {processing}\n"
    "• <b>Где храним:</b> {storage}\n"
    "• <b>Срок:</b> {retention}\n"
    "• <b>Версия документа:</b> <code>{version}</code>\n\n"
    "Нажимая «Согласен», Вы подтверждаете, что прочитали и приняли документ."
)
CONSENT_FULLTEXT_CAPTION = (
    "📄 Полный текст согласия на обработку персональных данных.\n"
    "<code>{version}</code>\nSHA-256: <code>{sha}</code>"
)
CONSENT_DECLINED = (
    "Понимаю. Без согласия на обработку данных работа с ботом невозможна. "
    "Если передумаете — отправьте /start.\n\n"
    "Удалить уже сохранённые данные можно командой /forget_me."
)

# Права субъекта ПДн.
MY_DATA = (
    "🔎 <b>Ваши данные в Ведомость AI</b>\n\n"
    "• Telegram ID: <code>{telegram_id}</code>\n"
    "• Согласие: {consent}\n"
    "• Ведомостей: <b>{statements}</b>\n\n"
    "Удалить всё и отозвать согласие — /forget_me."
)
FORGET_ME_CONFIRM = (
    "⚠️ Вы уверены? Будут <b>безвозвратно удалены</b> Ваш профиль, ведомости и "
    "внесённые оценки. Это действие нельзя отменить."
)
FORGET_ME_DONE = (
    "🗑 Готово. Ваши данные удаляются в течение 24 часов; согласие отозвано.\n\n"
    "Чтобы снова начать — отправьте /start."
)
FORGET_ME_CANCELLED = "Отменено. Ваши данные остаются на месте."

# Обратная связь.
FEEDBACK_ASK = "Как Вам Ведомость AI? Ваша оценка помогает нам стать лучше:"
FEEDBACK_ASK_PUD = (
    "Правильно ли я распознал формулу оценивания из Вашего ПУД? "
    "Если что-то не так — обязательно скажите:"
)
FEEDBACK_ASK_COMMENT = (
    "Спасибо! Хотите добавить комментарий — что понравилось или что улучшить? "
    "Напишите сообщением или нажмите «Пропустить комментарий»."
)
FEEDBACK_THANKS = "🙏 Спасибо за обратную связь! Мы её учтём."


# --- Клавиатуры ---
def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Новая ведомость", callback_data="new")],
        [InlineKeyboardButton(text="✍️ Ввести оценки", callback_data="enter")],
        [InlineKeyboardButton(text="👀 Показать ведомость", callback_data="show")],
        [InlineKeyboardButton(text="📊 Выгрузить Excel", callback_data="export")],
        [InlineKeyboardButton(text="☁️ В Яндекс.Диск (таблица)", callback_data="export_yadisk")],
        [InlineKeyboardButton(text="💬 Оставить обратную связь", callback_data="menu:feedback")],
    ])


# --- Клавиатуры согласия / обратной связи (паттерн TutorAI) ---
def consent_intro_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Прочитать согласие", callback_data="consent:read")],
    ])


def consent_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Полный текст (.md)", callback_data="consent:fulltext")],
        [InlineKeyboardButton(text="✅ Согласен", callback_data="consent:accept")],
        [InlineKeyboardButton(text="❌ Не согласен", callback_data="consent:decline")],
    ])


def forget_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Да, удалить мои данные", callback_data="forget:yes")],
        [InlineKeyboardButton(text="Отмена", callback_data="forget:no")],
    ])


def feedback_rating_kb(context: str = "menu", ref_id: int | None = None) -> InlineKeyboardMarkup:
    """👍/👎. callback: fb:<up|down>:<context>:<ref_id или пусто>."""
    ref = str(ref_id) if ref_id is not None else ""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👍 Нравится", callback_data=f"fb:up:{context}:{ref}"),
        InlineKeyboardButton(text="👎 Не нравится", callback_data=f"fb:down:{context}:{ref}"),
    ]])


def feedback_skip_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить комментарий", callback_data="fb:skip")],
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
        svc.get_or_create_teacher(s, message.from_user.id, message.from_user.full_name or "")
        need = consent.needs_consent(s, message.from_user.id)
    if need:  # 152-ФЗ: без согласия работу с ботом не начинаем
        await message.answer(CONSENT_INTRO, reply_markup=consent_intro_kb(), parse_mode="HTML")
        return
    await _welcome(message, message.from_user)


async def _welcome(target: Message, user) -> None:
    """Приветствие + тестовая группа + главное меню (после согласия). user — субъект
    (для колбэка target.from_user = бот, поэтому пользователя передаём явно)."""
    name = user.full_name or "преподаватель"
    with SessionLocal() as s:
        group = seed_group(s)
        count = len(svc.roster(s, group))
    await target.answer(
        WELCOME.format(name=name, group=group.name, count=count),
        reply_markup=main_menu(), parse_mode="HTML",
    )


# --- Согласие на обработку ПДн (152-ФЗ) ---
@dp.callback_query(F.data == "consent:read")
async def on_consent_read(cb: CallbackQuery) -> None:
    await cb.message.edit_text(
        CONSENT_SUMMARY.format(**consent.summary()), reply_markup=consent_kb(), parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data == "consent:fulltext")
async def on_consent_fulltext(cb: CallbackQuery) -> None:
    await cb.message.answer_document(
        FSInputFile(consent.CONSENT_DOC_PATH),
        caption=CONSENT_FULLTEXT_CAPTION.format(version=consent.CONSENT_VERSION, sha=consent.doc_sha256()),
        parse_mode="HTML",
    )
    await cb.answer()


@dp.callback_query(F.data == "consent:decline")
async def on_consent_decline(cb: CallbackQuery) -> None:
    with SessionLocal() as s:
        consent.record_consent(s, cb.from_user.id, consent.STATUS_DECLINED)
    await cb.message.edit_text(CONSENT_DECLINED)
    await cb.answer()


@dp.callback_query(F.data == "consent:accept")
async def on_consent_accept(cb: CallbackQuery, state: FSMContext) -> None:
    with SessionLocal() as s:
        consent.record_consent(s, cb.from_user.id, consent.STATUS_ACCEPTED)
    await state.clear()
    await cb.message.edit_text("✅ Согласие получено. Спасибо!")
    await cb.answer()
    await _welcome(cb.message, cb.from_user)


# --- Права субъекта ПДн: /my_data, /forget_me ---
@dp.message(Command("my_data"))
async def on_my_data(message: Message) -> None:
    tg_id = message.from_user.id
    with SessionLocal() as s:
        status, version = consent.consent_status(s, tg_id)
        teacher = svc.get_or_create_teacher(s, tg_id)
        n_st = svc.teacher_statement_count(s, teacher)
    consent_str = (f"дано (версия <code>{version}</code>)"
                   if status == consent.STATUS_ACCEPTED else "не дано")
    await message.answer(
        MY_DATA.format(telegram_id=tg_id, consent=consent_str, statements=n_st), parse_mode="HTML"
    )


@dp.message(Command("forget_me"))
async def on_forget_me(message: Message) -> None:
    await message.answer(FORGET_ME_CONFIRM, reply_markup=forget_confirm_kb(), parse_mode="HTML")


@dp.callback_query(F.data == "forget:yes")
async def on_forget_yes(cb: CallbackQuery, state: FSMContext) -> None:
    with SessionLocal() as s:
        consent.record_consent(s, cb.from_user.id, consent.STATUS_REVOKED)
        consent.forget_me(s, cb.from_user.id)
    await state.clear()
    await cb.message.edit_text(FORGET_ME_DONE)
    await cb.answer()


@dp.callback_query(F.data == "forget:no")
async def on_forget_no(cb: CallbackQuery) -> None:
    await cb.message.edit_text(FORGET_ME_CANCELLED)
    await cb.answer()


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
    # контекстная обратная связь: как распознана формула из ПУД
    await cb.message.answer(FEEDBACK_ASK_PUD, reply_markup=feedback_rating_kb("pud", sid))
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
        await message.answer("Нужно число. Введите оценку по шкале 0–10 ещё раз:")
        return
    data = await state.get_data()
    element_id, student_id = data.get("element_id"), data.get("student_id")
    from core.models import ControlElement, Student
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, message.from_user.id)
        st = svc.active_statement(s, teacher)
        student = s.get(Student, student_id)
        element = s.get(ControlElement, element_id)
        emax = element_max(element)
        # #5: детектор «вне диапазона» — жёсткая валидация ввода (остаёмся в состоянии ввода).
        if not GRADE_MIN <= value <= emax:
            await message.answer(
                f"К сожалению, за «{element.name}» можно поставить максимум {emax:g} "
                f"(шкала {GRADE_MIN:g}–{emax:g}). Больше поставить нельзя — введите оценку ещё раз:"
            )
            return
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


@dp.callback_query(F.data == "export_yadisk")
async def on_export_yadisk(cb: CallbackQuery) -> None:
    if not settings.yadisk_enabled:
        await cb.message.answer(
            "☁️ Интеграция с Яндекс.Диском готова, но нужен токен доступа.\n"
            "Получить OAuth-токен для Диска: https://yandex.ru/dev/disk/poligon/ — "
            "и добавить его как YANDEX_DISK_TOKEN. После этого ведомость будет "
            "выгружаться в Яндекс-таблицу одной кнопкой."
        )
        await cb.answer()
        return
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, cb.from_user.id)
        st = svc.active_statement(s, teacher)
        if st is None:
            await cb.message.answer("Нет активной ведомости для выгрузки.")
            await cb.answer()
            return
        wb = build_ledger_from_statement(s, st)
        course, sid = st.course_name, st.id
    tmp = Path(tempfile.gettempdir()) / f"vedomost_{sid}.xlsx"
    wb.save(tmp)
    await cb.message.answer("⏳ Выгружаю в Яндекс.Диск…")
    try:
        url = upload_xlsx(str(tmp), f"Ведомость — {course}.xlsx")
        await cb.message.answer(f"☁️ Готово. Открыть как таблицу в Яндекс Документах:\n{url}",
                                reply_markup=main_menu())
    except YaDiskError as e:
        await cb.message.answer(f"Ошибка выгрузки на Яндекс.Диск: {e}")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    await cb.answer()


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

    resolved, labels, rejected = [], [], []
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
            if not (stu and el):
                continue
            emax = element_max(el)
            if GRADE_MIN <= p.value <= emax:  # #5: только оценки в шкале записываем
                resolved.append((stu.id, el.id, p.value))
                labels.append(f"• {stu.full_name.split()[0]} — {el.name} = {p.value:g}")
            else:
                rejected.append(f"• {stu.full_name.split()[0]} — {el.name}: {p.value:g} вне шкалы {GRADE_MIN:g}–{emax:g}")

    if not resolved:
        note = ("\n\n⚠️ Вне шкалы, не записал:\n" + "\n".join(rejected)) if rejected else ""
        await message.answer(
            f"Распознанный текст: «{text}»\nНо оценки не понял. "
            f"Пример: «за блиц Иванов 8, Петрова 3»." + note
        )
        return

    await state.update_data(pending=resolved, source=source)
    await state.set_state(Flow.confirming_grades)
    head = f"🗣 Распознал: «{text}»\n\n" if source == "voice" else ""
    tail = ("\n\n⚠️ Вне шкалы (не запишу):\n" + "\n".join(rejected)) if rejected else ""
    await message.answer(head + "Записать эти оценки?\n" + "\n".join(labels) + tail,
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


# --- Обратная связь: 👍/👎 + необязательный комментарий (паттерн TutorAI) ---
@dp.callback_query(F.data == "menu:feedback")
async def on_feedback_menu(cb: CallbackQuery) -> None:
    await cb.message.answer(FEEDBACK_ASK, reply_markup=feedback_rating_kb("menu"))
    await cb.answer()


@dp.callback_query(F.data.startswith("fb:") & ~F.data.startswith("fb:skip"))
async def on_feedback_rating(cb: CallbackQuery, state: FSMContext) -> None:
    parts = cb.data.split(":")  # fb:<up|down>:<context>:<ref_id или пусто>
    rating = parts[1] if len(parts) > 1 else "up"
    context = parts[2] if len(parts) > 2 else "menu"
    ref_id = int(parts[3]) if len(parts) > 3 and parts[3] else None
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, cb.from_user.id)
        entry = fb.add_feedback(s, telegram_id=cb.from_user.id, rating=rating,
                                context=context, teacher_id=teacher.id, ref_id=ref_id)
        fb_id = entry.id
    await state.set_state(Flow.feedback_comment)
    await state.update_data(fb_id=fb_id)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    await cb.message.answer(FEEDBACK_ASK_COMMENT, reply_markup=feedback_skip_kb())


@dp.callback_query(F.data == "fb:skip")
async def on_feedback_skip(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.answer()
    await cb.message.answer(FEEDBACK_THANKS)


@dp.message(Flow.feedback_comment, F.text)
async def on_feedback_comment(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    fb_id = data.get("fb_id")
    await state.clear()
    if fb_id is not None and message.text.strip():
        with SessionLocal() as s:
            fb.set_comment(s, fb_id, message.text.strip())
    await message.answer(FEEDBACK_THANKS)


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


# --- Напоминания (контур 4): фоновый цикл + ручной триггер ---
_last_reminded: dict[int, datetime] = {}  # statement_id -> когда напомнили (дедуп в процессе)


async def _send_reminders(bot: Bot, *, force: bool = False) -> int:
    """Проверяет неактивные ведомости и шлёт напоминания преподавателям.
    force=True игнорирует внутрипроцессный дедуп. Возвращает число отправленных."""
    now = datetime.now(timezone.utc)
    with SessionLocal() as s:
        stale = rem.find_stale(s, now, settings.reminder_inactivity_hours,
                               settings.reminder_skip_month_set)
    dedupe = timedelta(hours=max(1, settings.reminder_inactivity_hours))
    sent = 0
    for item in stale:
        prev = _last_reminded.get(item.statement_id)
        if not force and prev and (now - prev) < dedupe:
            continue
        try:
            await bot.send_message(
                item.telegram_id,
                f"🔔 Напоминание: по ведомости «{item.course_name}» вы не вносили оценки "
                f"уже {rem.humanize_idle(item.idle)}. Загляните и продолжите заполнение — "
                f"«✍️ Ввести оценки».",
            )
            _last_reminded[item.statement_id] = now
            sent += 1
        except Exception:
            logging.exception("Не удалось отправить напоминание teacher_tg=%s", item.telegram_id)
    return sent


async def _reminders_loop(bot: Bot) -> None:
    """Периодическая проверка неактивных ведомостей (интервал — из .env)."""
    await asyncio.sleep(20)  # дать боту подняться
    interval = max(5, settings.reminder_check_interval_min) * 60
    while True:
        try:
            n = await _send_reminders(bot)
            if n:
                logging.info("Отправлено напоминаний: %s", n)
        except Exception:
            logging.exception("Сбой цикла напоминаний")
        await asyncio.sleep(interval)


@dp.message(Command("remind_now"))
async def on_remind_now(message: Message) -> None:
    """Ручная проверка напоминаний (тест доставки). Игнорирует дедуп."""
    sent = await _send_reminders(message.bot, force=True)
    hrs = settings.reminder_inactivity_hours
    tail = "" if sent else (
        "\nНет ведомостей без активности дольше порога. Чтобы проверить доставку "
        "прямо сейчас — временно поставьте REMINDER_INACTIVITY_HOURS=0 и повторите /remind_now."
    )
    await message.answer(
        f"Проверка выполнена. Порог неактивности: {hrs} ч. Отправлено напоминаний: {sent}." + tail
    )


async def _run() -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN в .env.")
    init_db()
    bot = Bot(token=settings.telegram_bot_token)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(_reminders_loop(bot))  # контур 4: напоминания в фоне
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_run())
