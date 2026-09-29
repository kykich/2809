 # -*- coding: utf-8 -*-
"""Автономная проверка КОМПОЗИЦИИ MCP-серверов (docs/task5.md).

Сеть и реальные API НЕ требуются: проверяем ЛОГИКУ пайплайна
  календарь -> замполит -> завхоз (и вывод «доски» таблицей):
  * правило замполита «время -> ФОРМАТ вывода времени» (до полудня — минуты,
    после полудня — часы, весь день — без изменения HH:MM:SS);
  * разбор текста календаря в список событий (оркестратор), включая события
    НА ВЕСЬ ДЕНЬ (дата без времени);
  * сохранение пар «Завхозом» в JSON и чтение обратно;
  * «Доска» строит таблицу по сохранённым данным.

Запуск:
    python tests/check_composition.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.harness import check, section, finish, ensure_utf8

import zampolit_server
import zavhoz_server
import doska_server


def main():
    ensure_utf8()

    # --------------------------------------------------------------
    section("1. Замполит: правило «время -> формат вывода времени»")
    # До полудня (00:00–11:59) — вывод В МИНУТАХ.
    m = zampolit_server.classify_time("2026-09-26 09:30")
    check("09:30 -> режим minutes", m["mode"] == "minutes", "%r" % m)
    check("период утра = 00:00–12:00", m["period"] == "00:00–12:00")
    check("09:30 -> 570:00 (минуты)", 
          zampolit_server.format_time("2026-09-26 09:30", m["mode"]) == "570:00")
    # Ровно полдень (12:00) — уже после полудня, вывод В ЧАСАХ.
    e = zampolit_server.classify_time("12:00")
    check("12:00 -> режим hours", e["mode"] == "hours", "%r" % e)
    check("12:00 -> 12.000 (часы)", 
          zampolit_server.format_time("12:00", e["mode"]) == "12.000")
    # Ночь (00:00) — утреннее правило (минуты).
    n = zampolit_server.classify_time("00:00")
    check("00:00 -> режим minutes", n["mode"] == "minutes", "%r" % n)
    # Поздний вечер — вечернее правило (часы).
    late = zampolit_server.classify_time("2026-09-26 23:45")
    check("23:45 -> режим hours", late["mode"] == "hours", "%r" % late)
    check("23:45 -> 23.750 (часы)", 
          zampolit_server.format_time("2026-09-26 23:45", late["mode"]) == "23.750")
    # Только время без даты.
    t = zampolit_server.classify_time("15:10")
    check("15:10 (только время) -> hours", t["mode"] == "hours")
    # Событие НА ВЕСЬ ДЕНЬ -> без изменения, HH:MM:SS.
    ad = zampolit_server.classify_time("", all_day=True)
    check("весь день -> режим full", ad["mode"] == "full", "%r" % ad)
    check("весь день -> 00:00:00 (HH:MM:SS)",
          zampolit_server.format_time("", ad["mode"]) == "00:00:00")
    check("период весь день", ad["period"] == "весь день")

    # --------------------------------------------------------------
    section("2. Оркестратор: разбор текста календаря в события")
    cal_text = (
        "События за 2026-09-26 — 2026-10-03:\n"
        "  UID: abc@yandex\n"
        "  2026-09-26 09:30 — Перекур, до 2026-09-26 09:45, место: офис\n"
        "  UID: def@yandex\n"
        "  2026-09-26 15:00 — Совещание\n"
        "  UID: ghi@yandex\n"
        "  2026-09-27 21:15 — Звонок, место: дом\n"
        "  UID: jkl@yandex\n"
        "  2026-09-30 — Отпуск, до 2026-10-01\n"
    )
    from web.server import _parse_calendar_events
    events = _parse_calendar_events(cal_text)
    check("найдено 4 события", len(events) == 4, "%r" % events)
    check("событие 1: название «Перекур»",
          events[0]["summary"] == "Перекур", "%r" % events[0])
    check("событие 1: время 09:30", events[0]["start"] == "2026-09-26 09:30")
    check("событие 1: не «весь день»", events[0]["all_day"] is False)
    check("событие 2: название «Совещание»",
          events[1]["summary"] == "Совещание", "%r" % events[1])
    check("событие 3: название «Звонок»",
          events[2]["summary"] == "Звонок", "%r" % events[2])
    # Событие НА ВЕСЬ ДЕНЬ (дата без времени) — распознаётся и помечается.
    check("событие 4: «Отпуск» на весь день",
          events[3]["summary"] == "Отпуск"
          and events[3]["all_day"] is True, "%r" % events[3])
    check("событие 4: start = дата (без времени)",
          events[3]["start"] == "2026-09-30", "%r" % events[3])
    # Пустой ответ календаря -> пустой список.
    check("пустой текст -> нет событий",
          _parse_calendar_events("Событий за период ... нет.") == [])

    # --------------------------------------------------------------
    section("3. Завхоз: сохранение и чтение пар (JSON-файл)")
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    os.environ["ZAVHOZ_FILE"] = path
    pairs = [
        {"event": "Перекур", "start": "2026-09-26 09:30",
         "mode": "minutes", "period": "00:00–12:00", "time": "570:00"},
        {"event": "Совещание", "start": "2026-09-26 15:00",
         "mode": "hours", "period": "12:00–24:00", "time": "15.000"},
        {"event": "Отпуск", "start": "2026-09-30",
         "mode": "full", "period": "весь день", "time": "00:00:00"},
    ]
    rep = zavhoz_server.save_pairs(json.dumps({"pairs": pairs}))
    check("сохранение сообщило о 3 парах", "Сохранено пар: 3" in rep, rep)
    check("файл создан", os.path.isfile(path))
    loaded = json.loads(zavhoz_server.load_pairs())
    check("в файле 3 пары", loaded.get("count") == 3, "%r" % loaded)
    check("событие сохранено верно",
          loaded["pairs"][0]["event"] == "Перекур")
    check("время сохранено верно", loaded["pairs"][0]["time"] == "570:00")
    # Принимаем и «голый» массив пар.
    rep2 = zavhoz_server.save_pairs(json.dumps(pairs[:1]))
    check("массив пар тоже принимается", "Сохранено пар: 1" in rep2, rep2)

    # --------------------------------------------------------------
    section("4. Доска: таблица «событие ↔ время»")
    # Вернём все три пары и построим таблицу.
    zavhoz_server.save_pairs(json.dumps({"pairs": pairs}))
    board = doska_server.show_board()
    check("таблица содержит заголовок «Событие»", "Событие" in board)
    check("таблица содержит колонку «Время»", "Время" in board)
    check("таблица содержит «Перекур»", "Перекур" in board)
    check("таблица содержит время 570:00", "570:00" in board)
    check("таблица содержит время 15.000", "15.000" in board)
    check("итоговая строка про 3 события", "Всего событий: 3" in board)
    # HTML-вариант.
    html = doska_server.show_board_html()
    check("HTML содержит <table>", "<table" in html)
    check("HTML содержит название события", "Перекур" in html)

    # --------------------------------------------------------------
    section("5. Доска пуста без данных")
    empty_path = path + ".empty"
    os.environ["ZAVHOZ_FILE"] = empty_path
    check("на пустом хранилище — сообщение о пустоте",
          "пуст" in doska_server.show_board().lower())
    check("HTML на пустом хранилище — заглушка",
          "board-empty" in doska_server.show_board_html())

    # --------------------------------------------------------------
    section("6. Композиция: отчёт + сообщение о серверах (для чата)")
    # Оркестратор _composition_run с ЗАГЛУШКОЙ MCP: проверяем, что отчёт
    # содержит состав серверов и что _format_composition_message выдаёт
    # отдельное сообщение с названиями и кратким описанием каждого MCP.
    import web.server as ws

    def _fake_call(tool, args=None, timeout=None, server_id=None):
        if tool == "list_events":
            return {"ok": True, "text":
                    "  2026-09-26 09:30 — Утренник, до 10:00\n"
                    "  2026-09-26 15:00 — Вечер, до 16:00"}
        if tool == "build_pairs":
            return {"ok": True, "text": json.dumps({"pairs": [
                {"event": "Утренник", "start": "2026-09-26 09:30",
                 "mode": "minutes", "period": "00:00–12:00", "time": "570:00"},
                {"event": "Вечер", "start": "2026-09-26 15:00",
                 "mode": "hours", "period": "12:00–24:00", "time": "15.000"}]},
                ensure_ascii=False)}
        if tool == "save_pairs":
            return {"ok": True, "text": "Сохранено пар: 2"}
        if tool == "show_board":
            return {"ok": True, "text":
                    "Событие | Начало | Период | Формат | Время\n"
                    "Утренник | 2026-09-26 09:30 | 00:00–12:00 | minutes | 570:00"}
        if tool == "show_board_html":
            return {"ok": True, "text":
                    '<table class="board-table"><thead><tr><th>Событие</th>'
                    '</tr></thead><tbody><tr><td>Утренник</td></tr></tbody>'
                    '</table>'}
        return {"ok": False, "error": "unknown %s" % tool}

    class _FakeClient:
        @staticmethod
        def mcp_call_tool(tool, args=None, timeout=None, server_id=None):
            return _fake_call(tool, args, timeout, server_id)

    real_client = ws.mcp_client
    ws.mcp_client = _FakeClient
    try:
        report = ws._composition_run(verbose=False)
        check("отчёт композиции успешен", report.get("ok") is True,
              "%r" % report.get("error"))
        check("обработано 2 пары", report.get("count") == 2)
        ids = [s.get("id") for s in (report.get("servers") or [])]
        check("в отчёте все 5 серверов",
              ids == ["calendar", "currency", "zampolit", "zavhoz", "doska"],
              "%r" % ids)
        msg = ws._format_composition_message(report)
        check("сообщение — отдельное (о запуске)",
              "Композиция MCP запущена" in msg)
        check("сообщение перечисляет «Календарь»", "**Календарь**" in msg)
        check("сообщение перечисляет «Замполит»", "**Замполит**" in msg)
        check("сообщение перечисляет «Завхоз»", "**Завхоз**" in msg)
        check("сообщение перечисляет «Доска»", "**Доска**" in msg)
        check("в сообщении есть описание работы",
              "сохраняет пары" in msg, msg)
        check("в сообщении есть итог прогона", "Итог:" in msg)
    finally:
        ws.mcp_client = real_client

    # --------------------------------------------------------------
    section("7. Запрос к «Доске» независимо от чекбокса (прямой вывод)")
    # Проверяем через HTTP: при ВЫКЛЮЧЕННОМ чекбоксе «Использовать MCP», но
    # выбранном сервере «Доска», запрос идёт MCP-путём и выводит таблицу
    # ДЕТЕРМИНИРОВАННО (без выбора инструмента моделью).
    import threading
    import time as _time
    import urllib.request

    seen = {"answer": 0, "via_mcp": None}

    class _FakeSysClient:
        @staticmethod
        def mcp_call_tool(tool, args=None, timeout=None, server_id=None):
            return _fake_call(tool, args, timeout, server_id)

        @staticmethod
        def mcp_servers():
            return [{"id": "doska", "label": "Доска"}]

        @staticmethod
        def mcp_list_tools(server_id=None):
            return {"ok": True, "tools": [{"name": "show_board"}]}

        @staticmethod
        def mcp_status(server_id=None):
            return {"ok": True, "connected": True, "tools_count": 1,
                    "tools": ["show_board"], "server": ""}

    class _FakeAgent:
        label = "stub"

        def available(self):
            return []

        def answer(self, *a, **k):
            seen["answer"] += 1
            return {"ok": True, "text": "regular", "html": "", "answers": []}

        def answer_via_mcp(self, question, tools, model=None, server_id=None):
            seen["via_mcp"] = {"model": model, "server_id": server_id}
            return {"ok": True, "text": "board table", "html": "", "answers": []}

    ws.mcp_client = _FakeSysClient
    old_enabled, old_server, old_model = (ws._ServerState.mcp_enabled,
                                          ws._ServerState.mcp_server,
                                          ws._ServerState.mcp_model)
    # Чекбокс ВЫКЛЮЧЕН, выбран сервер «Доска», модель «модель MCP» = GigaChat.
    ws._ServerState.mcp_enabled = False
    ws._ServerState.mcp_server = "doska"
    ws._ServerState.mcp_model = "GigaChat"
    httpd = ws.create_server(_FakeAgent(), host="127.0.0.1", port=0)
    hport = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _time.sleep(0.2)
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/ask" % hport,
            data=json.dumps({"question": "покажи доску"}).encode(),
            headers={"Content-Type": "application/json"})
        resp = json.loads(urllib.request.urlopen(req).read().decode("utf-8"))
        check("запрос к «Доске» успешен при выкл. чекбоксе", resp.get("ok") is True,
              "%r" % resp.get("error"))
        # «Доска» обрабатывается ДЕТЕРМИНИРОВАННО (_board_direct_result),
        # без выбора инструмента моделью (answer_via_mcp НЕ вызывается).
        check("ответ содержит таблицу доски",
              "Событие" in (resp.get("text") or ""), "%r" % resp.get("text"))
        check("обычный путь агента НЕ вызывался", seen["answer"] == 0)
        check("MCP-путь выбора инструмента НЕ использован (доска прямая)",
              seen["via_mcp"] is None)
    finally:
        httpd.shutdown()
        ws.mcp_client = real_client
        ws._ServerState.mcp_enabled = old_enabled
        ws._ServerState.mcp_server = old_server
        ws._ServerState.mcp_model = old_model

    # --------------------------------------------------------------
    section("8. Композиция и «Использовать MCP» — ВЗАИМОИСКЛЮЧАЮЩИЕ")
    # Проверяем переходы режимов (через HTTP /api/mcp):
    #   * запуск композиции -> MCP ВЫКЛючен (чекбокс), композиция ВКЛючена;
    #   * включение MCP     -> композиция ОСТАНОВЛена, MCP включён;
    #   * выключение MCP    -> оба выключены.
    # И то, что при активной композиции ЛЮБОЕ обращение в чате ВСЕГДА идёт к
    # «Доске» (серверу ВЫВОДА) — таблицей, без блокировки «выберите модель».
    seen2 = {"answer": 0, "via_mcp": None, "model": None, "server": None}

    class _FakeAgent2:
        label = "stub"

        def available(self):
            return []

        def answer(self, *a, **k):
            seen2["answer"] += 1
            return {"ok": True, "text": "regular", "html": "", "answers": []}

        def answer_via_mcp(self, question, tools, model=None, server_id=None):
            seen2["via_mcp"] = True
            seen2["model"] = model
            return {"ok": True, "text": "mcp", "html": "", "answers": []}

        def compose_message(self, text, model=None, fallback=""):
            return {"ok": False, "text": fallback, "model": ""}

    ws.mcp_client = _FakeSysClient
    ws._ServerState.mcp_enabled = False
    ws._ServerState.mcp_server = "calendar"   # НЕ «Доска»
    ws._ServerState.mcp_model = "GigaChat"    # «Модель MCP» из селекта
    ws._ServerState.composition_running = False
    httpd2 = ws.create_server(_FakeAgent2(), host="127.0.0.1", port=0)
    hport2 = httpd2.server_address[1]
    threading.Thread(target=httpd2.serve_forever, daemon=True).start()
    _time.sleep(0.2)

    def _post2(obj):
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/mcp" % hport2,
            data=json.dumps(obj).encode(),
            headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read().decode("utf-8"))

    def _ask2():
        req = urllib.request.Request(
            "http://127.0.0.1:%d/api/ask" % hport2,
            # Модель выбрана сверху — при выключенных MCP/композиции запрос
            # должен идти ОБЫЧНЫМ путём (напрямую к моделям), не к «Доске».
            data=json.dumps({"question": "show_board",
                             "models": [{"label": "Fake"}]}).encode(),
            headers={"Content-Type": "application/json"})
        return json.loads(urllib.request.urlopen(req).read().decode("utf-8"))

    try:
        # --- Запуск композиции: MCP ВЫКЛючается, композиция ВКЛючается ---
        r = _post2({"action": "compose"})
        check("после запуска композиции MCP выключен", r.get("enabled") is False)
        check("после запуска композиции режим композиции ВКЛючен",
              r.get("composition_running") is True,
              "%r" % r.get("composition_running"))

        # При активной композиции ЛЮБОЕ обращение в чате -> к «ДОСКЕ»
        # (детерминированная таблица), даже если в списке выбран календарь.
        seen2["answer"] = 0
        seen2["via_mcp"] = None
        resp = _ask2()
        check("при композиции запрос НЕ блокируется (нет ошибки)",
              resp.get("ok") is True, "%r" % resp.get("error"))
        check("при композиции ответ — ТАБЛИЦА «Доски» (Событие/Время)",
              "Событие" in (resp.get("text") or ""), "%r" % resp.get("text"))
        check("при композиции обычный путь агента НЕ вызывался",
              seen2["answer"] == 0, "%r" % seen2)
        check("при композиции выбор инструмента моделью НЕ использован "
              "(доска прямая)",
              seen2["via_mcp"] is None, "%r" % seen2)

        # --- Включение чекбокса MCP: композиция ОСТАНАВЛИВАЕТСЯ ---
        r = _post2({"action": "set", "enabled": True})
        check("при включении MCP композиция ОСТАНОВЛена",
              r.get("composition_running") is False,
              "%r" % r.get("composition_running"))
        check("при включении MCP чекбокс включён", r.get("enabled") is True)

        # Запрос при включённом MCP -> через MCP (выбор инструмента моделью).
        seen2["answer"] = 0
        seen2["via_mcp"] = None
        _ask2()
        check("при включённом MCP запрос идёт ЧЕРЕЗ MCP",
              seen2["via_mcp"] is True and seen2["answer"] == 0, "%r" % seen2)

        # --- Выключение MCP: оба режима выключены ---
        r = _post2({"action": "set", "enabled": False})
        check("после выключения MCP: MCP выключен", r.get("enabled") is False)
        check("после выключения MCP: композиция выключена",
              r.get("composition_running") is False)

        # Обычный запрос при выкл. MCP (и без композиции) -> ОБЫЧНЫЙ путь
        # агента (напрямую к моделям), НЕ к «Доске».
        seen2["answer"] = 0
        seen2["via_mcp"] = None
        resp = _ask2()
        check("при выкл. MCP запрос идёт ОБЫЧНЫМ путём (не к «Доске»)",
              seen2["answer"] == 1 and seen2["via_mcp"] is None
              and "Событие" not in (resp.get("text") or ""), "%r" % seen2)
    finally:
        httpd2.shutdown()
        ws.mcp_client = real_client
        ws._ServerState.composition_running = False
        ws._stop_composition_tick()
        ws._ServerState.mcp_enabled = old_enabled
        ws._ServerState.mcp_server = old_server
        ws._ServerState.mcp_model = old_model

    # Очистим временные файлы.
    for p in (path, empty_path):
        try:
            os.remove(p)
        except OSError:
            pass

    sys.exit(finish())


if __name__ == "__main__":
    main()
