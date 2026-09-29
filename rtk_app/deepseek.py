"""Клиент для работы с DeepSeek API (chat completions).
Поддерживает дополнительные настройки генерации: temperature и max_tokens.
"""
import json
import urllib.request

from . import config

__all__ = ["chat"]


def chat(api_key, messages, model=None, temperature=None, max_tokens=None):
    """Отправляет сообщения, возвращает dict: content, prompt_tokens, completion_tokens.

    temperature — float или None; None означает «не передавать» (системное
    значение модели по умолчанию).
    max_tokens — int или None; None означает «не передавать» (лимит модели
    по умолчанию). Если задан — ограничивает число новых токенов ответа.
    """
    if not model:
        model = config.DS_MODELS[0]
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
    }
    payload = {"model": model, "messages": messages, "stream": False}
    if temperature is not None:
        payload["temperature"] = float(temperature)
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)
    req = urllib.request.Request(
        config.DS_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage") or {}
    return {
        "content": content,
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
    }
