"""MCP-сервер «Доска» — вывод данных «Завхоза» ТАБЛИЦЕЙ (см. docs/task5.md).

Роль в композиции task5:
  * по запросу пользователя в чате ВЫВОДИТ данные «Завхоза»;
  * представляет их ТАБЛИЦЕЙ, где сопоставлены СОБЫТИЕ и ВРЕМЯ (в формате,
    выбранном «Замполитом» по времени начала события).

Данные «Доска» читает напрямую из файла-хранилища «Завхоза»
(session/zavhoz.json) — серверы не вызывают друг друга напрямую.

Инструменты:
  * show_board — таблица «событие ↔ время» по сохранённому снимку (по умолчанию);
  * show_board_html — то же, но в виде HTML-таблицы (для ответа в чате);
  * ping — проверка живости.

Запуск (как stdio-подпроцесс MCP):
    python doska_server.py
"""

import html
import json
import os
import sys

from mcp.server.mcpserver import MCPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

mcp = MCPServer("doska")

# Файл-хранилище «Завхоза» (та же папка session/, не коммитится).
STORE_FILE = os.path.join(BASE_DIR, "session", "zavhoz.json")


def _store_path():
    """Путь к файлу-хранилищу «Завхоза» (можно переопределить окружением)."""
    return os.environ.get("ZAVHOZ_FILE") or STORE_FILE


def _load():
    """Читает снимок «Завхоза» из файла."""
    path = _store_path()
    if not os.path.isfile(path):
        return {"updated": "", "count": 0, "pairs": []}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"updated": "", "count": 0, "pairs": []}
    if not isinstance(data, dict):
        return {"updated": "", "count": 0, "pairs": []}
    if not isinstance(data.get("pairs"), list):
        data["pairs"] = []
    return data


def _cell(value, default="-"):
    """Безопасное представление значения для ячейки."""
    if value is None or value == "":
        return default
    return str(value)


# --------------------------------------------------------------------------
# Инструменты MCP
# --------------------------------------------------------------------------
@mcp.tool()
def ping() -> str:
    """Проверка живости сервера «Доска»."""
    return "Доска на связи."


@mcp.tool()
def show_board() -> str:
    """Таблица «СОБЫТИЕ ↔ ВРЕМЯ» по сохранённым данным «Завхоза».

    Читает снимок session/zavhoz.json и печатает текстовую таблицу с
    колонками: событие, начало, период, формат, время.
    """
    data = _load()
    pairs = data.get("pairs") or []
    if not pairs:
        return ("Доска пуста: данных «Завхоза» ещё нет. Сначала запустите "
                "композицию (кнопка «Запустить композицию»).")
    # Ширины колонок — по фактическому содержимому (с разумными минимумами).
    rows = []
    for p in pairs:
        rows.append((
            _cell(p.get("event")),
            _cell(p.get("start")),
            _cell(p.get("period")),
            _cell(p.get("mode")),
            _cell(p.get("time")) if p.get("time") not in (None, "")
            else _cell(p.get("rate")),
        ))
    headers = ("Событие", "Начало", "Период", "Формат", "Время")
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))

    def line(cells):
        return "| " + " | ".join(
            c.ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = ["Доска: события и время (обновлено: %s)"
           % _cell(data.get("updated")), sep, line(headers), sep]
    for r in rows:
        out.append(line(r))
    out.append(sep)
    out.append("Всего событий: %d" % len(rows))
    return "\n".join(out)


@mcp.tool()
def show_board_html() -> str:
    """Таблица «СОБЫТИЕ ↔ ВРЕМЯ» в виде HTML (для ответа в чате).

    Возвращает готовый HTML-фрагмент <table> с экранированием значений.
    """
    data = _load()
    pairs = data.get("pairs") or []
    if not pairs:
        return ('<div class="board-empty">Доска пуста: сначала запустите '
                'композицию.</div>')
    head = ("<tr><th>Событие</th><th>Начало</th><th>Период</th>"
            "<th>Формат</th><th>Время</th></tr>")
    body = []
    for p in pairs:
        value = p.get("time") if p.get("time") not in (None, "") else p.get("rate")
        body.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (html.escape(_cell(p.get("event"))),
               html.escape(_cell(p.get("start"))),
               html.escape(_cell(p.get("period"))),
               html.escape(_cell(p.get("mode"))),
               html.escape(_cell(value))))
    return ('<table class="board-table"><thead>%s</thead><tbody>%s</tbody>'
            '</table>' % (head, "".join(body)))


if __name__ == "__main__":
    # Транспорт по умолчанию — stdio (как подпроцесс MCP-клиента проекта).
    mcp.run()
