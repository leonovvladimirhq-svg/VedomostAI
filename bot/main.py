"""Telegram-бот Ведомость AI — точка входа (aiogram 3).

Бот — ТОНКИЙ слой: разбирает апдейты Telegram и вызывает core.services.
Никакой доменной логики здесь нет (это условие превращения в субагента).

Запуск:  python -m bot.main   (из папки app, при заполненном .env)
Без TELEGRAM_BOT_TOKEN бот не стартует — код при этом корректен и импортируется.
"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message,
)

from config import settings
from core.db import SessionLocal, init_db
from core.services import statement_service as svc
from core.statuses import StatementStatus
from seed.test_group import seed as seed_group

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Новая ведомость", callback_data="new_statement")],
        [InlineKeyboardButton(text="👀 Показать ведомость", callback_data="show")],
        [InlineKeyboardButton(text="⌨️ Ввести оценки (кнопки)", callback_data="enter_buttons")],
    ])


@dp.message(CommandStart())
async def on_start(message: Message) -> None:
    tg_id = message.from_user.id
    if not settings.teacher_allowed(tg_id):
        await message.answer("Доступ ограничен списком преподавателей.")
        return
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, tg_id, message.from_user.full_name or "")
        group = seed_group(s)  # тестовая группа из 20 студентов
        count = len(svc.roster(s, group))
    await message.answer(
        f"Здравствуйте, {teacher.full_name or 'преподаватель'}!\n"
        f"Это <b>Ведомость AI</b> — прототип.\n"
        f"Тестовая группа: <b>{group.name}</b> ({count} студентов).\n\n"
        f"Оценки можно будет вводить кнопками, текстом («Иванов 8, Петров 3») "
        f"и голосом. Выберите действие:",
        reply_markup=main_menu(), parse_mode="HTML",
    )


@dp.callback_query(F.data == "new_statement")
async def on_new(cb: CallbackQuery) -> None:
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, cb.from_user.id)
        group = seed_group(s)
        st = svc.create_statement(s, teacher, group, course_name="Демо-курс", module="Модуль 1")
        # В прототипе структуру ещё не строим из ПУД -> сразу в «Заполняется».
        svc.set_status(s, st, StatementStatus.FILLING)
        sid = st.id
    await cb.message.answer(
        f"Создана ведомость #{sid} (Демо-курс, Модуль 1), статус: «Заполняется».\n"
        f"⚠️ Элементы контроля и веса подтянутся из ПУД — жду пример файла ПУД, "
        f"после этого включу расчёт."
    )
    await cb.answer()


@dp.callback_query(F.data == "show")
async def on_show(cb: CallbackQuery) -> None:
    with SessionLocal() as s:
        group = seed_group(s)
        students = svc.roster(s, group)
    lines = "\n".join(f"{i+1}. {st.full_name}" for i, st in enumerate(students))
    await cb.message.answer(f"<b>{group.name}</b>\n{lines}", parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "enter_buttons")
async def on_enter_buttons(cb: CallbackQuery) -> None:
    await cb.message.answer(
        "Ввод кнопками: выбор студента → элемент контроля → оценка.\n"
        "Экран включится, как только будет структура ведомости из ПУД."
    )
    await cb.answer()


@dp.message(F.voice)
async def on_voice(message: Message) -> None:
    await message.answer(
        "🎙 Голос принят. Транскрипция через Yandex SpeechKit подключается — "
        "нужна роль ai.speechRecognition.user на сервисном аккаунте (см. список материалов)."
    )


@dp.message(F.text)
async def on_text(message: Message) -> None:
    await message.answer(
        "✍️ Текст принят. Разбор «Иванов 8, Петров 3» через Yandex AI Studio подключается — "
        "нужны структура ведомости и подтверждение модели (см. список материалов).\n"
        "Наберите /start для меню."
    )


async def _run() -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("Нет TELEGRAM_BOT_TOKEN в .env — заполни .env и перезапусти.")
    init_db()
    bot = Bot(token=settings.telegram_bot_token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(_run())
