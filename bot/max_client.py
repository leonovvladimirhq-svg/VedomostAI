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

    def answer_callback(self, callback_id: str, notification: str | None = None) -> dict:
        """Аналог cb.answer(): короткое всплывающее уведомление на нажатие кнопки."""
        body: dict = {}
        if notification:
            body["notification"] = notification
        r = self._http.post(f"{self._base}/answers", headers=self._headers,
                            params={"callback_id": callback_id}, json=body, timeout=20)
        r.raise_for_status()
        return r.json()
