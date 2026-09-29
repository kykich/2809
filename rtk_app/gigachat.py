"""Клиент для работы с GigaChat (облачный Сбер) через chat completions.
Требует файл gigakey.txt (base64 "client_id:client_secret").
OAuth2 client_credentials; access-токен кешируется.
"""
import json
import time
import uuid
import urllib.parse
import urllib.request

from . import config

__all__ = ["chat"]

_TOKEN = None
_TOKEN_EXP = 0.0


def _read_basic_auth():
    """Читает базовую авторизацию (base64 client_id:client_secret).

    Комментарии (строки, начинающиеся с '#') и пустые строки игнорируются —
    ключом считается первая непустая рабочая строка. Так в файле-заглушке
    можно держать инструкцию # и рядом отдельной строкой настоящий ключ.
    """
    with open(config.GC_KEY_FILE, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            return line
    return ""


def _post(url, headers, body):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=config.REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_access_token():
    global _TOKEN, _TOKEN_EXP
    now = time.time()
    if _TOKEN and now < _TOKEN_EXP - 30:
        return _TOKEN
    basic = _read_basic_auth()
    headers = {
        "Authorization": "Basic " + basic,
        "RqUID": str(uuid.uuid4()),
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    body = urllib.parse.urlencode({"scope": config.GC_SCOPE}).encode("utf-8")
    data = _post(config.GC_TOKEN_URL, headers, body)
    _TOKEN = data.get("access_token")
    if not _TOKEN:
        raise RuntimeError("OAuth GigaChat: нет access_token")
    _TOKEN_EXP = now + config.GC_TOKEN_TTL
    return _TOKEN




def chat(messages, model=config.GC_MODEL, temperature=None, max_tokens=None):
    """Отправляет сообщения в GigaChat.

    Возвращает dict: content, prompt_tokens, completion_tokens.
    temperature — float или None; если задан — передаётся в запрос.
    max_tokens — int или None; если задан — ограничивает число токенов ответа.
    """
    token = _get_access_token()
    headers = {
        "Authorization": "Bearer " + token,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {"model": model, "messages": messages, "stream": False}
    if temperature is not None:
        payload["temperature"] = float(temperature)
    if max_tokens is not None:
        payload["max_tokens"] = int(max_tokens)
    data = _post(
        config.GC_API_BASE,
        headers,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage") or {}
    return {
        "content": content,
        "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
        "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
    }