"""Статусная модель ведомости — конечный автомат (раздел 10 плана).

Переходы — каркас всей логики прав и сроков. В итерации 1 реально используются
только DRAFT -> FILLING -> CLOSED, но полный автомат заложен сразу, чтобы контуры
3–4 (подпись, экспорт, переоткрытие) наращивались без переделки.
"""
from __future__ import annotations

import enum


class StatementStatus(str, enum.Enum):
    DRAFT = "draft"            # Черновик — структура ещё не подтверждена
    FILLING = "filling"        # Заполняется — идёт ввод оценок (контур 2)
    CLOSED = "closed"          # Закрыта — итог рассчитан, ждёт подписи
    PUBLISHED = "published"    # Опубликована — ЭП поставлена (контур 3, позже)
    EXPORTED = "exported"      # Экспортирована — official record в СЭВ (позже)
    REOPENED = "reopened"      # Переоткрыта — правки после экспорта (позже)


# Разрешённые переходы. Любой переход журналируется (AuditLog — волна позже).
ALLOWED_TRANSITIONS: dict[StatementStatus, set[StatementStatus]] = {
    StatementStatus.DRAFT: {StatementStatus.FILLING},
    StatementStatus.FILLING: {StatementStatus.CLOSED},
    StatementStatus.CLOSED: {StatementStatus.PUBLISHED, StatementStatus.FILLING},
    StatementStatus.PUBLISHED: {StatementStatus.EXPORTED},
    StatementStatus.EXPORTED: {StatementStatus.REOPENED},
    StatementStatus.REOPENED: {StatementStatus.CLOSED},
}


class InvalidTransition(Exception):
    pass


def can_transition(src: StatementStatus, dst: StatementStatus) -> bool:
    return dst in ALLOWED_TRANSITIONS.get(src, set())


def assert_transition(src: StatementStatus, dst: StatementStatus) -> None:
    if not can_transition(src, dst):
        raise InvalidTransition(f"Недопустимый переход статуса: {src.value} -> {dst.value}")
