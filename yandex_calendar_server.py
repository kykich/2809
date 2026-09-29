"""MCP-сервер Яндекс.Календаря (CalDAV).

Отдельный MCP-сервер проекта: предоставляет инструменты для работы с
Яндекс.Календарём по протоколу CalDAV (https://caldav.yandex.ru/):
  * list_calendars — список календарей пользователя;
  * list_events    — события за период (по датам);
  * find_event     — найти события по названию/дате/месту (без UID);
  * create_event   — создать событие;
  * update_event   — изменить событие (по UID ИЛИ по названию/дате);
  * delete_event   — удалить событие (по UID ИЛИ по названию/дате).

ДОПОЛНИТЕЛЬНО (см. docs/task4.md) — РАБОТА ПО РАСПИСАНИЮ как НАПОМИНАЛКА
до события (напомнить за N минут/часов/дней до начала события, с повтором):
  * schedule_reminder  — напоминание к событию (по UID ИЛИ словами; можно
                          задать периодичность повторения — раз в N минут/часов);
  * list_reminders      — список напоминаний;
  * cancel_reminder     — отменить напоминание по id;
  * due_reminders        — наступившие напоминания (что озвучить сейчас);
  * run_due              — исполнить/собрать наступившие (ленивый и явный).

Учётные данные берутся из файла ya.txt (первая строка — логин/email,
вторая — пароль приложения с доступом к календарю). Файл НЕ коммитится.

Запуск (как stdio-подпроцесс MCP):
    python yandex_calendar_server.py

Переключение проекта на этот сервер — в rtk_app/config.py (MCP_SERVERS).
"""

import datetime as _dt
import os
import sys
import time

from mcp.server.mcpserver import MCPServer

# BASE_DIR — папка проекта (рядом с config.py); учитываем и запуск из др. CWD.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Хранилище заданий расписания — общий модуль проекта (rtk_app/jobs_store.py).
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
from rtk_app.jobs_store import (JobsStore, now_str, parse_time, TIME_FMT)

mcp = MCPServer("yandex-calendar")


# --------------------------------------------------------------------------
# Учётные данные и подключение
# --------------------------------------------------------------------------
def _creds():
    """Читает логин/пароль из ya.txt. Формат: строка 1 — логин, строка 2 — пароль."""
    path = os.environ.get("YA_CRED_FILE") or os.path.join(BASE_DIR, "ya.txt")
    if not os.path.isfile(path):
        raise RuntimeError("не найден файл учётных данных Яндекс.Календаря: %s" % path)
    with open(path, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
    if len(lines) < 2:
        raise RuntimeError("ya.txt должен содержать две строки: логин и пароль приложения")
    return lines[0], lines[1]


def _client():
    """Создаёт CalDAV-клиент Яндекс.Календаря."""
    import caldav
    login, password = _creds()
    url = os.environ.get("YA_CALDAV_URL") or "https://caldav.yandex.ru/"
    return caldav.DAVClient(url=url, username=login, password=password)


def _main_calendar(client):
    """Возвращает основной (первый) календарь пользователя."""
    cals = list(client.principal().calendars())
    if not cals:
        raise RuntimeError("у пользователя нет доступных календарей")
    return cals[0]


def _parse_dt(value, default_date=None):
    """Разбирает дату/время из строки.

    Поддерживаются форматы:
      * "YYYY-MM-DD"                    — дата (без времени);
      * "YYYY-MM-DD HH:MM"             — дата и время;
      * "YYYY-MM-DDTHH:MM"             — ISO;
      * "YYYY-MM-DD HH:MM:SS"          — с секундами.
    Возвращает datetime. Если время не задано, берётся 00:00.
    """
    if value is None or value == "":
        return default_date
    s = str(value).strip().replace("T", " ")
    fmts = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]
    for fmt in fmts:
        try:
            return _dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError("не удалось разобрать дату/время: %r" % value)


def _is_date_only(value):
    """True, если строка задаёт только дату (без времени)."""
    if value is None or value == "":
        return False
    s = str(value).strip().replace("T", " ")
    return len(s) == 10   # формат YYYY-MM-DD — ровно 10 символов


def _end_of_day(dt_value):
    """Конец суток для переданной даты (23:59:59)."""
    return dt_value.replace(hour=23, minute=59, second=59, microsecond=0)


def _tzinfo():
    """Часовой пояс для отображения/создания событий (по умолчанию Europe/Moscow)."""
    name = os.environ.get("CALENDAR_TZ") or _default_tz_name()
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return _dt.timezone(_dt.timedelta(hours=3))   # запасной вариант — MSK


def _default_tz_name():
    """Читает CALENDAR_TZ из config (без жёсткой зависимости от rtk_app)."""
    try:
        from rtk_app import config
        return getattr(config, "CALENDAR_TZ", "Europe/Moscow")
    except Exception:
        return "Europe/Moscow"


def _localize_str(value):
    """Приводит дату/время к строке в локальном поясе (Europe/Moscow)."""
    if value is None:
        return None
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_tzinfo())
        return value.astimezone(_tzinfo()).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, _dt.date):
        return value.strftime("%Y-%m-%d")
    return str(value)


def _fmt_event(comp=None, ev=None):
    """Формирует краткое текстовое описание события (VEVENT)."""
    summary = str(comp.get("summary") or "") if comp is not None else ""
    uid = str(comp.get("uid") or "") if comp is not None else ""
    dtstart = comp.get("dtstart").dt if (comp is not None and comp.get("dtstart")) else None
    dtend = comp.get("dtend").dt if (comp is not None and comp.get("dtend")) else None
    desc = str(comp.get("description") or "") if comp is not None else ""
    loc = str(comp.get("location") or "") if comp is not None else ""
    dtstart = _localize_str(dtstart)
    dtend = _localize_str(dtend)
    bits = ["%s — %s" % (dtstart, summary)]
    if dtend:
        bits.append("до %s" % dtend)
    if loc:
        bits.append("место: %s" % loc)
    text = ", ".join(bits)
    if desc:
        text += "\n  " + desc
    return "  UID: %s\n  %s" % (uid, text)


def _event_span(comp):
    """Возвращает (начало, конец) события как aware-datetime в локальном поясе.

    Для событий «на весь день» (dtstart — date, без времени) возвращает
    границы суток [00:00, 23:59:59]. Если dtend не задан, конец = началу.
    Возвращает None, если у события нет dtstart.
    """
    def _to_dt(value):
        if value is None:
            return None
        if isinstance(value, _dt.datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=_tzinfo())
            return value.astimezone(_tzinfo())
        if isinstance(value, _dt.date):          # событие на весь день
            return _dt.datetime(value.year, value.month, value.day,
                                tzinfo=_tzinfo())
        return None

    if comp is None or not comp.get("dtstart"):
        return None
    start = _to_dt(comp.get("dtstart").dt)
    if start is None:
        return None
    end = _to_dt(comp.get("dtend").dt) if comp.get("dtend") else None
    if end is None:
        # Для события на весь день без dtend берём конец тех же суток.
        d = comp.get("dtstart").dt
        if isinstance(d, _dt.date) and not isinstance(d, _dt.datetime):
            end = start.replace(hour=23, minute=59, second=59)
        else:
            end = start
    return start, end


def _events_in_range(cal, start, end, single_day=False):
    """Возвращает список (Event, VEVENT-компонент), попадающих в [start, end].

    single_day=True — запрос за ОДНИ сутки: возвращаются ТОЛЬКО события этого
    дня (ничего из соседних дней). В этом режиме фильтр работает по
    КАЛЕНДАРНОЙ ДАТЕ события в локальном поясе.

    ВАЖНО (две тонкости Яндекс.CalDAV):
      1) Ресурсы могут прийти ШИРЕ запрошенного диапазона (например, событие
         соседней даты) — поэтому выполняется ЯВНАЯ фильтрация по фактическим
         датам события: остаются только пересекающиеся с окном [start, end].
      2) ПОВТОРЯЮЩИЕСЯ события (RRULE, напр. «Тренировка» по ПН/СР/ПТ) нужно
         РАСКРЫТЬ на вхождения внутри периода. Поэтому поиск идёт с
         expand=True: сервер отдаёт КАЖДОЕ вхождение отдельно (со своим
         recurrence-id и dtstart). При expand=False повтор вернулся бы только
         один раз — на свою ПЕРВУЮ дату (напр. 23 сентября), и вхождения
         внутри окна (напр. пятница 25 сентября) терялись бы.
    """
    import icalendar
    # Границы окна приводим к aware-datetime в локальном поясе: событие
    # может быть отдано с часовым поясом (aware), и сравнивать его с
    # «наивным» datetime нельзя (TypeError: offset-naive vs offset-aware).
    tz = _tzinfo()

    def _aware(value):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=tz)
        return value.astimezone(tz)

    win_start = _aware(start)
    win_end = _aware(end)
    # День (для режима single_day): границы суток [00:00, 23:59:59].
    day0 = win_start.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day0.replace(hour=23, minute=59, second=59)
    out = []
    raw = cal.search(start=start, end=end, event=True, expand=True)
    for ev in raw:
        try:
            cal_obj = icalendar.Calendar.from_ical(str(ev.data))
        except Exception:
            continue
        for comp in cal_obj.walk("VEVENT"):
            span = _event_span(comp)
            if span is None:
                continue
            ev_start, ev_end = span
            if single_day:
                # Только события, ПЕРЕСЕКАЮЩИЕ запрошенные сутки:
                # начало не позже конца дня И конец не раньше начала дня.
                if ev_start <= day_end and ev_end >= day0:
                    out.append((ev, comp, ev_start))
            else:
                # Событие пересекается с окном, если его начало не позже конца
                # окна, а конец — не раньше начала окна.
                if ev_start <= win_end and ev_end >= win_start:
                    out.append((ev, comp, ev_start))
    # Сортируем по времени начала — читаемый порядок.
    out.sort(key=lambda item: item[2])
    return out


# --------------------------------------------------------------------------
# Поиск события по «человеческим» признакам (название/дата/место)
# --------------------------------------------------------------------------
def _norm(text):
    """Нормализует строку для сравнения: нижний регистр, без лишних пробелов."""
    return " ".join(str(text or "").strip().lower().split())


def _summary_matches(comp, query):
    """True, если название события содержит запрос (без учёта регистра).

    Сравнение по вхождению: «мойка» найдёт «Мойка машины».
    """
    if not query:
        return True
    return _norm(query) in _norm(comp.get("summary"))


def _selector_window(start, end):
    """Возвращает (границы окна поиска, single_day) по строкам start/end.

    start/end — "YYYY-MM-DD" или "YYYY-MM-DD HH:MM" (оба необязательны).
    Если задана только одна граница — вторая берётся равной ей.
    """
    s = _parse_dt(start) if start else None
    e = _parse_dt(end) if end else None
    if s is None and e is None:
        return None, None, False
    if s is None:
        s = e
    if e is None:
        e = s
    single_day = (s.date() == e.date())
    if _is_date_only(end) or e <= s:
        e = _end_of_day(e)
    return s, e, single_day


def _iter_all_events(cal):
    """Перебирает события календаря за ШИРОКОЕ окно (надёжнее, чем cal.events()).

    Яндекс.Календарь по запросу cal.events() отдаёт НЕ ВСЕ ресурсы
    (наблюдались пропуски). Поэтому «поиск по всему календарю» делаем через
    cal.search по широкому диапазону дат (±CALENDAR_SEARCH_YEARS лет, по
    умолчанию 2). Возвращает (Event, VEVENT-компонент), без дублей.
    """
    years = int(os.environ.get("CALENDAR_SEARCH_YEARS") or "2")
    today = _dt.datetime.now(_tzinfo())
    lo = today.replace(year=today.year - years, month=1, day=1, hour=0,
                       minute=0, second=0, microsecond=0)
    hi = today.replace(year=today.year + years, month=12, day=31, hour=23,
                       minute=59, second=59, microsecond=0)
    seen = set()
    for ev, comp, ev_start in _events_in_range(cal, lo, hi):
        key = (str(comp.get("uid") or ""), ev_start)
        if key in seen:
            continue
        seen.add(key)
        yield ev, comp


def _find_candidates(cal, summary="", start="", end="", location=""):
    """Ищет события по «человеческим» признакам.

    Критерии (все необязательны, комбинируются как И):
      * summary  — подстрока в названии (без регистра);
      * start/end — период (даты/время), по умолчанию — весь календарь;
      * location — подстрока в месте события (без регистра).

    Возвращает список кортежей (Event, comp, ev_start, recurrence_id_str).
    """
    if start or end:
        s, e, single_day = _selector_window(start, end)
        src = ((ev, comp) for ev, comp, _st in
               _events_in_range(cal, s, e, single_day=single_day))
    else:
        src = _iter_all_events(cal)

    out = []
    for ev, comp in src:
        if not _summary_matches(comp, summary):
            continue
        if location and _norm(location) not in _norm(comp.get("location")):
            continue
        span = _event_span(comp)
        if span is None:
            continue
        rid = comp.get("recurrence-id")
        rid_str = _localize_str(rid.dt) if (rid is not None and rid.dt is not None) else ""
        out.append((ev, comp, span[0], rid_str))
    out.sort(key=lambda item: item[2])
    return out


def _delete_comp(cal, ev, comp):
    """Удаляет событие или, для вхождения повтора, — только одно вхождение.

    ev   — CalDAV-ресурс;
    comp — конкретный VEVENT (возможно, раскрытое вхождение с recurrence-id).

    ВАЖНО: при поиске с expand=True ресурс ev.data содержит ТОЛЬКО раскрытое
    вхождение (без RRULE). Поэтому, если у вхождения есть recurrence-id,
    загружаем МАСТЕР-ресурс заново по ev.url (там правило RRULE), добавляем
    EXDATE на дату вхождения и сохраняем — правило и прочие даты сохраняются.
    Если recurrence-id нет — это обычное событие, удаляем ресурс целиком.
    """
    import icalendar
    target_rid = comp.get("recurrence-id")
    if target_rid is not None and target_rid.dt is not None:
        # Загружаем мастер-ресурс по URL (ev.data здесь — лишь вхождение).
        master = cal.event_by_url(ev.url)
        cal_obj = icalendar.Calendar.from_ical(str(master.data))
        for m in cal_obj.walk("VEVENT"):
            if m.get("recurrence-id") is None and m.get("rrule"):
                m.add("exdate", target_rid.dt)
                master.data = cal_obj.to_ical().decode("utf-8")
                master.save()
                return "recurrence"
    # Обычное событие (или одиночный VEVENT) — удаляем ресурс целиком.
    ev.delete()
    return "event"


def _fmt_candidates(candidates):
    """Формирует читаемый список найденных событий (для уточнения выбора)."""
    lines = []
    for i, (_ev, comp, ev_start, rid_str) in enumerate(candidates, 1):
        when = ev_start.strftime("%Y-%m-%d %H:%M")
        mark = " (вхождение повтора)" if rid_str else ""
        lines.append("  %d) «%s» — %s%s" % (i, comp.get("summary") or "", when, mark))
    return "\n".join(lines)


def _pick_target(cal, uid="", summary="", start="", end="", location=""):
    """Определяет ЦЕЛЕВОЕ событие для правки/удаления.

    Возвращает кортеж (ok, payload):
      * (True, (ev, comp, ev_start, rid_str)) — найдено ровно одно событие;
      * (False, "текст")                — 0 совпадений или несколько
                                          (текст — сообщение с перечнем).
    Сначала ищет по uid; если uid не задан — по названию/дате/месту.
    """
    # 1) Поиск по UID (точное совпадение) — если он задан.
    if uid:
        for ev, comp in _iter_all_events(cal):
            if str(comp.get("uid") or "") == str(uid):
                span = _event_span(comp)
                ev_start = span[0] if span else None
                return True, (ev, comp, ev_start, "")
        return False, "Событие с UID %s не найдено." % uid
    # 2) Поиск по «человеческим» признакам.
    cands = _find_candidates(cal, summary=summary, start=start,
                             end=end, location=location)
    if not cands:
        return False, ("Событие не найдено. Уточните название или дату "
                       "(инструмент find_event).")
    if len(cands) > 1:
        return False, ("Найдено несколько событий — уточните, какое именно "
                       "(добавьте дату или полное название):\n%s"
                       % _fmt_candidates(cands))
    return True, cands[0]


# --------------------------------------------------------------------------
# Напоминания по расписанию (до события)
# --------------------------------------------------------------------------
_REMINDERS = JobsStore(server="calendar")


def _reminders():
    """Хранилище напоминаний этого сервера (пространство "calendar")."""
    return _REMINDERS


def _event_start_dt(cal, uid="", summary="", date=""):
    """Возвращает (start_datetime, event_summary) целевого события.

    Ищет по uid ИЛИ словами (summary/date). Возбуждает RuntimeError, если
    событие не найдено/неоднозначно или у него нет времени начала.
    """
    ok, payload = _pick_target(cal, uid=uid, summary=summary,
                               start=date, end=date)
    if not ok:
        raise RuntimeError(payload)
    _ev, comp, ev_start, _rid = payload
    if ev_start is None:
        raise RuntimeError("у события не определено время начала")
    return ev_start, str(comp.get("summary") or "")


def _run_due_reminders():
    """Собирает НАСТУПИВШИЕ напоминания. Возвращает список текстов-напоминаний.

    Лениво вызывается из других инструментов и явно из run_due().

    Разовое напоминание после срабатывания помечается done. ДЛЯ ПОВТОРЯЮЩЕГОСЯ
    (repeat_minutes > 0) напоминание срабатывает СНОВА через repeat_minutes —
    но только ПОКА НЕ НАСТУПИЛО событие: как только следующий момент повтора
    достигнет/перешагнёт время начала события, задание завершается (done),
    чтобы не напоминать о прошедшем событии.
    """
    messages = []
    for job in _reminders().due_jobs():
        if job.get("kind") != "reminder":
            continue
        params = job.get("params") or {}
        title = params.get("event_summary") or params.get("summary") or "событие"
        when = _trim_seconds(params.get("event_start"))
        lead = int(params.get("lead_minutes", 0) or 0)
        repeat = int(params.get("repeat_minutes", 0) or 0)
        lead_txt = _human_lead(lead)
        # ВАЖНО: НЕ выдаём напоминание о событии, которое УЖЕ началось.
        # Если напоминание «залежалось» (планировщик не вызывался, пока шло
        # событие) — закрываем задание молча, без «напоминания о прошедшем».
        if _event_has_started(params):
            _reminders().mark_fired(job["id"], "", "событие уже наступило")
            continue
        if repeat > 0:
            messages.append("НАПОМИНАНИЕ: «%s» начнётся %s (%s; повтор каждые %s)."
                            % (title, when, lead_txt, _human_dur(repeat)))
        else:
            messages.append("НАПОМИНАНИЕ: «%s» начнётся %s (%s)."
                            % (title, when, lead_txt))
        # Вычисляем следующий момент повтора с оглядкой на начало события.
        next_run = _next_reminder_time(params, repeat)
        _reminders().mark_fired(job["id"], next_run, "напомнил: %s" % title)
    return messages


def _event_has_started(params, at_time=None):
    """True, если время начала события уже наступило (<= указанного момента).

    Нужно, чтобы НЕ озвучивать напоминание о прошедшем событии (когда
    планировщик вызывается с опозданием — например, после простоя).
    Сравнение идёт по строке в формате TIME_FMT — лексикографически это
    эквивалентно сравнению во времени.

    at_time — момент сравнения (строка TIME_FMT); по умолчанию «сейчас».
    Если время начала события не разбирается — считаем, что НЕ наступило
    (лучше показать напоминание, чем потерять его из-за формата).
    """
    start = _parse_event_start(params.get("event_start"))
    if not start:
        return False
    ref = at_time or now_str()
    return time.strftime(TIME_FMT, start) <= ref


def _next_reminder_time(params, repeat_minutes):
    """Следующий момент повторного напоминания (строка TIME_FMT) или "".

    Правило: после каждого срабатывания следующий момент = предыдущий +
    repeat_minutes. Если repeat_minutes <= 0 — возвращаем "" (разовое, done).
    Если следующий момент оказывается НЕ РАНЬШЕ начала события — тоже ""
    (напоминания прекращаются с наступлением события).
    """
    if int(repeat_minutes or 0) <= 0:
        return ""
    start = _parse_event_start(params.get("event_start"))
    if not start:
        return ""
    # Предыдущий момент — время «сейчас» (задание только что сработало).
    nxt = time.localtime(time.time() + int(repeat_minutes) * 60)
    if nxt >= start:
        return ""   # событие уже началось (или начнётся до след. повтора)
    return time.strftime(TIME_FMT, nxt)


def _parse_event_start(value):
    """Разбирает время начала события из params в struct_time (терпимо к формату).

    В параметрах event_start может храниться без секунд ("YYYY-MM-DD HH:MM") —
    поэтому пробуем несколько форматов, а не только TIME_FMT.
    """
    if not value:
        return None
    s = str(value).strip().replace("T", " ")
    for fmt in (TIME_FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return time.strptime(s, fmt)
        except ValueError:
            continue
    return parse_time(value)


def _trim_seconds(value):
    """Убирает секунды из строки времени для показа: '…15:44:00' -> '…15:44'."""
    s = str(value or "").strip()
    if len(s) == 19 and s[16] == ":":   # "YYYY-MM-DD HH:MM:SS"
        return s[:16]
    return s


def _human_dur(minutes):
    """Человекочитаемая длительность периода: 60 -> '1 ч', 90 -> '1 ч 30 мин'."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return ""
    if m <= 0:
        return "0 мин"
    days, rem = divmod(m, 1440)
    hours, mins = divmod(rem, 60)
    parts = []
    if days:
        parts.append("%d д" % days)
    if hours:
        parts.append("%d ч" % hours)
    if mins:
        parts.append("%d мин" % mins)
    return " ".join(parts)


def _human_lead(minutes):
    """Человекочитаемая длительность опережения: 90 -> 'за 1 ч 30 мин до начала'."""
    try:
        m = int(minutes)
    except (TypeError, ValueError):
        return ""
    if m <= 0:
        return "до начала"
    return "за " + _human_dur(m) + " до начала"


def _fmt_reminders(jobs):
    """Читаемое представление списка напоминаний."""
    if not jobs:
        return "Напоминаний нет."
    lines = []
    for j in jobs:
        p = j.get("params") or {}
        repeat = int(p.get("repeat_minutes", 0) or 0)
        rep_txt = ("; повтор каждые %s" % _human_dur(repeat)) if repeat > 0 else ""
        if j.get("done"):
            status = "выполнено"
        else:
            status = ("сработает: %s" % j.get("next_run", ""))
            if int(j.get("runs", 0) or 0) > 0:
                status = ("сработает: %s (уже раз: %d)"
                          % (j.get("next_run", ""), int(j.get("runs", 0) or 0)))
        lines.append("  %s — «%s» %s%s, %s"
                     % (j.get("id"), p.get("event_summary", "?"),
                        _human_lead(p.get("lead_minutes", 0)), rep_txt, status))
    return "\n".join(lines)



# --------------------------------------------------------------------------
# Инструменты MCP
# --------------------------------------------------------------------------
@mcp.tool()
def list_calendars() -> str:
    """Список календарей пользователя Яндекс.Календаря."""
    try:
        with _client() as client:
            cals = list(client.principal().calendars())
    except Exception as exc:
        return "Ошибка доступа к календарю: %s" % exc
    if not cals:
        return "Календарей не найдено."
    lines = []
    for i, c in enumerate(cals):
        try:
            name = c.get_display_name()
        except Exception:
            name = "календарь %d" % i
        lines.append("- %s (%s)" % (name, c.url))
    return "Календари:\n" + "\n".join(lines)


@mcp.tool()
def list_events(start: str, end: str) -> str:
    """Список событий календаря за период.

    start, end — границы периода в формате "YYYY-MM-DD" или "YYYY-MM-DD HH:MM".
    Год подставляйте ТЕКУЩИЙ (см. системное сообщение), например
    start="<текущий год>-01-01", end="<текущий год>-01-31".
    Если start и end — один и тот же день, вернутся только события этого дня.
    """
    # ЛЕНИВЫЙ ПРОГОН напоминаний: при обращении к календарю собираем наступившие.
    _run_due_reminders()
    try:
        s = _parse_dt(start)
        e = _parse_dt(end)
        if s is None or e is None:
            return ("Укажите start и end в формате YYYY-MM-DD "
                    "(год — текущий).")
        # Даты БЕЗ времени трактуются как ВКЛЮЧАЮЩИЕ календарные дни:
        #   start=2026-09-25, end=2026-09-26  ->  [25.09 00:00, 26.09 23:59:59].
        last_day = e
        if _is_date_only(end):
            e = _end_of_day(e)
        elif e <= s:
            e = _end_of_day(e)
        # ОДИН ДЕНЬ: обе границы — одна и та же календарная дата.
        single_day = (s.date() == last_day.date())
        with _client() as client:
            cal = _main_calendar(client)
            events = _events_in_range(cal, s, e, single_day=single_day)
    except Exception as exc:
        return "Ошибка доступа к календарю: %s" % exc
    # Подпись периода: для одного дня — одна дата, иначе диапазон.
    disp_start = s.strftime("%Y-%m-%d")
    disp_end = last_day.strftime("%Y-%m-%d")
    if not _is_date_only(end) and last_day <= s:
        disp_end = disp_start
    period = disp_start if disp_start == disp_end else ("%s — %s" % (disp_start, disp_end))
    if not events:
        return "Событий за период %s нет." % period
    lines = ["События за %s:" % period]
    for _ev, comp, _start in events:
        lines.append(_fmt_event(comp))
    return "\n".join(lines)


@mcp.tool()
def find_event(summary: str = "", date: str = "", start: str = "",
               end: str = "", location: str = "") -> str:
    """Найти события по названию/дате/месту — БЕЗ UID.

    Используйте, чтобы уточнить, какое событие имеется в виду, прежде чем
    удалять или менять его. Критерии комбинируются (И):
      * summary — часть названия (например, «мойка»);
      * date    — один день ("YYYY-MM-DD");
      * start, end — период ("YYYY-MM-DD"); если задан только date — он же;
      * location — часть места события.
    """
    try:
        if date and not start:
            start = end = date
        with _client() as client:
            cal = _main_calendar(client)
            cands = _find_candidates(cal, summary=summary, start=start,
                                     end=end, location=location)
    except Exception as exc:
        return "Ошибка доступа к календарю: %s" % exc
    if not cands:
        return "Подходящих событий не найдено."
    return ("Найдено событий: %d\n%s" % (len(cands), _fmt_candidates(cands)))


@mcp.tool()
def create_event(summary: str, start: str, end: str,
                 description: str = "", location: str = "") -> str:
    """Создать событие в календаре.

    summary     — название;
    start, end  — начало и конец ("YYYY-MM-DD" или "YYYY-MM-DD HH:MM");
    description — описание (необязательно);
    location    — место (необязательно).
    """
    try:
        s = _parse_dt(start)
        e = _parse_dt(end)
        if s is None or e is None:
            return "Укажите start и end события."
        if not summary:
            return "Укажите название события (summary)."
        # Привязываем события без пояса к локальному (Europe/Moscow),
        # чтобы время сохранялось корректно.
        tz = _tzinfo()
        if isinstance(s, _dt.datetime) and s.tzinfo is None:
            s = s.replace(tzinfo=tz)
        if isinstance(e, _dt.datetime) and e.tzinfo is None:
            e = e.replace(tzinfo=tz)
        with _client() as client:
            cal = _main_calendar(client)
            ev = cal.save_event(
                dtstart=s, dtend=e, summary=summary,
                description=description or None,
                location=location or None,
            )
    except Exception as exc:
        return "Не удалось создать событие: %s" % exc
    return "Событие создано: «%s» (%s). UID: %s" % (summary, start, ev.url)


@mcp.tool()
def update_event(uid: str = "", summary: str = "", start: str = "",
                 end: str = "", description: str = "", location: str = "",
                 find_summary: str = "", date: str = "") -> str:
    """Изменить существующее событие.

    Найти событие можно ЛИБО по uid, ЛИБО «словами»:
      * find_summary — часть текущего названия (например, «мойка»);
      * date         — дата события ("YYYY-MM-DD").
    Задавайте только те поля, которые нужно изменить (summary/start/end/
    description/location). Пустые поля не меняются.
    Если под описание подходит несколько событий — вернётся их список
    для уточнения (тогда добавьте date).

    Для события-ПОВТОРА правки применяются ко ВСЕЙ серии (мастер-событию).
    Чтобы изменить/сдвинуть только одно вхождение повтора — удалите это
    вхождение (delete_event) и создайте новое (create_event).
    """
    try:
        import icalendar
        with _client() as client:
            cal = _main_calendar(client)
            ok, payload = _pick_target(cal, uid=uid, summary=find_summary,
                                       start=date, end=date)
            if not ok:
                return payload
            ev, comp, _st, _rid = payload
            is_occurrence = comp.get("recurrence-id") is not None
            if is_occurrence:
                # Загружаем МАСТЕР-ресурс (в ev.data — лишь одно вхождение).
                ev = cal.event_by_url(ev.url)
            cal_obj = icalendar.Calendar.from_ical(str(ev.data))
            # Правим VEVENT: для повтора — мастер (без recurrence-id),
            # иначе — выбранный (по uid).
            target_uid = str(comp.get("uid") or "")
            edited = False
            for c in cal_obj.walk("VEVENT"):
                if str(c.get("uid") or "") != target_uid:
                    continue
                if is_occurrence and c.get("recurrence-id") is not None:
                    continue   # пропускаем раскрытые вхождения
                if summary:
                    c["summary"] = summary
                if description:
                    c["description"] = description
                if location:
                    c["location"] = location
                if start:
                    c["dtstart"].dt = _parse_dt(start)
                if end:
                    c["dtend"].dt = _parse_dt(end)
                edited = True
                break
            if not edited:
                return "Не удалось определить событие для изменения."
            ev.data = cal_obj.to_ical().decode("utf-8")
            ev.save()
    except Exception as exc:
        return "Не удалось изменить событие: %s" % exc
    if is_occurrence:
        return "Изменена вся серия повтора (не только одно вхождение)."
    return "Событие изменено."


@mcp.tool()
def delete_event(uid: str = "", summary: str = "", date: str = "",
                 start: str = "", end: str = "", location: str = "") -> str:
    """Удалить событие.

    Найти событие можно ЛИБО по uid, ЛИБО «словами»:
      * summary — часть названия (например, «мойка»);
      * date    — дата ("YYYY-MM-DD"), либо период start/end;
      * location — часть места.
    Если под описание подходит НЕСКОЛЬКО событий — вернётся их список
    для уточнения (тогда добавьте дату). Если ровно одно — оно удаляется.
    Для события-повтора удаляется ТОЛЬКО выбранное вхождение.
    """
    ev_start = None
    comp = None
    try:
        with _client() as client:
            cal = _main_calendar(client)
            ok, payload = _pick_target(cal, uid=uid, summary=summary,
                                       start=(date or start), end=(date or end),
                                       location=location)
            if not ok:
                return payload
            ev, comp, ev_start, _rid = payload
            _delete_comp(cal, ev, comp)
    except Exception as exc:
        return "Не удалось удалить событие: %s" % exc
    when = ev_start.strftime("%Y-%m-%d %H:%M") if ev_start else ""
    return "Событие «%s»%s удалено." % (
        comp.get("summary") or "", (" (%s)" % when) if when else "")


# --------------------------------------------------------------------------
# Инструменты напоминаний (расписание «до события»)
# --------------------------------------------------------------------------
@mcp.tool()
def schedule_reminder(uid: str = "", summary: str = "", date: str = "",
                      lead_minutes: int = 30, repeat_minutes: int = 0,
                      repeat_hours: float = 0) -> str:
    """Поставить НАПОМИНАНИЕ к событию — напомнить за N до начала, с повтором.

    Событие ищется ЛИБО по uid, ЛИБО «словами»:
      * summary — часть названия (например, «встреча»);
      * date    — дата события ("YYYY-MM-DD") — если нужно уточнить.
    lead_minutes  — за сколько МИНУТ до начала НАЧАТЬ напоминать
                    (например, 30, 120, 1440 — за сутки).
    repeat_minutes — КАК ЧАСТО повторять напоминание, в минутах
                    (например, 1 — раз в минуту; 60 — раз в час; 0 — один раз).
    repeat_hours  — то же в часах (удобно для «раз в час»: repeat_hours=1);
                    если задано, перекрывает repeat_minutes.

    Первое напоминание — за lead_minutes до начала; далее — каждые
    repeat_minutes, ПОКА НЕ НАСТУПИТ событие (затем напоминания прекращаются).
    Наступившие напоминания попадают в due_reminders()/run_due() (и в ленивый
    прогон при обращении к календарю). Пример: напомнить за 1 ч и повторять
    каждые 10 минут: lead_minutes=60, repeat_minutes=10.
    """
    try:
        with _client() as client:
            cal = _main_calendar(client)
            start_dt, title = _event_start_dt(cal, uid=uid, summary=summary,
                                              date=date)
    except Exception as exc:
        return "Не удалось поставить напоминание: %s" % exc
    try:
        lead = max(0, int(lead_minutes or 0))
    except (TypeError, ValueError):
        lead = 30
    # Частота повтора: часы (если заданы) важнее минут.
    try:
        if repeat_hours and float(repeat_hours) > 0:
            repeat = int(round(float(repeat_hours) * 60))
        else:
            repeat = max(0, int(repeat_minutes or 0))
    except (TypeError, ValueError):
        repeat = 0
    # Момент первого напоминания = начало события − lead (в прошлом → сразу).
    remind_at = start_dt - _dt.timedelta(minutes=lead)
    job = _reminders().add_job(
        "reminder",
        {"event_summary": title,
         "event_start": start_dt.strftime(TIME_FMT),
         "lead_minutes": lead, "repeat_minutes": repeat, "uid": uid or ""},
        every_minutes=0,
        at=remind_at.strftime(TIME_FMT),
    )
    rep_txt = (", повтор каждые %s" % _human_dur(repeat)) if repeat > 0 else ""
    return ("Напоминание %s создано: «%s» %s%s (событие: %s)."
            % (job["id"], title, _human_lead(lead), rep_txt,
               start_dt.strftime("%Y-%m-%d %H:%M")))


@mcp.tool()
def list_reminders() -> str:
    """Список напоминаний (к каким событиям и за сколько)."""
    return "Напоминания (Календарь):\n" + _fmt_reminders(_reminders().list_jobs())


@mcp.tool()
def cancel_reminder(reminder_id: str) -> str:
    """Отменить напоминание по его id."""
    if _reminders().delete_job(str(reminder_id or "").strip()):
        return "Напоминание %s отменено." % reminder_id
    return "Напоминание %s не найдено." % reminder_id


@mcp.tool()
def due_reminders() -> str:
    """НАСТУПИВШИЕ напоминания — что нужно озвучить сейчас.

    Возвращает список напоминаний, срок которых уже наступил (и которые ещё
    не выданы). Для ПОВТОРЯЮЩИХСЯ напоминаний (repeat_minutes > 0) после
    выдачи назначается следующее срабатывание (пока не наступит событие);
    разовое напоминание помечается исполненным.
    """
    msgs = _run_due_reminders()
    if not msgs:
        return "Наступивших напоминаний нет."
    return "\n".join(msgs)


@mcp.tool()
def run_due() -> str:
    """Собрать все НАСТУПИВШИЕ напоминания (тик планировщика).

    То же, что due_reminders(), но под «тик»: удобно периодически вызывать,
    чтобы забирать сработавшие напоминания (заменяет отсутствующий демон).
    Повторяющиеся напоминания остаются активными до начала события.
    """
    msgs = _run_due_reminders()
    if not msgs:
        return "Наступивших напоминаний нет."
    return "Напоминаний: %d\n%s" % (len(msgs), "\n".join(msgs))


if __name__ == "__main__":
    # Транспорт по умолчанию — stdio (как подпроцесс MCP-клиента проекта).
    mcp.run()
