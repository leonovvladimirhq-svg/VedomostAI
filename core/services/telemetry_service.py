"""Телеметрия в общий дашборд мониторинга проектов Школы коммуникаций.

Дашборд (http://89.169.146.175:8080) собирает события со всех сервисов команды
в одну таблицу; для каждого проекта — свой ингест-токен. Сюда уходит по одному
событию на каждое действие преподавателя в боте.

Принципы:
- «выстрелил и забыл» в фоновом потоке, таймаут 5 с: мониторинг не имеет права
  замедлить или уронить бота;
- если DASHBOARD_URL / DASHBOARD_TOKEN не заданы — тихий no-op;
- ПДн не передаём: ни текст сообщений преподавателя, ни оценки, ни ФИО студентов.
  В дашборд уходит только КАТЕГОРИЯ действия («загрузил ПУД», «ввод оценок»),
  MAX-id пользователя и результат ok/error. Оценки студентов и содержимое
  ведомостей остаются в этой базе.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

import httpx

from config import settings

log = logging.getLogger(__name__)

_TIMEOUT = 5.0


def _post(payload: dict) -> None:
    try:
        httpx.post(
            f"{settings.dashboard_url.rstrip('/')}/api/ingest",
            json=payload,
            headers={"Authorization": f"Bearer {settings.dashboard_token}"},
            timeout=_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001 — телеметрия никогда не роняет бота
        log.warning("Дашборд недоступен: %s", e)


def track(
    user_id: int | None,
    action: str,
    result: str = "",
    *,
    status: str = "ok",
    latency_ms: int | None = None,
    user_name: str = "",
) -> None:
    """Отправить событие «действие пользователя» в дашборд.

    action — категория без ПДн (см. label_for_update), result — краткий итог.
    """
    if not settings.dashboard_url or not settings.dashboard_token:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    payload = {
        "occurred_at": ts,
        "user_ref": str(user_id) if user_id is not None else None,
        "user_name": user_name or None,
        "request_text": action,
        "response_text": result or None,
        "status": "error" if status == "error" else "ok",
        "latency_ms": latency_ms,
        "model": settings.ai_model_uri if settings.ai_enabled else None,
        # уникальность с точностью до секунды на пользователя и действие
        "dedup_key": f"{user_id}:{ts}:{action}"[:200],
    }
    threading.Thread(target=_post, args=(payload,), daemon=True).start()


# Подписи callback-кнопок бота — чтобы в дашборде было «Экспорт в Excel»,
# а не «export». Неизвестный payload уходит как есть (без ПДн он не бывает).
_CALLBACK_LABELS = {
    "enter": "Кнопка: ввод оценок",
    "new": "Кнопка: новая ведомость",
    "show": "Кнопка: показать ведомость",
    "export": "Кнопка: экспорт в Excel",
    "pud_ok": "Кнопка: ПУД распознан верно",
    "pud_cancel": "Кнопка: ПУД распознан неверно",
    "grades_ok": "Кнопка: подтвердить оценки",
    "grades_cancel": "Кнопка: отменить оценки",
    "consent:read": "Согласие ПДн: читать",
    "consent:fulltext": "Согласие ПДн: полный текст",
    "consent:accept": "Согласие ПДн: принято",
    "consent:decline": "Согласие ПДн: отклонено",
    "menu:feedback": "Кнопка: обратная связь",
    "fb:skip": "Обратная связь: без комментария",
    "forget:yes": "Удаление данных: подтверждено",
    "forget:no": "Удаление данных: отменено",
}


def label_for_update(u: dict) -> tuple[int | None, str, str]:
    """(user_id, имя, категория действия) для сырого апдейта MAX — без ПДн."""
    t = u.get("update_type")
    if t == "bot_started":
        user = u.get("user") or {}
        return user.get("user_id"), user.get("name", ""), "Открыл бота"
    if t == "message_created":
        m = u.get("message") or {}
        sender = m.get("sender") or {}
        body = m.get("body") or {}
        text = (body.get("text") or "").strip()
        atts = body.get("attachments") or []
        if not m:
            return None, "", "Сообщение без тела (голосовое?)"
        if atts:
            kinds = {a.get("type") for a in atts}
            if "audio" in kinds:
                label = "Голосовое сообщение"
            elif "file" in kinds:
                label = "Загрузил файл (ПУД)"
            else:
                label = "Сообщение с вложением: " + ", ".join(sorted(k for k in kinds if k))
        elif text.startswith("/"):
            label = "Команда " + text.split()[0].split("@")[0]
        else:
            label = "Текстовое сообщение"
        return sender.get("user_id"), sender.get("name", ""), label
    if t == "message_callback":
        cb = u.get("callback") or {}
        user = cb.get("user") or {}
        payload = cb.get("payload") or ""
        label = _CALLBACK_LABELS.get(payload)
        if label is None:
            if payload.startswith("el:"):
                label = "Кнопка: выбор элемента контроля"
            elif payload.startswith("stu:"):
                label = "Кнопка: выбор студента"
            elif payload.startswith("fb:"):
                label = "Обратная связь: оценка бота"
            else:
                label = f"Кнопка: {payload}"
        return user.get("user_id"), user.get("name", ""), label
    return None, "", f"Событие {t}"
