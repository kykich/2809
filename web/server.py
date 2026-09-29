

"""Встроенный HTTP-сервер (стандартная библиотека).

Чат "только в окне": сервер ничего не хранит на диске. История ведётся в
памяти вкладки и присылается целиком в POST /api/ask.

Сервер работает через АГЕНТА (rtk_app.agent.Agent) — отдельную сущность,
которая инкапсулирует всю логику запросов к LLM. Агент принимает вопрос и
историю диалога, сам обращается к моделям через API и возвращает готовый
результат (html, text, ответы моделей). HTTP-обработчик лишь передаёт данные
между браузером и агентом.

Запрос пользователя уходит агенту, который опрашивает модели:
  - DeepSeek-flash,
  - GigaChat (базовая, простая).

JSON-API:
    GET  /                  - страница (index.html)
    GET  /css/*,/js/*       - стили и скрипты
    GET  /api/model         - список моделей, которые обслуживает агент
    GET  /api/session       - сохранённая история + стратегия + facts + ветки
    POST /api/ask           - {question, models[], max_tokens?, compact?,
                               strategy?} -> ответ
    POST /api/newchat       - начать новый разговор (очистить историю)
    POST /api/compact       - {enabled, keep} -> настройки сжатия (summary)
    POST /api/compact_summary - {keep} -> дописать вытесненное в summary
    POST /api/strategy      - {strategy, window} -> стратегия контекста
                              (none / sliding / facts / branch)
    POST /api/facts         - {facts:{ключ:значение}} -> сохранить блок facts
    POST /api/memory        - {type:"working"|"longterm", …} -> память агента
                              (действия: set/delete/replace/clear, GET-снимок)
    POST /api/branches      - {action:"create"|"switch"|"delete"|"rename", …}
                              -> ветки (rename: {index, name})
    POST /api/task          - состояние задачи (Task State Machine):
                              {action:"start"|"advance"|"pause"|"resume"|
                               "finish"|"reset"|"state", …} -> этап/шаг/
                              ожидаемое действие (planning->execution->
                              validation->done), пауза/продолжение.
    GET  /api/invariants    - инварианты (правила-ограничения) + категории
    POST /api/invariants    - {action:"add"|"update"|"delete"|"replace"|
                               "clear"|"state", …} -> инварианты (отдельно от
                              диалога; жёсткие правила, нарушать нельзя)
    GET  /api/profiles      - список профилей (персон) + активный
    POST /api/profiles      - {action:"create"|"switch"|"update"|"delete", …}
                              -> профили (персоны): при создании задаются
                              name, model (модель персоны), character
                              (характер/тон) и style (характер ответов).
                              Ответы даёт АКТИВНАЯ персона своей моделью;
                              при отсутствии персон диалог идёт с моделями.
    GET  /api/mcp           - состояние MCP: {enabled, model, tools, status}
                              (настройки + последний статус сервера)
    POST /api/mcp           - {action, …} -> настройки и статус MCP:
                              "state"  — вернуть текущее состояние (по умолч.);
                              "set"    — сохранить {enabled?, model?, server?};
                              "status" — ПРОВЕРИТЬ статус MCP-сервера
                                         (подключиться, получить список
                                         инструментов и вернуть результат);
                              "compose"— ЗАПУСТИТЬ КОМПОЗИЦИЮ MCP-серверов
                                         (пайплайн календарь → замполит →
                                         завхоз; см. docs/task5.md) и вернуть
                                         отчёт о прогоне. Композиция
                                         ВЗАИМОИСКЛЮЧАЮЩА с чекбоксом
                                         «Использовать MCP»: её запуск
                                         выключает чекбокс, а включение
                                         чекбокса — останавливает композицию
                                         (см. ниже);
                              "compose_stop" — остановить режим композиции
                                         (возврат к отдельным серверам).
    """
import json
import mimetypes
import os
import webbrowser
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rtk_app import config
from rtk_app.agent import Agent
from rtk_app.session_store import SessionStore

try:
    # MCP-клиент (опциональная зависимость): сервер проекта должен
    # запускаться даже без пакета mcp — тогда статус MCP будет «недоступен».
    from rtk_app import mcp_client
except Exception:  # pragma: no cover — зависит от окружения
    mcp_client = None


class _ServerState:
    """Глобальное состояние сервера: агент (единая сущность) и сессия."""
    agent = None
    session = None
    # Настройки MCP (включается чекбоксом в левой колонке):
    #   enabled — использовать ли инструменты MCP;
        #   model   — метка модели, применяемой при работе с MCP;
    #   status  — последний результат проверки статуса MCP-сервера.
    mcp_enabled = getattr(config, "MCP_ENABLED", False)
    mcp_model = getattr(config, "MCP_MODEL", "")
    # id выбранного MCP-сервера (см. config.MCP_SERVERS).
    mcp_server = getattr(config, "MCP_SERVER_DEFAULT", "calendar")
    mcp_status = None
    mcp_lock = threading.Lock()
    # КОМПОЗИЦИЯ MCP: запущена ли она сейчас.
    # ВАЖНО: композиция и режим «Использовать MCP» — ВЗАИМОИСКЛЮЧАЮЩИЕ.
    #   * запуск композиции — выключает чекбокс «Использовать MCP»
    #     (композиция работает сама по себе);
    #   * включение чекбокса «Использовать MCP» — останавливает композицию
    #     и переводит сервер в режим отдельных серверов по выбору.
    composition_running = False
    # Поток фонового ТИКА композиции (запускается только в режиме композиции).
    composition_thread = None
    # Событие остановки тика композиции (устанавливается при выключении).
    composition_stop = None


def _mcp_settings_file():
    """Путь к файлу настроек MCP (в папке сессии)."""
    return getattr(config, "MCP_SETTINGS_FILE", None)


def _load_mcp_settings():
    """Загружает настройки MCP из файла (если есть), заполняя состояние."""
    path = _mcp_settings_file()
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            _ServerState.mcp_enabled = bool(data.get("enabled",
                                                   _ServerState.mcp_enabled))
            model = data.get("model")
            if isinstance(model, str):
                _ServerState.mcp_model = model
            server = data.get("server")
            if isinstance(server, str) and server:
                _ServerState.mcp_server = server
            # Режим композиции (взаимоисключающий с MCP).
            comp = data.get("composition")
            if comp is not None:
                _ServerState.composition_running = bool(comp)
    except Exception as exc:
        print("[MCP] не удалось загрузить настройки: %s" % exc, flush=True)


def _save_mcp_settings():
    """Сохраняет настройки MCP на диск (в папку сессии)."""
    path = _mcp_settings_file()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"enabled": _ServerState.mcp_enabled,
                       "model": _ServerState.mcp_model,
                       "server": _ServerState.mcp_server,
                       "composition": _ServerState.composition_running}, f,
                      ensure_ascii=False, indent=2)
    except Exception as exc:
        print("[MCP] не удалось сохранить настройки: %s" % exc, flush=True)

def _mcp_state_payload(check=False):
    """Собирает состояние MCP для ответа клиенту.

    check=True — предварительно ПРОВЕРЯЕТ статус сервера (подключение +
    список инструментов) и обновляет сохранённый статус.
        """
    if check:
        _ServerState.mcp_status = _probe_mcp_status()
    status = _ServerState.mcp_status
    servers = []
    if mcp_client is not None:
        try:
            servers = mcp_client.mcp_servers()
        except Exception:
            servers = []
    return {
        "ok": True,
        "enabled": bool(_ServerState.mcp_enabled),
        "model": _ServerState.mcp_model or "",
        "server": _ServerState.mcp_server or "",
        "servers": servers,
        "available": mcp_client is not None,
        "status": status,
        # Режим композиции (взаимоисключающий с «Использовать MCP»).
        # ВАЖНО: ключ называется composition_running, чтобы НЕ конфликтовать
        # с ключом "composition" (ОТЧЁТ прогона) в ответе на action="compose".
        "composition_running": bool(_ServerState.composition_running),
    }


def _probe_mcp_status():
    """Проверяет статус MCP-сервера (через mcp_client.mcp_status)."""
    if mcp_client is None:
        return {"ok": False, "connected": False, "tools_count": 0,
                "tools": [], "server": "",
                "error": "MCP-клиент недоступен (не установлен пакет mcp)."}
    try:
        return mcp_client.mcp_status(server_id=(_ServerState.mcp_server or None))
    except Exception as exc:
        return {"ok": False, "connected": False, "tools_count": 0,
                "tools": [], "server": "", "error": str(exc)}


def _collect_due_reminders():
    """Собирает НАСТУПИВШИЕ напоминания календаря (для доставки в чат).

    Напоминания выдаёт MCP-инструмент ``run_due`` календарного сервера —
    он же делает «ленивый прогон» и возвращает готовые тексты. Вызываем его
    АВТОМАТИЧЕСКИ при каждом запросе пользователя, чтобы напоминание
    приходило САМО, а не только когда модель «догадается» вызвать инструмент.

    Возвращает список строк-напоминаний (может быть пустым). Ошибки MCP
    игнорируются (возвращается []): доставка напоминаний не должна ломать
    основной ответ. Работает только для календарного сервера.
    """
    if mcp_client is None:
        return []
    # Только у календарного MCP-сервера есть инструмент run_due.
    server_id = _ServerState.mcp_server or None
    try:
        res = mcp_client.mcp_call_tool("run_due", {}, server_id=server_id)
    except Exception as exc:
        print("[REMINDER] не удалось собрать напоминания: %s" % exc,
              flush=True)
        return []
    if not res.get("ok"):
        # Инструмента run_due нет (другой сервер) или ошибка — молча пропускаем.
        return []
    text = (res.get("text") or "").strip()
    if not text or "нет" in text.lower() and "напоминан" in text.lower():
        # «Наступивших напоминаний нет.» — пустой результат.
        return []
    # Ответ инструмента run_due: первая строка — «Напоминаний: N», далее тексты
    # напоминаний. Отделяем служебную шапку, если она есть.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and lines[0].lower().startswith("напоминаний:"):
        lines = lines[1:]
    return lines


def _mcp_text(server_id, tool, arguments=None, timeout=None):
    """Вызывает MCP-инструмент и возвращает текст результата.

    Возвращает (ok, text): ok=False, если клиент недоступен или инструмент
    сообщил об ошибке. Обёртка над mcp_client.mcp_call_tool с явным сроком.
    """
    if mcp_client is None:
        return False, "MCP-клиент недоступен (не установлен пакет mcp)."
    try:
        res = mcp_client.mcp_call_tool(tool, arguments or {},
                                       timeout=timeout, server_id=server_id)
    except Exception as exc:
        return False, "Ошибка вызова %s: %s" % (tool, exc)
    if not res.get("ok"):
        return False, res.get("error") or res.get("text") or "ошибка инструмента"
    return True, (res.get("text") or "")


def _composition_run(verbose=False):
    """ЗАПУСКАЕТ КОМПОЗИЦИЮ MCP-серверов (см. docs/task5.md).

    Пайплайн:
      1) «календарь» — события за COMPOSITION_DAYS_AHEAD дней (вперёд от сегодня);
      2) «замполит»  — по каждому событию определяется ФОРМАТ вывода времени
                       по времени начала (минуты/часы/без изменения) и время
                       начала приводится к нему (build_pairs);
      3) «завхоз»    — сохранение пар «событие + время» в JSON-файл (save_pairs).

    «Доска» в композиции не участвует (это сервер ВЫВОДА по запросу в чате),
    но результат «завхоза» ей потом доступен для таблицы.

    Возвращает dict с отчётом: {ok, calendar, pairs, saved, error, steps}.
    События календаря передаются «замполиту» строкой JSON (серверы остаются
    раздельными stdio-процессами; композицию собирает оркестратор).
    """
    timeout = getattr(config, "COMPOSITION_CALL_TIMEOUT", 30)
    days = int(getattr(config, "COMPOSITION_DAYS_AHEAD", 7) or 7)
    cal_srv = getattr(config, "COMPOSITION_CALENDAR_SERVER", "calendar")
    zam_srv = getattr(config, "COMPOSITION_ZAMPOLIT_SERVER", "zampolit")
    zav_srv = getattr(config, "COMPOSITION_ZAVHOZ_SERVER", "zavhoz")
    steps = []
    # Состав композиции для СООБЩЕНИЯ в чат (названия серверов + описания).
    servers = list(getattr(config, "COMPOSITION_SERVERS", []) or [])

    # --- Шаг 1: события календаря на ближайшие N дней ---
    today = time.strftime("%Y-%m-%d")
    until = time.strftime("%Y-%m-%d", time.localtime(time.time() + days * 86400))
    ok, cal_text = _mcp_text(cal_srv, "list_events",
                             {"start": today, "end": until}, timeout=timeout)
    steps.append({"step": "calendar", "ok": ok, "detail": cal_text[:400]})
    if not ok:
        return {"ok": False, "error": "Календарь: %s" % cal_text,
                "steps": steps, "pairs": [], "count": 0, "servers": servers}
    events = _parse_calendar_events(cal_text)
    steps.append({"step": "calendar_parsed", "ok": True,
                  "detail": "событий: %d" % len(events)})
    if not events:
        # Нет событий — композиция формально успешна, но сохранять нечего.
        ok, sav_text = _mcp_text(zav_srv, "save_pairs",
                                 {"pairs_json": json.dumps({"pairs": []}),
                                  "source": "composition"}, timeout=timeout)
        steps.append({"step": "zavhoz", "ok": ok, "detail": sav_text[:400]})
        return {"ok": True, "calendar": cal_text, "pairs": [], "count": 0,
                "saved": sav_text, "steps": steps, "servers": servers,
                "detail": "Событий на ближайшие %d дней нет." % days}

    # --- Шаг 2: замполит — пары «событие + время» ---
    ok, zam_text = _mcp_text(zam_srv, "build_pairs",
                             {"events_json": json.dumps(events, ensure_ascii=False)},
                             timeout=timeout)
    steps.append({"step": "zampolit", "ok": ok, "detail": zam_text[:600]})
    if not ok:
        return {"ok": False, "error": "Замполит: %s" % zam_text,
                "steps": steps, "pairs": [], "count": 0, "servers": servers}
    try:
        built = json.loads(zam_text)
    except Exception as exc:
        return {"ok": False, "error": "Замполит вернул не JSON: %s" % exc,
                "steps": steps, "pairs": [], "count": 0, "servers": servers}
    pairs = built.get("pairs") if isinstance(built, dict) else None
    if not isinstance(pairs, list):
        pairs = []

    # --- Шаг 3: завхоз — сохранить снимок в JSON ---
    ok, sav_text = _mcp_text(zav_srv, "save_pairs",
                             {"pairs_json": json.dumps({"pairs": pairs},
                                                       ensure_ascii=False),
                              "source": "composition"}, timeout=timeout)
    steps.append({"step": "zavhoz", "ok": ok, "detail": sav_text[:400]})
    if not ok:
        return {"ok": False, "error": "Завхоз: %s" % sav_text,
                "steps": steps, "pairs": pairs, "count": len(pairs),
                "servers": servers}
    if verbose:
        print("[COMPOSE] пар: %d; %s" % (len(pairs), sav_text), flush=True)
    return {"ok": True, "calendar": cal_text, "pairs": pairs,
            "count": len(pairs), "saved": sav_text, "steps": steps,
            "servers": servers,
            "detail": "Событий: %d, пар сохранено: %d." % (len(events), len(pairs))}


def _format_composition_message(report):
    """Формирует ОТДЕЛЬНОЕ сообщение в чат после запуска композиции.

    Содержит: заголовок «Композиция MCP запущена», список задействованных
    MCP-серверов с кратким описанием работы каждого, и краткий итог прогона.
    Возвращает готовый к показу текст (или "" если серверов нет).
    """
    report = report or {}
    servers = report.get("servers") or list(
        getattr(config, "COMPOSITION_SERVERS", []) or [])
    lines = ["**Композиция MCP запущена.** Задействованы серверы:"]
    for s in servers:
        label = s.get("label") or s.get("id") or "?"
        desc = s.get("desc") or ""
        lines.append("- **%s** — %s" % (label, desc))
    # Итог прогона (сколько событий/пар) — если есть.
    if report.get("ok"):
        detail = report.get("detail") or ""
        if detail:
            lines.append("")
            lines.append("Итог: %s" % detail)
    else:
        lines.append("")
        lines.append("Внимание, ошибка прогона: %s"
                     % (report.get("error") or "неизвестно"))
    return "\n".join(lines)


def _composition_report_text(report):
    """Готовит ТЕКСТОВУЮ сводку прогона композиции ДЛЯ МОДЕЛИ.

    Модель, выбранная в селекте «Модель MCP», получает эту сводку и
    формирует короткое сообщение в чат (_handle_mcp, action «compose»).
    Возвращает строку: состав серверов, итог и (если есть) сохранённые пары.
    """
    report = report or {}
    lines = []
    servers = report.get("servers") or list(
        getattr(config, "COMPOSITION_SERVERS", []) or [])
    if servers:
        lines.append("Задействованные MCP-серверы:")
        for s in servers:
            label = s.get("label") or s.get("id") or "?"
            desc = s.get("desc") or ""
            lines.append("  - %s — %s" % (label, desc))
    if report.get("ok"):
        lines.append("Результат: %s" % (report.get("detail") or "успешно"))
        if report.get("saved"):
            lines.append("Сохранено: %s" % report.get("saved"))
        pairs = report.get("pairs") or []
        if pairs:
            lines.append("Пары «событие + время» (до %d):" % len(pairs))
            for p in pairs[:10]:
                ev = p.get("event") or p.get("summary") or "?"
                tm = p.get("time") or ""
                mode = p.get("mode") or ""
                lines.append("  - %s → %s (%s)" % (ev, tm, mode))
    else:
        lines.append("Результат: ОШИБКА — %s" % (report.get("error") or "?"))
    return "\n".join(lines)


def _parse_calendar_events(text):
    """Разбирает текстовый вывод list_events в список {summary, start}.

    Календарь печатает блоки вида:
        События за <период>:
          UID: <uid>
          <YYYY-MM-DD HH:MM> — <summary>, до …, место: …   (с временем)
          <YYYY-MM-DD>       — <summary>, до …, место: …   (на весь день)

    Событие «НА ВЕСЬ ДЕНЬ» календарь отдаёт БЕЗ времени — только датой
    (``_localize_str`` для date-значения печатает ``"%Y-%m-%d"``). Такие
    строки распознаём отдельно и помечаем ``all_day: True`` (поле ``start`` —
    дата, поля ``time`` нет). Для событий со временем ``all_day: False``.

    Берём строку времени и название до запятой/«, до». Терпимо к формату:
    если разобрать не удалось — событие пропускается.
    """
    import re
    events = []
    # Строка события со ВРЕМЕНЕМ: "  2026-09-26 10:00 — Перекур, до ...".
    pat_timed = re.compile(
        r"(?P<start>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2})\s*—\s*(?P<summary>.+)")
    # Строка события БЕЗ времени (на весь день): "  2026-09-30 — Отпуск, ...".
    pat_allday = re.compile(
        r"(?P<start>\d{4}-\d{2}-\d{2})\s*—\s*(?P<summary>.+)")
    # Отсекаем служебные строки (заголовок «События за …», «UID: …»), иначе
    # заголовок-диапазон «… 2026-09-26 — 2026-10-03:» ложно ловится как
    # событие «на весь день».
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("UID:"):
            continue
        if re.match(r"^\s*События за\b", line) or re.match(
                r"^\s*\d{4}-\d{2}-\d{2}\s+—\s+\d{4}-\d{2}-\d{2}\s*:", line):
            continue
        m = pat_timed.search(line)
        all_day = False
        if m:
            start = m.group("start").replace("T", " ")
        else:
            m = pat_allday.search(line)
            if not m:
                continue
            start = m.group("start").strip()
            all_day = True
        summary = m.group("summary").strip()
        # Обрезаем хвост «, до …» / «, место …» — оставляем только название.
        for sep in (", до ", ", место:", ", место "):
            idx = summary.find(sep)
            if idx != -1:
                summary = summary[:idx].strip()
        # Название в кавычках-ёлочках убираем, если есть.
        summary = summary.strip().strip("«»\"'")
        events.append({"summary": summary, "start": start, "all_day": all_day})
    return events


def _board_direct_result(server_id, model=None):
    """Надёжно формирует ответ для сервера «Доска» БЕЗ выбора инструмента LLM.

    Проблема: при запросе к «Доске» через обычный MCP-путь инструмент выбирает
    МОДЕЛЬ — она может выбрать не тот инструмент (ping) или ошибиться, и вывод
    доски «падает». Здесь инструменты доски вызываются ДЕТЕРМИНИРОВАННО:

      1) ``show_board_html`` — HTML-таблица (основной вывод в чате);
      2) ``show_board``      — текстовая таблица (текстовый ответ/фолбэк).

    Возвращает готовый ``result``-dict той же формы, что и ``answer_via_mcp``:
    {ok, text, html, answers, meta, ...}. Никакой LLM для выбора инструмента
    не привлекается — это гарантирует, что доска всегда выводится таблицей.
    """
    ok_html, html_text = _mcp_text(server_id, "show_board_html", {})
    ok_txt, board_text = _mcp_text(server_id, "show_board", {})
    # Если ни один инструмент не ответил — вернём понятную ошибку.
    if not ok_html and not ok_txt:
        err = board_text or html_text or "инструмент доски недоступен"
        return {"ok": False, "error": "Доска: %s" % err, "text": "",
                "html": "", "answers": [], "meta": ""}
    # Текст для ответа — таблица show_board; если она не удалась, берём HTML.
    text = board_text if ok_txt else html_text
    html = ""
    if ok_html and "<table" in str(html_text):
        html = ('<div class="mcp-answer"><div class="mcp-answer-head">'
                'MCP-инструмент: <b>show_board_html</b>({})</div>'
                '<div class="mcp-answer-body">%s</div></div>'
                % html_text)
    else:
        # Фолбэк: оборачиваем текст в pre, как это делает агент.
        import html as _html
        html = ('<div class="mcp-answer"><div class="mcp-answer-head">'
                'MCP-инструмент: <b>show_board</b>({})</div>'
                '<div class="mcp-answer-body"><pre>%s</pre></div></div>'
                % _html.escape(str(board_text)))
    label = model or "MCP"
    return {"ok": True, "text": text, "html": html,
            "answers": [{"label": label, "model": label, "text": text}],
            "meta": "MCP: show_board"}


def _set_agent(agent):
    """Запоминает агента, который обслуживает все запросы."""
    _ServerState.agent = agent

def _ensure_session():
    """Лениво создаёт единственное хранилище сессии диалога."""
    if _ServerState.session is None:
        _ServerState.session = SessionStore()
    return _ServerState.session


class WebRequestHandler(BaseHTTPRequestHandler):
    server_version = "MultiModelChat/1.0"

    @property
    def agent(self):
        """Единый агент (rtk_app.agent.Agent), созданный при старте сервера."""
        return _ServerState.agent

    @property
    def session(self):
        """Хранилище сессии диалога (rtk_app.session_store.SessionStore)."""
        return _ensure_session()

    def _send_bytes(self, status, body, content_type):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, obj):
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, payload, "application/json; charset=utf-8")

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    # ---------------- GET ----------------
    def do_GET(self):
        import urllib.parse
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if path in ("/", "/index.html"):
            return self._serve_static_safe("index.html")
        if path.startswith(("/css/", "/js/")):
            return self._serve_static_safe(path.lstrip("/"))
        if path == "/api/model":
            return self._send_json(200, {
                "ok": True,
                "model": self.agent.label if self.agent else config.MODEL,
                "models": [m["label"] for m in
                           (self.agent.available() if self.agent else [])],
                "available": (self.agent.available() if self.agent else []),
            })
        if path == "/api/session":
            # Единый снимок состояния сессии (session.full_state()) — те же
            # поля, что и в ответе /api/ask (см. §1.4 обзора оптимизаций).
            state = {"ok": True, "has_history": self.session.has_history()}
            state.update(self.session.full_state())
            return self._send_json(200, state)
        if path == "/api/profiles":
            state = self.session.profiles_state()
            return self._send_json(200, {
                "ok": True,
                "profiles": state["profiles"],
                "active": state["active"],
            })
        if path == "/api/invariants":
            return self._send_json(200, self.session.invariants_state())
        if path == "/api/mcp":
            return self._send_json(200, _mcp_state_payload(check=False))
        self._send_json(404, {"ok": False, "error": "Not Found"})

    def do_POST(self):
        import urllib.parse
        if urllib.parse.urlparse(self.path).path == "/api/ask":
            return self._handle_ask()
        if urllib.parse.urlparse(self.path).path == "/api/newchat":
            self.session.reset()
            return self._send_json(200, {"ok": True,
                                         "messages": self.session.snapshot()})
        if urllib.parse.urlparse(self.path).path == "/api/compact":
            return self._handle_compact()
        if urllib.parse.urlparse(self.path).path == "/api/compact_summary":
            return self._handle_compact_summary()
        if urllib.parse.urlparse(self.path).path == "/api/strategy":
            return self._handle_strategy()
        if urllib.parse.urlparse(self.path).path == "/api/facts":
            return self._handle_facts()
        if urllib.parse.urlparse(self.path).path == "/api/memory":
            return self._handle_memory()
        if urllib.parse.urlparse(self.path).path == "/api/branches":
            return self._handle_branches()
        if urllib.parse.urlparse(self.path).path == "/api/task":
            return self._handle_task()
        if urllib.parse.urlparse(self.path).path == "/api/invariants":
            return self._handle_invariants()
        if urllib.parse.urlparse(self.path).path == "/api/profiles":
            return self._handle_profiles()
        if urllib.parse.urlparse(self.path).path == "/api/mcp":
            return self._handle_mcp()
        self._send_json(404, {"ok": False, "error": "Not Found"})

    # ---------------- Статика ----------------
    def _serve_static_safe(self, rel):
        root_real = os.path.realpath(config.WEB_ROOT)
        full = os.path.realpath(os.path.join(root_real, os.path.normpath(rel)))
        if not (full.startswith(root_real + os.sep) or full == root_real):
            return self._send_bytes(403, "Forbidden", "text/plain")
        if not os.path.isfile(full):
            return self._send_bytes(404, "Not Found", "text/plain")
        ctype, _ = mimetypes.guess_type(full)
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------- Обработчики: делегируют всю работу агенту ----------------

    def _maybe_auto_compact(self):
        """Инкрементальное сжатие истории: дописывает вытесненные сообщения.

        Логика: как только история становится больше keep, каждое новое
        вытесненное сообщение ДОПИСЫВАЕТСЯ в summary (Вариант A). Для keep = 5
        сжатие начинается с 6-го сообщения и продолжается по мере вытеснения.
        Summary хранится ОТДЕЛЬНО (compact.summary) и подставляется в
        следующий запрос вместо вытесненной части истории.
        """
        try:
            if not self.session.should_auto_compact():
                return
            head, end = self.session.head_to_compact()
            if not head:
                return
            keep = self.session.get_compact().get("keep", config.COMPACT_KEEP)
            prev_summary = self.session.get_compact().get("summary", "")
            # Дописываем вытесненную часть в существующее summary.
            summary = self.agent.compact_update(prev_summary, head)
            if summary:
                # Сохраняем summary и новую границу: сообщения [0:end) покрыты.
                self.session.apply_summary(summary, upto=end, keep=keep)
                print("[COMPACT] сжатие: +%d сообщ. -> summary (upto=%d)"
                      % (len(head), end), flush=True)
        except Exception as exc:
            # Сжатие не должно ломать основной запрос.
            print("[COMPACT] сжатие не удалось: %s" % exc, flush=True)

    def _handle_ask(self):
        """Принимает запрос пользователя и передаёт его агенту.

        История диалога хранится на сервере (папка session/) и подхватывается
        при каждом запуске. Сервер передаёт агенту историю, выбор моделей и
        температуру, а после успешного ответа добавляет ход в сессию и
        сохраняет её на диск.
        """
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        question = str(data.get("question", "")).strip()
        # Выбор моделей пользователем: [{"label": "…", "temperature": 0.7}]
        selected = data.get("models")
        if not isinstance(selected, list) or not selected:
            selected = None

        # «ДОСКА» — особый сервер ВЫВОДА: запрос к ней обрабатывается через
        # MCP-путь (с предварительным обновлением данных композицией) и
        # НЕЗАВИСИМО от чекбокса «Использовать MCP». Для «Доски» применяется
        # модель из селекта «модель MCP» (_ServerState.mcp_model), поэтому
        # выбранная сверху модель и персона не нужны.
        doska_srv = getattr(config, "COMPOSITION_DOSKA_SERVER", "doska")
        is_doska = (_ServerState.mcp_server or "") == doska_srv
        # РЕЖИМ КОМПОЗИЦИИ: пока композиция активна, ФОНОМ работают серверы
        # «календарь → замполит → завхоз» (отрабатывают логику и обновляют
        # данные), а ЛЮБОЕ обращение в чате ВСЕГДА идёт К «ДОСКЕ» — серверу
        # ВЫВОДА (таблица «событие + время»). Выбранный в списке сервер и
        # выбранная сверху модель/персона при этом НЕ важны: отвечает «Доска».
        composition_active = bool(_ServerState.composition_running)
        # На время композиции цель чата — «Доска» (детерминированный вывод).
        target_is_board = is_doska or composition_active
        # ЗАПРОС ИДЁТ ЧЕРЕЗ MCP, если ВКЛЮЧЁН ЧЕКБОКС «Использовать MCP»,
        # ЛИБО выбран сервер «Доска» (запрос к ней не зависит от чекбокса),
        # ЛИБО активна КОМПОЗИЦИЯ (тогда цель — всегда «Доска»).
        # ВАЖНО: сам ФАКТ выбора сервера в списке (без чекбокса и без
        # композиции) НЕ включает MCP-режим.
        via_mcp = bool(_ServerState.mcp_enabled or target_is_board)

        # РЕЖИМ ОТВЕТА зависит от наличия персон:
        #   * ЕСТЬ активная персона — отвечает ПЕРСОНА, используя СВОЮ модель
        #     (выбор моделей сверху недоступен, но если клиент всё же прислал
        #     список — модель персоны имеет приоритет);
        #   * НЕТ персон — диалог идёт напрямую с ВЫБРАННЫМИ моделями.
        # ВАЖНО: при запросе через MCP (в т.ч. к «Доске») модель берётся из
        # селекта «модель MCP», а не из персон/моделей сверху.
        pid, pname, pmodel, pchar, pstyle = self.session.active_profile_attrs()
        if pid and not via_mcp:
            # Отвечает персона. Заголовок блока — имя персоны.
            answer_title = pname or "Персона"
            profile = {"name": pname, "character": pchar, "style": pstyle}
            # Модель персоны имеет приоритет; если у персоны модель не задана —
            # используем выбранные сверху модели, а если и их нет — все
            # доступные модели агента (передаём None).
            if pmodel:
                selected = [{"label": pmodel}]
            elif selected is None:
                selected = None  # агент опросит все доступные модели
        else:
            # Персон нет (или запрос идёт через MCP) — режим «напрямую».
            answer_title = None
            profile = None
            if selected is None and not via_mcp:
                return self._send_json(200, {
                    "ok": False,
                    "error": "Не выбрана ни одна модель. Включите модель для "
                             "запроса.",
                    "text": "", "html": "", "answers": [],
                })

        # Глобальное ограничение max_tokens (None — не применяется)
        max_tokens = data.get("max_tokens")
        try:
            max_tokens = int(max_tokens)
        except (TypeError, ValueError):
            max_tokens = None

        # Настройки сжатия
        compact = data.get("compact")
        if isinstance(compact, dict):
            # Клиент может прислать свои настройки (enabled/keep) — применяем.
            self.session.set_compact(compact.get("enabled"),
                                     compact.get("keep"),
                                     None)
        # Стратегия управления контекстом (если клиент прислал).
        strategy = data.get("strategy")
        if isinstance(strategy, dict):
            self.session.set_strategy(strategy.get("strategy"),
                                      strategy.get("window"))
        compact = self.session.get_compact()

        # Управление контекстом: применяем АКТИВНУЮ стратегию
        # (sliding / facts / branch) и, поверх неё, сжатие summary.
        history = self.session.get_context_messages()

        # ПРОВЕРКА НАЛИЧИЯ данных в памяти при формировании запроса:
        # если в рабочей/долговременной памяти есть данные — они уже
        # подмешаны в контекст (memory_message) как системное сообщение.
        memory = self.session.memory_state()
        mem_items = (len(memory.get("working") or {})
                     + len(memory.get("longterm") or {}))
        if mem_items:
            print("[MEMORY] в запрос добавлена память: рабочая=%d, "
                  "долговременная=%d (всего %d элементов)"
                  % (len(memory.get("working") or {}),
                     len(memory.get("longterm") or {}), mem_items),
                  flush=True)
        else:
            print("[MEMORY] память пуста — в запрос не добавляется", flush=True)

        # Профиль (персона): характер и характер ответов активного профиля
        # подставляются в системный промпт каждой модели (тон + формат/длина).
        # pid/pname/... и profile уже получены выше (см. выбор режима).

        # СОСТОЯНИЕ ЗАДАЧИ (Task State Machine): формализованный автомат
        # «этап -> шаг -> ожидаемое действие». Передаём его агенту, чтобы он
        # вёл ответ сообразно этапу и после паузы продолжал без повторных
        # объяснений. Если активной задачи нет — None.
        task_state = self.session.get_task_state()
        if not task_state.get("active"):
            task_state = None

        # ИНВАРИАНТЫ: жёсткие правила (архитектура/техрешения/стек/бизнес),
        # которые ассистент не вправе нарушать. Передаём их агенту: он учтёт
        # правила в промпте и выполнит пост-проверку ответа.
        invariants = self.session.get_invariants()
        if invariants:
            print("[INVARIANT] инвариантов в запросе: %d" % len(invariants),
                  flush=True)

        # НАПОМИНАНИЯ: АВТОМАТИЧЕСКИ забираем наступившие напоминания из
        # календаря (MCP-инструмент run_due) и доставляем их в чат ОТДЕЛЬНЫМ
        # сообщением (красный фон) — независимо от того, что спросил
        # пользователь. Так напоминание приходит САМО, без специального
        # запроса «покажи напоминания».
        reminders = _collect_due_reminders()

        # ВЕТКА MCP: запрос идёт ЧЕРЕЗ MCP, если MCP включён в интерфейсе ЛИБО
        # выбран сервер «Доска» (запрос к ней не зависит от чекбокса).
        # В этой ветке модель выбирает инструмент, инструмент вызывается на
        # MCP-сервере, его результат возвращается как ответ.
        if via_mcp:
            print("[MCP] запрос идёт через MCP (server=%r, model=%r, "
                  "board=%s, composition=%s)"
                  % (_ServerState.mcp_server or "default",
                     _ServerState.mcp_model or "auto",
                     target_is_board, composition_active), flush=True)
            # ЦЕЛЬ ЗАПРОСА — СЕРВЕР ВЫВОДА «ДОСКА»: так при активной
            # композиции ЛЮБОЕ обращение в чате всегда выводит таблицу
            # «событие + время», независимо от выбранного в списке сервера.
            # Перед выдачей таблицы ОБНОВЛЯЕМ данные до актуального состояния,
            # прогнав композицию заново (календарь -> замполит -> завхоз):
            # так на доске всегда свежие события, даже если фоновый тик ещё
            # не успел их обновить.
            if target_is_board:
                print("[MCP] запрос к «Доске»: обновляю данные композицией…",
                      flush=True)
                try:
                    refresh = _composition_run(verbose=False)
                    if not refresh.get("ok"):
                        print("[MCP] обновление «Доски» не удалось: %s"
                              % refresh.get("error"), flush=True)
                except Exception as exc:
                    print("[MCP] обновление «Доски» не удалось: %s" % exc,
                          flush=True)
                # «ДОСКА» — сервер ВЫВОДА: инструмент показываем ДЕТЕРМИНИРОВАННО
                # (show_board_html + show_board), НЕ доверяя выбор инструмента
                # модели. Это исключает «ошибку доски», когда модель выбирала
                # ping/неверный инструмент.
                result = _board_direct_result(
                    doska_srv, model=(_ServerState.mcp_model or None))
                if result.get("ok"):
                    self.session.append_turn(question, {
                        "role": "assistant",
                        "content": result.get("text", ""),
                        "html": result.get("html", ""),
                        "answers": result.get("answers", []),
                        "meta": result.get("meta", ""),
                        "usage": result.get("usage"),
                    })
                    result.update(self.session.full_state())
                if reminders:
                    result["reminders"] = reminders
                return self._send_json(200, result)
            tools = []
            if mcp_client is not None:
                listed = mcp_client.mcp_list_tools(
                    server_id=(_ServerState.mcp_server or None))
                if listed.get("ok"):
                    tools = listed.get("tools", [])
                else:
                    print("[MCP] не удалось получить список инструментов: %s"
                          % listed.get("error"), flush=True)
            # Модель для MCP-запроса — из селекта «модель MCP».
            result = self.agent.answer_via_mcp(
                question, tools,
                model=(_ServerState.mcp_model or None),
                server_id=(_ServerState.mcp_server or None))
        else:
            result = self.agent.answer(question, history, selected,
                                       max_tokens=max_tokens,
                                       compact=compact,
                                       memory=memory,
                                       profile=profile,
                                       answer_title=answer_title,
                                       task_state=task_state,
                                       invariants=invariants)
        if result.get("ok"):
            # По одному ходу на ответ модели с уже готовой разметкой
            self.session.append_turn(question, {
                "role": "assistant",
                "content": result.get("text", ""),
                "html": result.get("html", ""),
                "answers": result.get("answers", []),
                "meta": result.get("meta", ""),
                "usage": result.get("usage"),
            })
            # Стратегия Facts: обновляем блок facts после каждого сообщения
            # пользователя (через GigaChat).
            self._maybe_update_facts(question, result)
            # Счётчики использования памяти: сколько фрагментов ответа
            # заимствовано из рабочей/долговременной памяти (для интерфейса).
            mused = result.get("memory_used") or {}
            self.session.add_memory_usage(mused.get("working", 0),
                                          mused.get("longterm", 0))
            # Сжатие: если появились вытесненные сообщения — дописываем их
            # в summary (инкрементально; работает только при keep > 0).
            self._maybe_auto_compact()
            # СОСТОЯНИЕ ЗАДАЧИ: если задача активна (не на паузе и не done) —
            # пусть агент предложит следующий переход автомата, а сервер
            # применит его ТОЛЬКО если переход корректен (проверка в
            # SessionStore.advance_task). Паузу и завершение клиент задаёт сам.
            self._maybe_advance_task(question, result)
            # Единый снимок состояния сессии (те же поля, что и /api/session).
            result.update(self.session.full_state())
        # Доставляем наступившие напоминания в чат (красным фоном) в ЛЮБОМ
        # случае — даже если ответ модели не удался, напоминание важно.
        if reminders:
            result["reminders"] = reminders
            # В историю сессии НЕ пишем: это разовая доставка, а не ход
            # диалога (иначе напоминания дублировались бы при перезагрузке).
        return self._send_json(200, result)

    def _maybe_advance_task(self, question, result):
        """Продвигает автомат задачи по ходу пользователя (если задача активна).

        Решение о переходе принимает агент (LLM на GigaChat), а сервер
        применяет его только при КОРРЕКТНОСТИ перехода. Задача на паузе не
        двигается — продолжение инициирует пользователь кнопкой «Продолжить».
        """
        try:
            ts = self.session.get_task_state()
            if not ts.get("active") or ts.get("paused") or ts.get("stage") == "done":
                return
            move = self.agent.advance_task(
                ts, question, result.get("text", ""))
            if not move or not move.get("stage"):
                return
            res = self.session.advance_task(
                stage=move.get("stage"),
                step=move.get("step"),
                expected=move.get("expected"),
                note=move.get("note") or "авто-переход по ходу диалога")
            if res.get("ok"):
                print("[TASK] этап -> %s (шаг: %s)"
                      % (res["task"].get("stage"), res["task"].get("step")),
                      flush=True)
            else:
                print("[TASK] переход отклонён: %s" % res.get("error"),
                      flush=True)
        except Exception as exc:
            print("[TASK] авто-переход не удался: %s" % exc, flush=True)

    def _maybe_update_facts(self, question, result):
        """Обновляет блок facts после хода пользователя (стратегия Facts)."""
        try:
            if self.session.get_strategy().get("strategy") != "facts":
                return
            prev = self.session.get_facts()
            last_turn = [
                {"role": "user", "content": str(question)},
                {"role": "assistant", "content": result.get("text", "")},
            ]
            facts = self.agent.update_facts(prev, last_turn)
            if isinstance(facts, dict):
                self.session.set_facts(facts)
        except Exception as exc:
            print("[FACTS] обновление не удалось: %s" % exc, flush=True)

    def _handle_compact(self):
        """Обновляет настройки сжатия сессии (enabled/keep/summary)."""
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        enabled = data.get("enabled")
        keep = data.get("keep")
        summary = data.get("summary")
        # Пустой summary из клиента НЕ должен затирать уже сгенерированный
        # на сервере — иначе настройки каждый раз обнуляют сжатие.
        if not summary:
            summary = None
        self.session.set_compact(enabled, keep, summary)
        return self._send_json(200, {
            "ok": True,
            "compact": self.session.get_compact(),
        })

    def _handle_compact_summary(self):
        """Дописывает вытесненную часть истории в summary (по запросу).

        Сжимаем только то, что вышло за пределы последних keep сообщений,
        и ДОПИСЫВАЕМ это в существующее summary (инкрементально, Вариант A).
        Summary сохраняется отдельно и будет подставлено в следующий запрос
        вместо вытесненной части истории.
        """
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        keep = data.get("keep", config.COMPACT_KEEP)
        try:
            keep = max(0, int(keep))
        except (TypeError, ValueError):
            keep = config.COMPACT_KEEP
        # Применяем актуальный keep перед вычислением вытесняемой части.
        self.session.set_compact(True, keep, None)
        head, end = self.session.head_to_compact()
        if not head:
            return self._send_json(200, {
                "ok": True, "summary": self.session.get_compact().get("summary", ""),
                "detail": "Нет вытесненных сообщений — сжимать нечего.",
            })
        prev_summary = self.session.get_compact().get("summary", "")
        summary = self.agent.compact_update(prev_summary, head)
        if summary:
            self.session.apply_summary(summary, upto=end, keep=keep)
            return self._send_json(200, {
                "ok": True, "summary": summary,
                "detail": "Summary дополнен (%d сообщ.)." % len(head),
            })
        return self._send_json(200, {
            "ok": False, "summary": self.session.get_compact().get("summary", ""),
            "detail": "Не удалось обновить summary.",
        })

    def _handle_strategy(self):
        """Управление стратегией контекста: none / sliding / facts / branch."""
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        state = self.session.set_strategy(data.get("strategy"),
                                          data.get("window"))
        return self._send_json(200, {
            "ok": True,
            "strategy": state,
            "context": self.session.context_stats(),
            "facts": self.session.get_facts(),
            "branches": self.session.branches_state(),
        })

    def _handle_facts(self):
        """Просмотр/редактирование фактов (key-value) стратегии Facts.

        Факты — это данные ПАМЯТИ агента: каждый факт раскладывается в
        ВЫБРАННЫЙ пользователем слой памяти: mem_map = {ключ: "working"|
        "longterm"}. Ключи без явного выбора считаются рабочей памятью.
        Отдельного хранилища facts нет — читать/писать их можно через память.
        """
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        facts = data.get("facts")
        if isinstance(facts, dict):
            facts = {str(k): str(v) for k, v in facts.items()}
            mem_map = data.get("mem_map")
            if not isinstance(mem_map, dict):
                mem_map = {}
            # Раскладываем факты по слоям памяти согласно выбору у значения.
            working, longterm = {}, {}
            for k, v in facts.items():
                layer = str(mem_map.get(k, "working")).strip().lower()
                if layer == "longterm":
                    longterm[k] = v
                else:
                    working[k] = v
            try:
                self.session.set_memory_bulk("working", working)
                self.session.set_memory_bulk("longterm", longterm)
            except ValueError as exc:
                return self._send_json(400, {"ok": False, "error": str(exc)})
        return self._send_json(200, {
            "ok": True,
            "facts": self.session.get_facts(),
            "memory": self.session.memory_state(),
        })

    def _handle_memory(self):
        """Память агента: чтение/запись трёх типов (short/working/longterm).

        Тело запроса (все поля необязательны, но action определяет смысл):
            action: "state"  — вернуть снимок всех типов (по умолчанию);
                    "set"    — записать одну пару: {type, key, value};
                    "delete" — удалить ключ: {type, key};
                    "replace"— заменить тип целиком: {type, data:{…}};
                    "clear"  — очистить тип: {type}.
            type:   "working" | "longterm" (для "short" запись запрещена —
                    краткосрочная память ведётся самим диалогом).

        Именно здесь реализован ЯВНЫЙ выбор «что и куда сохраняется» (задание
        B2): тип памяти задаётся вызывающим кодом, слои не пересекаются.
        """
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        action = str(data.get("action", "state")).strip().lower()
        mem_type = data.get("type")

        try:
            if action == "set":
                entry = self.session.set_memory_key(
                    mem_type, data.get("key"), data.get("value"))
                return self._send_json(200, {
                    "ok": True, "saved": entry,
                    "memory": self.session.memory_state(),
                })
            if action == "delete":
                removed = self.session.delete_memory_key(mem_type,
                                                         data.get("key"))
                return self._send_json(200, {
                    "ok": True, "removed": removed,
                    "memory": self.session.memory_state(),
                })
            if action == "replace":
                saved = self.session.set_memory_bulk(mem_type, data.get("data"))
                return self._send_json(200, {
                    "ok": True, "saved": saved,
                    "memory": self.session.memory_state(),
                })
            if action == "clear":
                mt = str(mem_type or "").strip()
                if mt not in config.MEMORY_TYPES:
                    return self._send_json(400, {
                        "ok": False,
                        "error": "Неизвестный тип памяти: %r" % (mem_type,),
                    })
                if mt == "short":
                    return self._send_json(400, {
                        "ok": False,
                        "error": "Краткосрочная память (диалог) очищается "
                                 "кнопкой «Новый разговор».",
                    })
                self.session.set_memory_bulk(mt, {})
                return self._send_json(200, {
                    "ok": True, "memory": self.session.memory_state(),
                })
            # action == "state" и всё прочее — отдаём снимок.
            return self._send_json(200, {
                "ok": True, "memory": self.session.memory_state(),
            })
        except ValueError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            return self._send_json(500, {"ok": False,
                                         "error": "Ошибка памяти: %s" % exc})

    def _handle_branches(self):
        """Управление ветками: create / switch / delete / rename.

        Ожидаемые поля: action = "create" | "switch" | "delete" | "rename",
        name (для create/rename), count (для create),
        index (для switch/delete/rename).
        """
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        action = str(data.get("action", "")).strip().lower()
        if action == "create":
            state = self.session.create_branch(
                name=data.get("name"),
                count=data.get("count"),
                from_checkpoint=bool(data.get("from_checkpoint", True)))
        elif action == "switch":
            state = self.session.switch_branch(data.get("index"))
        elif action == "delete":
            state = self.session.delete_branch(data.get("index"))
        elif action == "rename":
            state = self.session.rename_branch(data.get("index"),
                                               data.get("name"))
        else:
            return self._send_json(400, {
                "ok": False,
                "error": "Неизвестное действие с ветками (action).",
            })
        return self._send_json(200, {
            "ok": True,
            "branches": state,
            "messages": self.session.snapshot(),
        })

    def _handle_task(self):
        """Управление СОСТОЯНИЕМ ЗАДАЧИ (Task State Machine).

        Задача — конечный автомат: этап (planning -> execution -> validation
        -> done) + текущий шаг + ожидаемое действие. Поддерживает ПАУЗУ на
        любом этапе и ПРОДОЛЖЕНИЕ без повторных объяснений (цель и журнал
        сохраняются).

        Ожидаемые поля: action =
            "start"   -> {goal, step?, expected?}   завести новую задачу;
            "advance" -> {stage?, step?, expected?, note?}  корректный переход;
            "pause"   -> {note?}                     поставить на паузу;
            "resume"  -> {note?}                     снять с паузы (продолжить);
            "finish"  -> {note?}                     завершить (из validation);
            "reset"                                  сбросить состояние;
            "state"                                  вернуть снимок (по умолч.).
        """
        data = self._read_json_body()
        if not data:
            # GET-подобный вызов (без тела) — просто отдаём состояние.
            return self._send_json(200, {
                "ok": True, "task": self.session.get_task_state()})
        action = str(data.get("action", "state")).strip().lower()
        try:
            if action == "start":
                task = self.session.start_task(
                    data.get("goal"), step=data.get("step", ""),
                    expected=data.get("expected", ""),
                    stage=data.get("stage", "planning"))
                return self._send_json(200, {"ok": True, "task": task})
            if action == "advance":
                res = self.session.advance_task(
                    stage=data.get("stage"), step=data.get("step"),
                    expected=data.get("expected"), note=data.get("note", ""))
                status = 200 if res.get("ok") else 409
                return self._send_json(status, {
                    "ok": res.get("ok"), "task": res.get("task"),
                    "error": res.get("error")})
            if action == "pause":
                res = self.session.pause_task(data.get("note", ""))
                return self._send_json(200, {"ok": res.get("ok"),
                                             "task": res.get("task")})
            if action == "resume":
                res = self.session.resume_task(data.get("note", ""))
                return self._send_json(200, {"ok": res.get("ok"),
                                             "task": res.get("task")})
            if action == "finish":
                res = self.session.finish_task(data.get("note", ""))
                status = 200 if res.get("ok") else 409
                return self._send_json(status, {
                    "ok": res.get("ok"), "task": res.get("task"),
                    "error": res.get("error")})
            if action == "reset":
                task = self.session.reset_task()
                return self._send_json(200, {"ok": True, "task": task})
            # action == "state" и всё прочее.
            return self._send_json(200, {
                "ok": True, "task": self.session.get_task_state()})
        except ValueError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            return self._send_json(500, {
                "ok": False, "error": "Ошибка состояния задачи: %s" % exc})

    def _handle_profiles(self):
        """Управление ПРОФИЛЯМИ (персонами).

        Профиль — именованная персона со СВОЕЙ памятью (рабочая +
        долговременная), своим диалогом и настройками. Атрибуты character
        (характер — тон общения) и style (характер ответов — формат/длина)
        задаются ПРИ СОЗДАНИИ и подставляются в системный промпт каждой
        модели. Переключение профиля заменяет активную память и диалог.

        Ожидаемые поля: action = "create" | "switch" | "update" | "delete";
        id (для switch/update/delete); name, character, style, model (для
        create/update). model — метка модели, которой отвечает персона.
        """
        data = self._read_json_body()
        if not data:
            return self._send_json(400, {"ok": False, "error": "Bad JSON."})
        action = str(data.get("action", "list")).strip().lower()
        try:
            if action == "create":
                state = self.session.create_profile(
                    data.get("name"),
                    character=data.get("character"),
                    style=data.get("style"),
                    model=data.get("model"),
                    activate=bool(data.get("activate", True)))
            elif action == "switch":
                state = self.session.switch_profile(data.get("id"))
            elif action == "update":
                state = self.session.update_profile(
                    data.get("id"),
                    name=data.get("name"),
                    character=data.get("character"),
                    style=data.get("style"),
                    model=data.get("model"))
            elif action == "delete":
                state = self.session.delete_profile(data.get("id"))
            elif action in ("list", "state", ""):
                state = self.session.profiles_state()
            else:
                return self._send_json(400, {
                    "ok": False,
                    "error": "Неизвестное действие с профилями (action).",
                })
        except ValueError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})
        # После переключения/удаления активным может стать другой профиль —
        # возвращаем актуальные память и диалог, чтобы интерфейс обновился.
        return self._send_json(200, {
            "ok": True,
            "profiles": state.get("profiles", []),
            "active": state.get("active"),
            "messages": self.session.snapshot(),
            "memory": self.session.memory_state(),
            "branches": self.session.branches_state(),
            "compact": self.session.get_compact(),
            "strategy": self.session.get_strategy(),
            "context": self.session.context_stats(),
        })

    def _handle_invariants(self):
        """Управление ИНВАРИАНТАМИ (правилами, которые нельзя нарушать).

        Инварианты хранятся ОТДЕЛЬНО от диалога (свой раздел профиля) и имеют
        КАТЕГОРИЮ (архитектура / техрешения / стек / бизнес-правила). При
        конфликте запроса с инвариантом ассистент отказывается от решения.

        Ожидаемые поля: action =
            "add"     -> {text, category}            добавить инвариант;
            "update"  -> {id, text?, category?}      изменить инвариант;
            "delete"  -> {id}                         удалить инвариант;
            "replace" -> {invariants:[{text,category,id?}, …]}  заменить весь список;
            "clear"                                   очистить список;
            "state"                                   вернуть снимок (по умолч.).
        """
        data = self._read_json_body() or {}
        action = str(data.get("action", "state")).strip().lower()
        try:
            if action == "add":
                state = self.session.add_invariant(
                    data.get("text"), data.get("category", "business"))
            elif action == "update":
                state = self.session.update_invariant(
                    data.get("id"), text=data.get("text"),
                    category=data.get("category"))
            elif action == "delete":
                removed = self.session.delete_invariant(data.get("id"))
                state = self.session.invariants_state()
                state["removed"] = removed
            elif action == "replace":
                state = self.session.set_invariants(data.get("invariants"))
            elif action == "clear":
                state = self.session.clear_invariants()
            else:  # "state" и всё прочее
                state = self.session.invariants_state()
        except ValueError as exc:
            return self._send_json(400, {"ok": False, "error": str(exc)})
        except Exception as exc:
            return self._send_json(500, {
                "ok": False, "error": "Ошибка инвариантов: %s" % exc})
        return self._send_json(200, state)

    def _handle_mcp(self):
        """Управление MCP: чтение/запись настроек и проверка статуса сервера.

        Тело запроса (все поля необязательны, action определяет смысл):
            action: "state"  — вернуть состояние MCP (по умолчанию);
                    "set"    — сохранить настройки {enabled?, model?};
                    "status" — ПРОВЕРИТЬ статус MCP-сервера: подключиться,
                               получить список инструментов и вернуть
                               результат (для кнопки «Проверить статус»).
            enabled — bool: включать ли использование инструментов MCP;
            model   — метка модели, применяемой при работе с MCP.
        """
        data = self._read_json_body() or {}
        action = str(data.get("action", "state")).strip().lower()

        if action == "set":
            with _ServerState.mcp_lock:
                # ВКЛЮЧЕНИЕ чекбокса «Использовать MCP» — ОСТАНАВЛИВАЕТ
                # композицию и переводит сервер в режим отдельных серверов
                # по выбору (композиция и MCP взаимоисключающие).
                if "enabled" in data and bool(data.get("enabled")):
                    if _ServerState.composition_running:
                        _ServerState.composition_running = False
                        _stop_composition_tick()
                        print("[MCP] включён режим MCP — композиция остановлена.",
                              flush=True)
                if "enabled" in data:
                    _ServerState.mcp_enabled = bool(data.get("enabled"))
                if "model" in data:
                    _ServerState.mcp_model = str(data.get("model") or "")
                if "server" in data:
                    _ServerState.mcp_server = str(data.get("server") or "")
                _save_mcp_settings()
            return self._send_json(200, _mcp_state_payload(check=False))

        if action == "status":
            return self._send_json(200, _mcp_state_payload(check=True))

        if action == "compose":
            # КНОПКА «Запустить композицию»: прогоняем пайплайн
            # календарь -> замполит -> завхоз и возвращаем отчёт.
            #
            # ВАЖНО: композиция и чекбокс «Использовать MCP» —
            # ВЗАИМОИСКЛЮЧАЮЩИЕ. Запуск композиции:
            #   * ВЫКЛЮЧАЕТ чекбокс «Использовать MCP» (mcp_enabled = False);
            #   * включает РЕЖИМ КОМПОЗИЦИИ (composition_running = True) —
            #     композиция работает САМА ПО СЕБЕ (фоновый тик);
            #   * при этом ЗАПРОСЫ в чат обслуживает МОДЕЛЬ из селекта
            #     «Модель MCP» через MCP-путь (см. _handle_ask: при активной
            #     композиции via_mcp = True, модель берётся из mcp_model).
            with _ServerState.mcp_lock:
                _ServerState.mcp_enabled = False
                _ServerState.composition_running = True
                _save_mcp_settings()
            # Запускаем фоновый тик композиции (если ещё не запущен).
            _start_composition_tick()
            # МОДЕЛЬ: сообщение о запуске формирует модель из селекта
            # «Модель MCP» (_ServerState.mcp_model). Если модель не задана —
            # берётся шаблон.
            report = _composition_run(verbose=True)
            payload = _mcp_state_payload(check=False)
            payload["composition"] = report
            # Отдельное СООБЩЕНИЕ для чата: какие MCP-серверы задействованы
            # и что каждый делает (название + краткое описание). Текст генерит
            # выбранная модель, шаблон — фолбэк.
            template = _format_composition_message(report)
            chooser = getattr(self, "agent", None)
            model_label = _ServerState.mcp_model or ""
            if chooser is not None and hasattr(chooser, "compose_message"):
                gen = chooser.compose_message(
                    _composition_report_text(report), model=model_label or None,
                    fallback=template)
                payload["composition_message"] = gen.get("text") or template
                payload["composition_message_model"] = gen.get("model") or ""
                payload["composition_message_ok"] = bool(gen.get("ok"))
            else:
                payload["composition_message"] = template
                payload["composition_message_model"] = ""
                payload["composition_message_ok"] = False
            print("[COMPOSE] композиция запущена (MCP выключен); сообщение "
                  "сформировано моделью %r (ok=%s)"
                  % (payload.get("composition_message_model") or "шаблон",
                     payload.get("composition_message_ok")), flush=True)
            return self._send_json(200, payload)

        if action == "compose_stop":
            # Явная ОСТАНОВКА режима композиции (возврат к отдельным серверам
            # по выбору). Обычно вызывается автоматически при включении
            # чекбокса «Использовать MCP», но можно и напрямую.
            with _ServerState.mcp_lock:
                _ServerState.composition_running = False
                _stop_composition_tick()
                _save_mcp_settings()
            print("[COMPOSE] режим композиции остановлен.", flush=True)
            return self._send_json(200, _mcp_state_payload(check=False))

        # action == "state" и всё прочее — отдаём состояние без проверки.
        return self._send_json(200, _mcp_state_payload(check=False))


def create_server(agent, host=None, port=None):
    """Создаёт HTTP-сервер, связанный с конкретным экземпляром агента."""
    if agent is None:
        raise ValueError("create_server: требуется экземпляр агента (Agent).")
    _set_agent(agent)
    # ВАЖНО: проверяем именно None, а не «ложность»: port=0 — валидное
    # значение (ОС сама выберет свободный порт), которое нельзя подменять
    # значением по умолчанию, иначе тесты/инстансы будут конфликтовать
    # на общем порту config.WEB_PORT.
    addr = (host or config.WEB_HOST,
            config.WEB_PORT if port is None else port)
    return ThreadingHTTPServer(addr, WebRequestHandler)


def _open_browser_later(url, delay=1.0):
    def _job():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception as exc:
            print("[WEB] браузер: %s" % exc)
    threading.Thread(target=_job, daemon=True).start()


def _composition_tick_loop(interval, stop_event):
    """Фоновый ТИК композиции: раз в interval секунд прогоняет пайплайн.

    По задаче (docs/task5.md) «календарь проверяет каждую минуту состояние
    событий на ближайшие 7 дней». Тик работает ТОЛЬКО в режиме композиции:
    его останавливают (stop_event) при включении чекбокса «Использовать MCP».
    Ошибки логируются и НЕ останавливают цикл.
    """
    print("[COMPOSE] фоновый тик композиции: каждые %d c." % interval, flush=True)
    while not stop_event.is_set():
        # Ждём интервал, но просыпаемся сразу при остановке.
        if stop_event.wait(interval):
            break
        if not _ServerState.composition_running:
            break
        try:
            report = _composition_run(verbose=False)
            if not report.get("ok"):
                print("[COMPOSE] тик: %s" % report.get("error"), flush=True)
        except Exception as exc:
            print("[COMPOSE] тик не удался: %s" % exc, flush=True)
    print("[COMPOSE] фоновый тик композиции остановлен.", flush=True)


def _start_composition_tick():
    """Запускает фоновый тик композиции (режим композиции ВКЛючён).

    Если тик уже запущен — ничего не делает. Тик перестаёт работать при
    выключении режима композиции (см. _stop_composition_tick).
    """
    interval = int(getattr(config, "COMPOSITION_TICK_SECONDS", 0) or 0)
    if interval <= 0:
        print("[COMPOSE] фоновый тик выключен (COMPOSITION_TICK_SECONDS=0).",
              flush=True)
        return
    if _ServerState.composition_thread and _ServerState.composition_thread.is_alive():
        return
    stop_event = threading.Event()
    _ServerState.composition_stop = stop_event
    th = threading.Thread(target=_composition_tick_loop,
                          args=(interval, stop_event), daemon=True)
    _ServerState.composition_thread = th
    th.start()


def _stop_composition_tick():
    """Останавливает фоновый тик композиции (режим композиции ВЫКлючён)."""
    stop_event = _ServerState.composition_stop
    if stop_event is not None:
        stop_event.set()
    _ServerState.composition_stop = None
    _ServerState.composition_thread = None


def serve(agent_or_key, host=None, port=None, open_page=True, agent=None):
    """Запускает сервер, используя агента в качестве единой сущности.

    agent_or_key — либо строка API-ключа DeepSeek (тогда создаётся агент),
    либо уже готовый экземпляр rtk_app.agent.Agent.
    agent — ещё один способ передать готового агента явно.
    """
    if agent is None:
        agent = agent_or_key

    # Загружаем сохранённые настройки MCP (вкл/выкл + модель).
    _load_mcp_settings()

    # Агент — отдельная сущность, построенная вокруг ключа либо переданная.
    if not isinstance(agent, Agent):
        agent = Agent(agent)

    httpd = create_server(agent, host, port)
    shown_host, shown_port = httpd.server_address[:2]
    url = "http://%s:%s/" % (shown_host, shown_port)
    print("[WEB] Сервер запущен: %s" % url)
    print("[WEB] Агент обслуживает модели: %s" % agent.label)
    # Фоновый ТИК композиции запускаем ТОЛЬКО если режим композиции был
    # сохранён как активный (взаимоисключает чекбокс «Использовать MCP»).
    if _ServerState.composition_running and not _ServerState.mcp_enabled:
        print("[COMPOSE] восстановлен режим композиции из настроек.", flush=True)
        _start_composition_tick()
    else:
        _ServerState.composition_running = False
        print("[COMPOSE] режим композиции не активен (работают отдельные "
              "серверы по выбору).", flush=True)
    if open_page:
        _open_browser_later(url)
    print("[WEB] Остановка: нажмите Ctrl+C.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[WEB] Остановка...")
    finally:
        _stop_composition_tick()
        httpd.server_close()


def main():
    import sys
    from rtk_app.key_store import read_api_key
    try:
        api_key = read_api_key(config.DS_KEY_FILE)
        print("[OK] Ключ DeepSeek прочитан из %s" % config.DS_KEY_FILE)
    except FileNotFoundError:
        # Файла ключа нет: сервер всё равно стартует — интерфейс откроется,
        # а недоступные модели покажут подсказку, как добавить ключ
        # (самодиагностика уже напечатана через rtk_web.py/key_check).
        api_key = ""
        print("[!] Файл %s не найден. DeepSeek-flash в чате станет "
              "недоступен до тех пор, пока вы не впишете ключ в apidpsk.txt."
              % config.DS_KEY_FILE)
    args = sys.argv[1:]
    host, port = config.WEB_HOST, config.WEB_PORT
    if args and args[0].isdigit():
        port = int(args[0])
        if len(args) > 1:
            host = args[1]
    sys.exit(serve(api_key, host, port) or 0)