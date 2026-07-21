"""Авто-выгрузка ведомости на Яндекс.Диск (контур 3 / цель по ТЗ: Яндекс.Таблицы).

Загруженный .xlsx открывается на Яндекс.Диске как редактируемая таблица в Яндекс
Документах — это и есть «выгрузка в Яндекс-таблицу». Нужен OAuth-токен Яндекс-аккаунта
(YANDEX_DISK_TOKEN): https://yandex.ru/dev/disk/poligon/ (получить токен для Диска).

Источник истины остаётся PostgreSQL/движок; Диск — представление/выгрузка.
"""
from __future__ import annotations

import httpx

from config import settings

_BASE = "https://cloud-api.yandex.net/v1/disk/resources"
_FOLDER = "disk:/Ведомость AI"


class YaDiskError(Exception):
    pass


def _headers() -> dict:
    return {"Authorization": f"OAuth {settings.yandex_disk_token}"}


def _ensure_folder() -> None:
    # 201 — создана, 409 — уже есть; оба ок
    httpx.put(_BASE, params={"path": _FOLDER}, headers=_headers(), timeout=30)


def upload_xlsx(local_path: str, remote_name: str) -> str:
    """Загружает файл в папку «Ведомость AI» на Диске, публикует и возвращает ссылку."""
    if not settings.yadisk_enabled:
        raise YaDiskError("Не задан YANDEX_DISK_TOKEN.")
    _ensure_folder()
    path = f"{_FOLDER}/{remote_name}"

    r = httpx.get(f"{_BASE}/upload", params={"path": path, "overwrite": "true"},
                  headers=_headers(), timeout=30)
    if r.status_code != 200:
        raise YaDiskError(f"upload href {r.status_code}: {r.text[:150]}")
    href = r.json()["href"]

    with open(local_path, "rb") as f:
        pr = httpx.put(href, content=f.read(), timeout=120)
    if pr.status_code not in (201, 202):
        raise YaDiskError(f"put {pr.status_code}: {pr.text[:150]}")

    httpx.put(f"{_BASE}/publish", params={"path": path}, headers=_headers(), timeout=30)
    gr = httpx.get(_BASE, params={"path": path, "fields": "public_url"},
                   headers=_headers(), timeout=30)
    return gr.json().get("public_url", "")
