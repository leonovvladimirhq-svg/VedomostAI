"""Транспорт Ведомость AI для MAX (МАКС) — отдельная точка входа.

Переиспускает всю доменную логику из core/services (согласие 152-ФЗ, обратная связь
и т.д.). Прод Telegram (bot/main.py) не трогает — другой токен, другой процесс.

Форматы MAX проверены на живом боте (см. bot/max_client). Long-poll синхронный —
так проще, а сервисы у нас синхронные.

СЛОЙ 1 (этот файл): согласие → приветствие/меню → обратная связь → /my_data /forget_me.
СЛОЙ 2 (следующий): приём ПУД файлом, ввод оценок, выгрузка .xlsx (нужен upload MAX).

Запуск:  ./.venv/Scripts/python -m bot.max_main   (при заполненном MAX_BOT_TOKEN в .env)
"""
from __future__ import annotations

import logging
import time

from config import settings
from core.db import SessionLocal, init_db
from core.services import consent_service as consent
from core.services import feedback_service as fb
from core.services import statement_service as svc
from seed.test_group import seed as seed_group
from bot.max_client import MaxClient, button

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("max")

# In-memory FSM (аналог aiogram FSMContext): user_id -> {"flow": ..., ...}
STATE: dict[int, dict] = {}

# --- Тексты (на «Вы», принадлежность к ШК НИУ ВШЭ) ---
WELCOME = (
    "👋 Здравствуйте, {name}! Это <b>Ведомость AI</b> — помощник преподавателя "
    "<b>Школы коммуникаций НИУ ВШЭ</b>.\n\n"
    "Я избавляю от ручного счёта: пришлите свой ПУД — извлеку формулу оценивания, "
    "соберу ведомость, посчитаю итоги и выгружу в Excel и Яндекс.Таблицы.\n\n"
    "Тестовая группа: <b>{group}</b> ({count} студентов)."
)
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
    "• <b>Версия документа:</b> {version}\n\n"
    "Нажимая «Согласен», Вы подтверждаете, что прочитали и приняли документ."
)
CONSENT_DECLINED = (
    "Понимаю. Без согласия на обработку данных работа с ботом невозможна. "
    "Если передумаете — отправьте /start. Удалить сохранённые данные — /forget_me."
)
MY_DATA = (
    "🔎 <b>Ваши данные в Ведомость AI</b>\n\n"
    "• MAX ID: {uid}\n• Согласие: {consent}\n• Ведомостей: <b>{statements}</b>\n\n"
    "Удалить всё и отозвать согласие — /forget_me."
)
FORGET_ME_CONFIRM = (
    "⚠️ Вы уверены? Будут <b>безвозвратно удалены</b> Ваш профиль, ведомости и оценки. "
    "Это действие нельзя отменить."
)
FORGET_ME_DONE = "🗑 Готово. Данные удаляются в течение 24 часов; согласие отозвано. /start — начать заново."
FORGET_ME_CANCELLED = "Отменено. Ваши данные остаются на месте."
FEEDBACK_ASK = "Как Вам Ведомость AI? Ваша оценка помогает нам стать лучше:"
FEEDBACK_ASK_COMMENT = (
    "Спасибо! Хотите добавить комментарий — что понравилось или что улучшить? "
    "Напишите сообщением или нажмите «Пропустить комментарий»."
)
FEEDBACK_THANKS = "🙏 Спасибо за обратную связь! Мы её учтём."
SECTION_SOON = (
    "🔧 Этот раздел (приём ПУД, ввод оценок, выгрузка .xlsx) добавляю следующим слоем порта. "
    "Согласие, меню и обратная связь на MAX уже работают."
)


# --- Клавиатуры (список рядов из button()) ---
def main_menu() -> list[list[dict]]:
    return [
        [button("📄 Новая ведомость", "new")],
        [button("✍️ Ввести оценки", "enter")],
        [button("👀 Показать ведомость", "show")],
        [button("📊 Выгрузить Excel", "export")],
        [button("💬 Оставить обратную связь", "menu:feedback")],
    ]


def consent_intro_kb() -> list[list[dict]]:
    return [[button("📄 Прочитать согласие", "consent:read")]]


def consent_kb() -> list[list[dict]]:
    return [
        [button("📄 Полный текст", "consent:fulltext")],
        [button("✅ Согласен", "consent:accept")],
        [button("❌ Не согласен", "consent:decline")],
    ]


def forget_kb() -> list[list[dict]]:
    return [[button("🗑 Да, удалить мои данные", "forget:yes")], [button("Отмена", "forget:no")]]


def feedback_rating_kb(context: str = "menu", ref_id: int | None = None) -> list[list[dict]]:
    ref = str(ref_id) if ref_id is not None else ""
    return [[button("👍 Нравится", f"fb:up:{context}:{ref}"),
             button("👎 Не нравится", f"fb:down:{context}:{ref}")]]


def feedback_skip_kb() -> list[list[dict]]:
    return [[button("Пропустить комментарий", "fb:skip")]]


# --- Хендлеры ---
def _welcome(c: MaxClient, uid: int, chat: int, name: str) -> None:
    with SessionLocal() as s:
        group = seed_group(s)
        count = len(svc.roster(s, group))
    c.send_message(chat, WELCOME.format(name=name or "преподаватель", group=group.name, count=count),
                   buttons=main_menu())


def handle_start(c: MaxClient, uid: int, chat: int, name: str) -> None:
    STATE.pop(uid, None)
    with SessionLocal() as s:
        svc.get_or_create_teacher(s, uid, name or "")
        need = consent.needs_consent(s, uid)
    if need:  # 152-ФЗ: без согласия не начинаем
        c.send_message(chat, CONSENT_INTRO, buttons=consent_intro_kb())
    else:
        _welcome(c, uid, chat, name)


def handle_message(c: MaxClient, uid: int, chat: int, name: str, text: str) -> None:
    text = (text or "").strip()
    st = STATE.get(uid, {})
    # комментарий обратной связи
    if st.get("flow") == "feedback_comment":
        STATE.pop(uid, None)
        if text and not text.startswith("/") and st.get("fb_id"):
            with SessionLocal() as s:
                fb.set_comment(s, st["fb_id"], text)
        c.send_message(chat, FEEDBACK_THANKS)
        return
    if text == "/start":
        handle_start(c, uid, chat, name)
    elif text == "/my_data":
        with SessionLocal() as s:
            status, version = consent.consent_status(s, uid)
            teacher = svc.get_or_create_teacher(s, uid, name or "")
            n_st = svc.teacher_statement_count(s, teacher)
        cons = f"дано (версия {version})" if status == consent.STATUS_ACCEPTED else "не дано"
        c.send_message(chat, MY_DATA.format(uid=uid, consent=cons, statements=n_st))
    elif text == "/forget_me":
        c.send_message(chat, FORGET_ME_CONFIRM, buttons=forget_kb())
    else:
        c.send_message(chat, "Наберите /start для меню. (Приём ПУД и оценок добавляю следующим слоем.)")


def handle_callback(c: MaxClient, uid: int, chat: int, name: str, payload: str, cbid: str) -> None:
    def ack(note: str | None = None) -> None:
        try:
            c.answer_callback(cbid, note)
        except Exception:
            log.exception("answer_callback failed")

    if payload == "consent:read":
        c.send_message(chat, CONSENT_SUMMARY.format(**consent.summary()), buttons=consent_kb())
        ack()
    elif payload == "consent:fulltext":
        doc = consent.CONSENT_DOC_PATH.read_text(encoding="utf-8")
        c.send_message(chat, f"📄 Полный текст согласия\nВерсия: {consent.CONSENT_VERSION}\n"
                             f"SHA-256: {consent.doc_sha256()}", fmt=None)
        c.send_message(chat, doc, fmt=None)  # плейн-текст: в .md есть <...>, html сломается
        ack("Отправил полный текст")
    elif payload == "consent:accept":
        with SessionLocal() as s:
            consent.record_consent(s, uid, consent.STATUS_ACCEPTED)
        ack("Спасибо!")
        c.send_message(chat, "✅ Согласие получено. Спасибо!")
        _welcome(c, uid, chat, name)
    elif payload == "consent:decline":
        with SessionLocal() as s:
            consent.record_consent(s, uid, consent.STATUS_DECLINED)
        c.send_message(chat, CONSENT_DECLINED)
        ack()
    elif payload == "forget:yes":
        with SessionLocal() as s:
            consent.record_consent(s, uid, consent.STATUS_REVOKED)
            consent.forget_me(s, uid)
        STATE.pop(uid, None)
        c.send_message(chat, FORGET_ME_DONE)
        ack()
    elif payload == "forget:no":
        c.send_message(chat, FORGET_ME_CANCELLED)
        ack()
    elif payload == "menu:feedback":
        c.send_message(chat, FEEDBACK_ASK, buttons=feedback_rating_kb("menu"))
        ack()
    elif payload.startswith("fb:") and payload != "fb:skip":
        parts = payload.split(":")  # fb:<up|down>:<context>:<ref>
        rating = parts[1] if len(parts) > 1 else "up"
        context = parts[2] if len(parts) > 2 else "menu"
        ref_id = int(parts[3]) if len(parts) > 3 and parts[3] else None
        with SessionLocal() as s:
            teacher = svc.get_or_create_teacher(s, uid, name or "")
            entry = fb.add_feedback(s, telegram_id=uid, rating=rating, context=context,
                                    teacher_id=teacher.id, ref_id=ref_id)
            fb_id = entry.id
        STATE[uid] = {"flow": "feedback_comment", "fb_id": fb_id}
        ack("Записал оценку")
        c.send_message(chat, FEEDBACK_ASK_COMMENT, buttons=feedback_skip_kb())
    elif payload == "fb:skip":
        STATE.pop(uid, None)
        c.send_message(chat, FEEDBACK_THANKS)
        ack()
    elif payload in ("new", "enter", "show", "export"):
        c.send_message(chat, SECTION_SOON, buttons=main_menu())
        ack()
    else:
        ack()  # неизвестный callback (напр. test:ping) — просто подтверждаем


def handle_update(c: MaxClient, u: dict) -> None:
    t = u.get("update_type")
    if t == "bot_started":
        handle_start(c, u["user"]["user_id"], u["chat_id"], u["user"].get("name", ""))
    elif t == "message_created":
        m = u["message"]
        sender = m.get("sender", {})
        handle_message(c, sender.get("user_id"), m["recipient"]["chat_id"],
                       sender.get("name", ""), m.get("body", {}).get("text", "") or "")
    elif t == "message_callback":
        cb = u["callback"]
        handle_callback(c, cb["user"]["user_id"], u["message"]["recipient"]["chat_id"],
                        cb["user"].get("name", ""), cb.get("payload", ""), cb.get("callback_id"))


def run() -> None:
    if not settings.max_bot_token:
        raise SystemExit("Нет MAX_BOT_TOKEN в .env.")
    init_db()
    c = MaxClient(settings.max_bot_token)
    me = c.get_me()
    log.info("MAX бот запущен: %s (@%s) id=%s", me.get("first_name"), me.get("username"), me.get("user_id"))
    _, marker = c.get_updates(timeout=1)  # слить backlog, начать с текущего маркера
    log.info("Начинаю с marker=%s", marker)
    while True:
        try:
            updates, marker = c.get_updates(marker=marker, timeout=30)
        except Exception:
            log.exception("Ошибка get_updates"); time.sleep(3); continue
        for u in updates:
            try:
                handle_update(c, u)
            except Exception:
                log.exception("Ошибка обработки апдейта %s", u.get("update_type"))


if __name__ == "__main__":
    run()
