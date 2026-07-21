"""Клиент Qwen (Yandex AI Studio, OpenAI-совместимый endpoint).

LLM применяется ТОЛЬКО на границах (раздел 5 плана): разбор ввода, извлечение из ПУД.
Любое число дальше проходит через детерминированный движок.
"""
from __future__ import annotations

import httpx

from config import settings

_ENDPOINT = "https://llm.api.cloud.yandex.net/v1/chat/completions"


class LLMError(Exception):
    pass


def chat(messages: list[dict], *, max_tokens: int = 2000, temperature: float = 0.0) -> str:
    """Синхронный вызов Qwen. Возвращает message.content (у reasoning-модели рассуждения
    отдельно в reasoning_content и нам не нужны)."""
    if not settings.ai_api_key:
        raise LLMError("Не задан YC_API_KEY (Api-Key сервисного аккаунта).")
    body = {
        "model": settings.ai_model_uri,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    r = httpx.post(_ENDPOINT, headers={"Authorization": f"Api-Key {settings.ai_api_key}"},
                   json=body, timeout=120)
    if r.status_code != 200:
        raise LLMError(f"LLM {r.status_code}: {r.text[:200]}")
    return r.json()["choices"][0]["message"].get("content") or ""
