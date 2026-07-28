"""Транспорт Ведомость AI для MAX (МАКС) — отдельная точка входа.

Переиспускает всю доменную логику из core/services (согласие 152-ФЗ, ПУД, оценки,
расчёт, экспорт). Прод Telegram (bot/main.py) не трогает — другой токен, процесс.

Форматы MAX проверены на живом боте (см. bot/max_client). Long-poll синхронный.

Запуск:  ./.venv/Scripts/python -m bot.max_main   (при заполненном MAX_BOT_TOKEN в .env)
"""
from __future__ import annotations

import json
import logging
import tempfile
import time
from pathlib import Path

from config import settings
from core.db import SessionLocal, init_db
from core.export.excel import build_ledger_from_statement
from core.parsing.pud_ingest import extract_text, find_formula, find_title
from core.parsing.pud_parser import parse_formula
from core.services import consent_service as consent
from core.services import feedback_service as fb
from core.services import statement_service as svc
from core.services.grading_service import GRADE_MIN, element_max
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
FEEDBACK_ASK_PUD = "Правильно ли я распознал формулу оценивания из ПУД? Если что-то не так — скажите:"
FEEDBACK_ASK_COMMENT = (
    "Спасибо! Хотите добавить комментарий — что понравилось или что улучшить? "
    "Напишите сообщением или нажмите «Пропустить комментарий»."
)
FEEDBACK_THANKS = "🙏 Спасибо за обратную связь! Мы её учтём."
NEW_PUD_ASK = (
    "Пришлите ваш <b>ПУД файлом</b> (PDF, DOCX или HTML из конструктора dp.hse.ru) — "
    "я извлеку элементы контроля и веса из формулы оценивания.\n\n"
    "Можно также вставить формулу текстом (например: «Тест * 0.2 + Проект * 0.5 + Активность * 0.3»)."
)
PUD_NOT_FOUND = (
    "Не нашёл формулу оценивания. Пришлите раздел «Система оценивания» текстом "
    "или другой файл (PDF/DOCX/HTML)."
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


def pud_confirm_kb() -> list[list[dict]]:
    return [[button("✅ Подтвердить и создать", "pud_ok")], [button("✖️ Отмена", "pud_cancel")]]


def elements_kb(elements) -> list[list[dict]]:
    return [[button(f"{e.name} ({e.weight:g})", f"el:{e.id}")] for e in elements]


def students_kb(students) -> list[list[dict]]:
    rows, row = [], []
    for i, s in enumerate(students, 1):
        row.append(button(f"{i}. {s.full_name.split()[0]}", f"stu:{s.id}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    return rows


# --- Общие помощники ---
def _welcome(c: MaxClient, uid: int, chat: int, name: str) -> None:
    with SessionLocal() as s:
        group = seed_group(s)
        count = len(svc.roster(s, group))
    c.send_message(chat, WELCOME.format(name=name or "преподаватель", group=group.name, count=count),
                   buttons=main_menu())


def _present_detected(c: MaxClient, uid: int, chat: int, text: str) -> None:
    """Показывает распознанную из ПУД структуру и просит подтверждение."""
    formula = find_formula(text)
    if not formula:
        c.send_message(chat, PUD_NOT_FOUND)
        return
    try:
        scheme = parse_formula(formula)
    except Exception:
        c.send_message(chat, "Формула найдена, но не разобралась. Пришлите её текстом.")
        return
    title = find_title(text)
    STATE[uid] = {"flow": "confirming_pud", "formula": formula, "title": title}
    names = "\n".join(f"• {e.name} — вес {e.weight:g}" for e in scheme.elements)
    total_w = scheme.check_weights()
    warn = "" if total_w == 1.0 else f"\n⚠️ Сумма весов = {total_w:g} (обычно 1.0)"
    c.send_message(chat, f"📋 Распознал ПУД: <b>{title}</b>\nЭлементы контроля:\n{names}{warn}\n\nВсё верно?",
                   buttons=pud_confirm_kb())


# --- Хендлеры ---
def handle_start(c: MaxClient, uid: int, chat: int, name: str) -> None:
    STATE.pop(uid, None)
    with SessionLocal() as s:
        svc.get_or_create_teacher(s, uid, name or "")
        need = consent.needs_consent(s, uid)
    if need:  # 152-ФЗ: без согласия не начинаем
        c.send_message(chat, CONSENT_INTRO, buttons=consent_intro_kb())
    else:
        _welcome(c, uid, chat, name)


def _record_value(c: MaxClient, uid: int, chat: int, name: str, text: str) -> None:
    """Ввод оценки после выбора элемента+студента (детектор «вне диапазона»)."""
    st_data = STATE.get(uid, {})
    element_id, student_id = st_data.get("element_id"), st_data.get("student_id")
    raw = text.strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        c.send_message(chat, "Нужно число. Введите оценку по шкале 0–10 ещё раз:")
        return
    from core.models import ControlElement, Student
    with SessionLocal() as s:
        teacher = svc.get_or_create_teacher(s, uid, name or "")
        st = svc.active_statement(s, teacher)
        student = s.get(Student, student_id)
        element = s.get(ControlElement, element_id)
        emax = element_max(element)
        if not GRADE_MIN <= value <= emax:
            c.send_message(chat, f"К сожалению, за «{element.name}» можно поставить максимум {emax:g} "
                                 f"(шкала {GRADE_MIN:g}–{emax:g}). Введите оценку ещё раз:")
            return
        svc.add_grade_entry(s, st, student, element, value, "buttons", teacher, raw_input=text)
        res = svc.student_total(s, st, student)
        surname = student.full_name.split()[0]
        elname = element.name
        total = res.total
        els = svc.scheme_elements(s, st)
    STATE[uid] = {}  # сбрасываем выбор, остаёмся в вводе через кнопки
    c.send_message(chat, f"✅ Записано: {surname} — {elname} = {value:g}. Текущий итог: <b>{total:g}</b>.\n"
                         f"Продолжить ввод — выберите элемент:", buttons=elements_kb(els))


def _sniff_ext(data: bytes, fname: str) -> str:
    """Определяем формат по содержимому (MAX может не сохранить расширение)."""
    if data[:4] == b"%PDF":
        return ".pdf"
    if data[:2] == b"PK":  # zip -> docx/xlsx
        return ".docx"
    low = data[:2048].lower()
    if b"<html" in low or b"<!doctype html" in low or b"<head" in low:
        return ".html"
    ext = Path(fname).suffix.lower()
    return ext if ext in (".html", ".htm", ".pdf", ".docx", ".txt", ".md") else ".txt"


def _detect_from_file(c: MaxClient, uid: int, chat: int, att: dict) -> bool:
    """Скачивает присланный файл ПУД и распознаёт формулу. True — обработано."""
    payload = att.get("payload", {}) or {}
    url = payload.get("url") or payload.get("file_url")
    fname = (att.get("filename") or att.get("name")
             or payload.get("filename") or payload.get("name") or "pud")
    log.info("ПУД-файл: fname=%r att_keys=%s payload_keys=%s has_url=%s",
             fname, list(att.keys()), list(payload.keys()), bool(url))
    if not url:
        log.info("ПУД-вложение без url: %s", json.dumps(att, ensure_ascii=False)[:500])
        c.send_message(chat, "Не смог получить ссылку на файл. Пришлите формулу текстом.")
        return True
    try:
        data = c.download(url)
        ext = _sniff_ext(data, fname)
        tmp = Path(tempfile.gettempdir()) / f"maxpud_{uid}{ext}"
        tmp.write_bytes(data)
        text = extract_text(str(tmp))
        tmp.unlink(missing_ok=True)
        log.info("ПУД-файл прочитан: %s байт, ext=%s -> %s символов, формула=%s",
                 len(data), ext, len(text), bool(find_formula(text)))
    except Exception as e:
        log.exception("Ошибка чтения ПУД-файла")
        c.send_message(chat, f"Не смог прочитать файл ({e}). Пришлите формулу текстом.")
        return True
    _present_detected(c, uid, chat, text)
    return True


def handle_message(c: MaxClient, uid: int, chat: int, name: str, text: str,
                   attachments: list[dict]) -> None:
    text = (text or "").strip()
    st = STATE.get(uid, {})
    flow = st.get("flow")
    file_atts = [a for a in (attachments or []) if a.get("type") == "file"]

    # 1) комментарий обратной связи
    if flow == "feedback_comment":
        STATE.pop(uid, None)
        if text and not text.startswith("/") and st.get("fb_id"):
            with SessionLocal() as s:
                fb.set_comment(s, st["fb_id"], text)
        c.send_message(chat, FEEDBACK_THANKS)
        return
    # 2) ввод числовой оценки
    if flow == "entering_value" and text and not text.startswith("/"):
        _record_value(c, uid, chat, name, text)
        return
    # 3) ожидание ПУД (файл или формула текстом)
    if flow == "waiting_pud":
        if file_atts and _detect_from_file(c, uid, chat, file_atts[0]):
            return
        if text and not text.startswith("/"):
            _present_detected(c, uid, chat, text)
            return
    # 4) команды
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
        c.send_message(chat, "Наберите /start для меню, затем «📄 Новая ведомость» — пришлёте ПУД.")


def handle_callback(c: MaxClient, uid: int, chat: int, name: str, payload: str, cbid: str) -> None:
    def ack(note: str | None = None) -> None:
        # MAX требует непустой notification; без note просто не отвечаем (спиннер снимется
        # приходом следующего сообщения) — иначе /answers отдаёт 400.
        if not note:
            return
        try:
            c.answer_callback(cbid, note)
        except Exception:
            log.exception("answer_callback failed")

    # --- Согласие ---
    if payload == "consent:read":
        c.send_message(chat, CONSENT_SUMMARY.format(**consent.summary()), buttons=consent_kb())
    elif payload == "consent:fulltext":
        doc = consent.CONSENT_DOC_PATH.read_text(encoding="utf-8")
        c.send_message(chat, f"📄 Полный текст согласия\nВерсия: {consent.CONSENT_VERSION}\n"
                             f"SHA-256: {consent.doc_sha256()}", fmt=None)
        c.send_message(chat, doc, fmt=None)
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
    # --- Право на забвение ---
    elif payload == "forget:yes":
        with SessionLocal() as s:
            consent.record_consent(s, uid, consent.STATUS_REVOKED)
            consent.forget_me(s, uid)
        STATE.pop(uid, None)
        c.send_message(chat, FORGET_ME_DONE)
    elif payload == "forget:no":
        c.send_message(chat, FORGET_ME_CANCELLED)
    # --- Обратная связь ---
    elif payload == "menu:feedback":
        c.send_message(chat, FEEDBACK_ASK, buttons=feedback_rating_kb("menu"))
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
    # --- Новая ведомость (ПУД) ---
    elif payload == "new":
        STATE[uid] = {"flow": "waiting_pud"}
        c.send_message(chat, NEW_PUD_ASK)
    elif payload == "pud_ok":
        data = STATE.get(uid, {})
        formula, title = data.get("formula"), data.get("title", "Ведомость по ПУД")
        STATE.pop(uid, None)
        if not formula:
            c.send_message(chat, "Сессия ПУД истекла. Начните заново — «📄 Новая ведомость».")
        else:
            with SessionLocal() as s:
                teacher = svc.get_or_create_teacher(s, uid, name or "")
                group = seed_group(s)
                stmt = svc.create_statement_with_scheme(s, teacher, group, parse_formula(formula),
                                                        course_name=title, module="")
                sid = stmt.id
            c.send_message(chat, f"✅ Ведомость #{sid} создана: <b>{title}</b>, статус «Заполняется».\n"
                                 f"Теперь вводите оценки — «✍️ Ввести оценки».", buttons=main_menu())
            c.send_message(chat, FEEDBACK_ASK_PUD, buttons=feedback_rating_kb("pud", sid))
    elif payload == "pud_cancel":
        STATE.pop(uid, None)
        c.send_message(chat, "Отменил. /start — меню.")
    # --- Ввод оценок ---
    elif payload == "enter":
        with SessionLocal() as s:
            teacher = svc.get_or_create_teacher(s, uid, name or "")
            st = svc.active_statement(s, teacher)
            els = svc.scheme_elements(s, st) if st else []
        if not els:
            c.send_message(chat, "Сначала создайте ведомость — «📄 Новая ведомость».")
        else:
            c.send_message(chat, "Выберите элемент контроля:", buttons=elements_kb(els))
    elif payload.startswith("el:"):
        STATE[uid] = {"flow": "picking_student", "element_id": int(payload.split(":")[1])}
        from core.models import Group
        with SessionLocal() as s:
            teacher = svc.get_or_create_teacher(s, uid, name or "")
            st = svc.active_statement(s, teacher)
            students = svc.roster(s, s.get(Group, st.group_id))
        c.send_message(chat, "Выберите студента:", buttons=students_kb(students))
    elif payload.startswith("stu:"):
        st_data = STATE.get(uid, {})
        st_data.update(flow="entering_value", student_id=int(payload.split(":")[1]))
        STATE[uid] = st_data
        c.send_message(chat, "Введите оценку (число 0–10):")
    # --- Показать / Выгрузить ---
    elif payload == "show":
        from core.models import Group
        with SessionLocal() as s:
            teacher = svc.get_or_create_teacher(s, uid, name or "")
            st = svc.active_statement(s, teacher)
            if st is None:
                c.send_message(chat, "Нет активной ведомости. Создайте — «📄 Новая ведомость».")
                return
            students = svc.roster(s, s.get(Group, st.group_id))
            lines = [f"{i}. {' '.join(stu.full_name.split()[:2])} — итог: {svc.student_total(s, st, stu).total:g}"
                     for i, stu in enumerate(students, 1)]
            course = st.course_name
        c.send_message(chat, f"<b>{course}</b>\n" + "\n".join(lines), buttons=main_menu())
    elif payload == "export":
        with SessionLocal() as s:
            teacher = svc.get_or_create_teacher(s, uid, name or "")
            st = svc.active_statement(s, teacher)
            if st is None:
                c.send_message(chat, "Нет активной ведомости для выгрузки.")
                return
            wb = build_ledger_from_statement(s, st)
            course, sid = st.course_name, st.id
        tmp = Path(tempfile.gettempdir()) / f"vedomost_max_{sid}.xlsx"
        wb.save(tmp)
        try:
            c.send_document(chat, str(tmp), f"Ведомость — {course}.xlsx",
                            caption=f"📊 Ведомость «{course}» (.xlsx)")
        except Exception as e:
            log.exception("export failed")
            c.send_message(chat, f"Не удалось выгрузить файл: {e}")
        finally:
            tmp.unlink(missing_ok=True)
    else:
        pass  # неизвестный callback — молча игнорируем


def handle_update(c: MaxClient, u: dict) -> None:
    t = u.get("update_type")
    if t == "bot_started":
        handle_start(c, u["user"]["user_id"], u["chat_id"], u["user"].get("name", ""))
    elif t == "message_created":
        m = u["message"]
        sender = m.get("sender", {})
        body = m.get("body", {})
        handle_message(c, sender.get("user_id"), m["recipient"]["chat_id"],
                       sender.get("name", ""), body.get("text", "") or "",
                       body.get("attachments", []) or [])
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
