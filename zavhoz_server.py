"""MCP-сервер «Завхоз» — ХРАНИЛИЩЕ результата композиции (см. docs/task5.md).

Роль в композиции task5:
  * получает данные от «Замполита» (пары «событие + время в выбранном формате»);
  * СОХРАНЯЕТ их в JSON-файл (перезаписывая снимок);
  * умеет отдать сохранённый снимок обратно (для «Доски» и проверки).

Файл-хранилище: session/zavhoz.json (папка session/ не коммитится).
Структура файла:
{
  "updated": "YYYY-MM-DD HH:MM:SS",  # когда снимок сохранён
  "count": 3,                        # сколько пар
  "pairs": [ {pair}, … ],            # пары от Замполита
  "source": "composition"            # метка источника (кто записал)
}

Инструменты:
  * save_pairs  — сохранить пары (JSON от Замполита) в файл;
  * load_pairs  — прочитать сохранённые пары (текст/JSON);
  * clear       — очистить хранилище;
  * info        — краткая сводка о хранилище (сколько, когда сохранено).

Запуск (как stdio-подпроцесс MCP):
    python zavhoz_server.py
"""

import json
import os
import sys
import time

from mcp.server.mcpserver import MCPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from rtk_app.jobs_store import now_str

mcp = MCPServer("zavhoz")

# Файл-хранилище снимка (в папке session/, которая не коммитится).
STORE_FILE = os.path.join(BASE_DIR, "session", "zavhoz.json")


def _store_path():
    """Путь к файлу-хранилищу (можно переопределить окружением)."""
    return os.environ.get("ZAVHOZ_FILE") or STORE_FILE


def _save(data):
    """Атомарно пишет снимок в JSON-файл."""
    path = _store_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _load():
    """Читает снимок из файла (или пустую структуру)."""
    path = _store_path()
    if not os.path.isfile(path):
        return {"updated": "", "count": 0, "pairs": [], "source": ""}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"updated": "", "count": 0, "pairs": [], "source": ""}
    if not isinstance(data, dict):
        return {"updated": "", "count": 0, "pairs": [], "source": ""}
    return data


def _as_json(value):
    """Если пришёл уже dict/list — сериализуем; строку возвращаем как есть."""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value or "")


# --------------------------------------------------------------------------
# Инструменты MCP
# --------------------------------------------------------------------------
@mcp.tool()
def save_pairs(pairs_json: str, source: str = "composition") -> str:
    """СОХРАНИТЬ пары от Замполита в JSON-файл (перезаписывает снимок).

    pairs_json — данные Замполита. Принимается ЛИБО объект вида
        {"pairs": [ … ], …}, ЛИБО сам массив пар [ … ].
    source     — метка источника (по умолчанию "composition").

    Возвращает строку-отчёт: сколько пар сохранено и путь к файлу.
    """
    try:
        data = json.loads(_as_json(pairs_json))
    except Exception as exc:
        return "Ошибка разбора pairs_json: %s" % exc
    if isinstance(data, dict):
        pairs = data.get("pairs")
        if not isinstance(pairs, list):
            pairs = data.get("result") if isinstance(data.get("result"), list) else []
    elif isinstance(data, list):
        pairs = data
    else:
        return "pairs_json должен быть объектом с полем 'pairs' или массивом."
    snapshot = {
        "updated": now_str(),
        "count": len(pairs),
        "pairs": pairs,
        "source": str(source or ""),
    }
    try:
        _save(snapshot)
    except Exception as exc:
        return "Не удалось сохранить снимок: %s" % exc
    return ("Сохранено пар: %d -> %s (обновлено: %s)"
            % (len(pairs), _store_path(), snapshot["updated"]))


@mcp.tool()
def load_pairs() -> str:
    """Прочитать СОХРАНЁННЫЕ пары (JSON-строкой)."""
    return json.dumps(_load(), ensure_ascii=False)


@mcp.tool()
def info() -> str:
    """Краткая сводка о хранилище: сколько пар и когда сохранено."""
    data = _load()
    if not data.get("count"):
        return "Хранилище пусто (снимок ещё не сохранён)."
    return ("Завхоз: пар=%d, сохранено=%s, источник=%s, файл=%s"
            % (data.get("count", 0), data.get("updated", ""),
               data.get("source", ""), _store_path()))


@mcp.tool()
def clear() -> str:
    """Очистить хранилище (пустой снимок)."""
    try:
        _save({"updated": now_str(), "count": 0, "pairs": [], "source": "clear"})
    except Exception as exc:
        return "Не удалось очистить хранилище: %s" % exc
    return "Хранилище Завхоза очищено."


if __name__ == "__main__":
    # Транспорт по умолчанию — stdio (как подпроцесс MCP-клиента проекта).
    mcp.run()
