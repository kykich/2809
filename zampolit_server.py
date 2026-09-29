"""MCP-сервер «Замполит» — формат времени события по времени начала (task5).

Роль в композиции task5:
  * получает СОБЫТИЯ календаря (за ближайшие 7 дней);
  * по ВРЕМЕНИ НАЧАЛА события выбирает ФОРМАТ вывода времени:
      - событие с 00:00 до 12:00  -> вывод В МИНУТАХ (M:SS);
      - событие с 12:00 до 24:00  -> вывод В ЧАСАХ   (HH.HHH, десятичные);
      - событие НА ВЕСЬ ДЕНЬ      -> вывод БЕЗ ИЗМЕНЕНИЯ в HH:MM:SS;
  * возвращает «пары» (событие + время начала в выбранном формате) —
    их сохраняет «Завхоз».

Как считается значение: берётся время НАЧАЛА события, переводится в число
секунд от начала суток (00:00:00) и приводится к выбранному формату:
    HH:MM:SS -> "%02d:%02d:%02d"   (напр. "09:30:00")
    минуты   -> "%d:%02d"  (M:SS)   (напр. "570:00")
    часы     -> "%.3f"     (HH.HHH) (напр. "9.500")

Событие «на весь день» приходит из календаря БЕЗ времени (только датой,
``"2026-09-30"``). Оркестратор размечает такие события флагом ``all_day``.

Сервер САМОДОСТАТОЧЕН по формату и НЕ ходит в сеть: значение берётся из
времени начала события. События ему передаёт вызывающий код (оркестратор —
веб-сервер) строкой JSON. Так серверы остаются раздельными (каждый — свой
stdio-подпроцесс), а композицию собирает оркестратор.

Инструменты:
  * classify_time    — в каком ФОРМАТЕ выводить время по строке начала («HH:MM»);
  * format_for_event — определить формат события и вернуть время начала в нём;
  * build_pairs      — обработать список событий (JSON) и вернуть «пары»
                       (событие + время начала в выбранном формате);
  * ping             — проверка живости сервера.

Запуск (как stdio-подпроцесс MCP):
    python zampolit_server.py
"""

import json

from mcp.server.mcpserver import MCPServer

mcp = MCPServer("zampolit")

# Границы суток для правил замполита:
#   [00:00, 12:00) -> МИНУТЫ ; [12:00, 24:00) -> ЧАСЫ ; весь день -> HH:MM:SS.
NOON_HOUR = 12

# Возможные режимы вывода времени.
MODE_MINUTES = "minutes"   # M:SS
MODE_HOURS = "hours"       # HH.HHH (десятичные часы)
MODE_FULL = "full"         # HH:MM:SS (без изменения)


# --------------------------------------------------------------------------
# Правила замполита (время начала -> формат вывода времени)
# --------------------------------------------------------------------------
def classify_time(time_str, all_day=False):
    """Определяет ФОРМАТ вывода времени по строке времени начала события.

    time_str — время в формате "HH:MM" или дата-время "YYYY-MM-DD HH:MM".
    all_day  — True, если событие длится весь день (формат HH:MM:SS).

    Возвращает dict:
        {mode, period, rule}
    где mode — "minutes" | "hours" | "full".
    Правило:
        событие на весь день     -> "full"    (HH:MM:SS, без изменения);
        00:00 <= время < 12:00   -> "minutes" (вывод в минутах M:SS);
        12:00 <= время <= 23:59  -> "hours"   (вывод в часах HH.HHH).
    Если время разобрать не удалось (и это не «весь день») — берём минуты.
    """
    if all_day or _is_all_day(time_str):
        return {"mode": MODE_FULL, "period": "весь день",
                "rule": "событие на весь день -> вывод без изменения (HH:MM:SS)"}
    hour = _extract_hour(time_str)
    if hour is not None and hour >= NOON_HOUR:
        return {"mode": MODE_HOURS, "period": "12:00–24:00",
                "rule": "событие после полудня -> вывод В ЧАСАХ (HH.HHH)"}
    return {"mode": MODE_MINUTES, "period": "00:00–12:00",
            "rule": "событие до полудня -> вывод В МИНУТАХ (M:SS)"}


def _is_all_day(time_str):
    """Событие «на весь день»? Если время начала не указано/пустое."""
    if time_str is None:
        return True
    return str(time_str).strip() == ""


def _extract_hour(time_str):
    """Достаёт ЧАС (0–23) из строки времени. None, если не удалось.

    Поддерживаемые формы: "HH:MM", "YYYY-MM-DD HH:MM", "YYYY-MM-DDTHH:MM",
    "YYYY-MM-DD HH:MM:SS". Берём первые две цифры времени.
    """
    hms = _extract_hms(time_str)
    if hms is None:
        return None
    return hms[0]


def _extract_hms(time_str):
    """Достаёт (h, m, s) из строки времени. None, если не удалось.

    Формы: "HH:MM", "YYYY-MM-DD HH:MM", "YYYY-MM-DDTHH:MM:SS". Часы берём
    из части после пробела (дата-время), иначе из начала строки.
    """
    if time_str is None:
        return None
    s = str(time_str).strip().replace("T", " ")
    if not s:
        return None
    part = s.split(" ")[-1]          # "HH:MM" или "HH:MM:SS"
    bits = part.split(":")
    try:
        h = int(bits[0])
        m = int(bits[1]) if len(bits) > 1 else 0
        sec = int(bits[2]) if len(bits) > 2 else 0
    except (TypeError, ValueError, IndexError):
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59 and 0 <= sec <= 59):
        return None
    return h, m, sec


def _seconds_since_midnight(time_str):
    """Секунды от начала суток (00:00:00) до времени начала события (или None)."""
    hms = _extract_hms(time_str)
    if hms is None:
        return None
    h, m, s = hms
    return h * 3600 + m * 60 + s


def format_time(time_str, mode):
    """Приводит время начала события к выбранному формату.

    time_str — время начала ("HH:MM" / "YYYY-MM-DD HH:MM" / …);
    mode     — один из "full" (HH:MM:SS), "minutes" (M:SS), "hours" (HH.HHH).
    Возвращает отформатированную строку. Если время не разобрать — "".

    ОСОБЫЙ СЛУЧАЙ: событие НА ВЕСЬ ДЕНЬ (нет времени начала). Тогда значение
    считаем началом суток (00:00:00): формат "full" выдаёт "00:00:00"
    (вывод «без изменения»), другие форматы — "0:00" / "0.000".
    """
    secs = _seconds_since_midnight(time_str)
    if secs is None:
        # Нет времени: для события на весь день берём начало суток.
        if mode == MODE_FULL:
            return "00:00:00"
        secs = 0
    if mode == MODE_FULL:
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return "%02d:%02d:%02d" % (h, m, s)
    if mode == MODE_MINUTES:
        m, s = divmod(secs, 60)
        return "%d:%02d" % (m, s)
    # MODE_HOURS
    return "%.3f" % (secs / 3600.0)


# --------------------------------------------------------------------------
# Инструменты MCP
# --------------------------------------------------------------------------
@mcp.tool()
def ping() -> str:
    """Проверка живости сервера «Замполит»."""
    return "Замполит на связи."


@mcp.tool()
def classify_time_tool(time: str) -> str:
    """По времени начала события вернуть, в каком ФОРМАТЕ выводить время.

    time — время начала ("HH:MM" или "YYYY-MM-DD HH:MM"). Пустая строка —
    событие на весь день.
    Правило: до полудня — в минутах, после полудня — в часах, весь день —
    без изменения (HH:MM:SS).
    """
    info = classify_time(time)
    sample = format_time(time, info["mode"])
    return ("%s. Формат: %s (период %s). Пример: %s"
            % (info["rule"], info["mode"], info["period"], sample or "—"))


@mcp.tool()
def format_for_event(event_time: str) -> str:
    """Определить формат по времени события и вернуть время в этом формате.

    event_time — время начала события ("HH:MM" или "YYYY-MM-DD HH:MM");
                 пустая строка — событие на весь день.
    Возвращает строку: какой период, какой формат и время начала в нём.
    """
    info = classify_time(event_time)
    value = format_time(event_time, info["mode"])
    if not value:
        return "%s. Время не разобрано (формат %s)." % (info["rule"], info["mode"])
    return "%s. Формат %s: %s" % (info["rule"], info["mode"], value)


@mcp.tool()
def build_pairs(events_json: str) -> str:
    """Обработать СПИСОК событий (JSON) и вернуть «пары» (событие + время).

    events_json — JSON-массив событий вида
        [{"summary": "Перекур", "start": "2026-09-26 10:00"}, …]
    (поле времени — "start" или "event_start"; название — "summary"
    или "event_summary"). Событие БЕЗ времени начала, с "all_day": true
    или с датой без времени считается событием на весь день.

    Для КАЖДОГО события: по времени начала определяется ФОРМАТ вывода
    (до полудня — минуты, после — часы, весь день — HH:MM:SS), время начала
    приводится к этому формату, и результат собирается в JSON-массив:
        [{"event": "...", "start": "...", "mode": "minutes",
          "period": "00:00–12:00", "time": "570:00"}, …]
    """
    try:
        raw = json.loads(_as_json(events_json))
    except Exception as exc:
        return "Ошибка разбора events_json: %s" % exc
    if not isinstance(raw, list):
        return "events_json должен быть JSON-МАССИВОМ событий."
    pairs = []
    for ev in raw:
        if not isinstance(ev, dict):
            continue
        title = (ev.get("summary") or ev.get("event_summary") or "событие")
        start = (ev.get("start") or ev.get("event_start") or "")
        all_day = (bool(ev.get("all_day")) or _is_all_day(start)
                   or not _extract_hms(start))
        info = classify_time(start, all_day=all_day)
        value = format_time(start, info["mode"])
        pairs.append({
            "event": str(title),
            "start": str(start),
            "mode": info["mode"],
            "period": info["period"],
            "time": value,
        })
    return json.dumps({"pairs": pairs, "count": len(pairs)}, ensure_ascii=False)


def _as_json(value):
    """Если пришёл уже dict/list — сериализуем; строку возвращаем как есть."""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value or "")


if __name__ == "__main__":
    # Транспорт по умолчанию — stdio (как подпроцесс MCP-клиента проекта).
    mcp.run()
