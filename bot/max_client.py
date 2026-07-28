"""Тонкий синхронный клиент MAX Bot API (botapi.max.ru) — только то, что нужно
Ведомость AI. Форматы проверены на живом боте «Ведомость ИИ»:

  * авторизация — заголовок ``Authorization: <token>`` (query ?access_token= устарел, 401);
  * приём — GET /updates (long-poll), типы bot_started / message_created / message_callback;
  * отправка — POST /messages?chat_id=<> {text, format, attachments};
  * инлайн-кнопки — attachment {type:"inline_keyboard", payload:{buttons:[[{type:"callback",...}]]}};
  * ответ на нажатие — POST /answers?callback_id=<> {notification}.

Бизнес-логика не здесь: клиент только транспорт (аналог Bot из aiogram).
"""
from __future__ import annotations

import httpx

BASE = "https://botapi.max.ru"


def button(text: str, payload: str) -> dict:
    """Callback-кнопка MAX (аналог InlineKeyboardButton с callback_data)."""
    return {"type": "callback", "text": text, "payload": payload}


class MaxClient:
    def __init__(self, token: str, base: str = BASE) -> None:
        self._headers = {"Authorization": token}
        self._base = base
        self._http = httpx.Client(timeout=60)

    def get_me(self) -> dict:
        r = self._http.get(f"{self._base}/me", headers=self._headers, timeout=20)
        r.raise_for_status()
        return r.json()

    def get_updates(self, marker: int | None = None, timeout: int = 30,
                    limit: int = 50) -> tuple[list[dict], int | None]:
        params: dict = {"timeout": timeout, "limit": limit}
        if marker is not None:
            params["marker"] = marker
        r = self._http.get(f"{self._base}/updates", headers=self._headers,
                           params=params, timeout=timeout + 15)
        r.raise_for_status()
        data = r.json()
        return data.get("updates", []), data.get("marker")

    def send_message(self, chat_id: int, text: str,
                     buttons: list[list[dict]] | None = None, fmt: str | None = "html") -> dict:
        """buttons — список рядов кнопок. fmt=None — плейн-текст (без разметки)."""
        body: dict = {"text": text}
        if fmt:
            body["format"] = fmt
        if buttons:
            body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": buttons}}]
        r = self._http.post(f"{self._base}/messages", headers=self._headers,
                            params={"chat_id": chat_id}, json=body, timeout=30)
        r.raise_for_status()
        return r.json()

    def answer_callback(self, callback_id: str, notification: str) -> dict:
        """Ответ на нажатие (аналог cb.answer). ВАЖНО: MAX требует непустой
        notification — с пустым телом /answers отдаёт 400. Поэтому вызываем
        только когда есть что показать (см. ack() в max_main)."""
        r = self._http.post(f"{self._base}/answers", headers=self._headers,
                            params={"callback_id": callback_id},
                            json={"notification": notification}, timeout=20)
        r.raise_for_status()
        return r.json()

    def upload_file(self, path: str, filename: str,
                    content_type: str = "application/octet-stream") -> str:
        """Двухшаговая загрузка файла в MAX -> token для вложения."""
        r1 = self._http.post(f"{self._base}/uploads", headers=self._headers,
                            params={"type": "file"}, timeout=30)
        r1.raise_for_status()
        url = r1.json()["url"]
        with open(path, "rb") as f:
            r2 = self._http.post(url, files={"data": (filename, f, content_type)}, timeout=120)
        r2.raise_for_status()
        return r2.json()["token"]

    def send_document(self, chat_id: int, path: str, filename: str, caption: str = "") -> dict:
        """Загружает и отправляет файл. Вложение может «дозревать» — ретраим на 400."""
        import time
        token = self.upload_file(path, filename)
        body = {"text": caption, "attachments": [{"type": "file", "payload": {"token": token}}]}
        r = None
        for _ in range(6):
            r = self._http.post(f"{self._base}/messages", headers=self._headers,
                               params={"chat_id": chat_id}, json=body, timeout=40)
            if r.status_code == 200:
                return r.json()
            time.sleep(2)
        r.raise_for_status()
        return {}

    def download(self, url: str) -> bytes:
        r = self._http.get(url, timeout=60)
        r.raise_for_status()
        return r.content
