# -*- coding: utf-8 -*-
"""Автономная проверка ПОВТОРЯЮЩИХСЯ напоминаний календаря (без сети).

Проверяем механизм напоминаний MCP-сервера календаря (docs/task4.md):
  * разовое напоминание срабатывает один раз и завершается;
  * ПОВТОРЯЮЩЕЕСЯ (repeat_minutes) срабатывает снова каждые N минут;
  * повторы прекращаются с наступлением события (задание завершается);
  * человекочитаемый вывод периода (мин/ч/д).

Сеть и CalDAV не требуются: работаем на временном файле заданий и напрямую
вызываем исполнитель напоминаний. Запуск:
    python tests/check_reminders.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.harness import check, section, finish, ensure_utf8


def _iso(ts):
    """Время (секунды эпохи) -> строка TIME_FMT."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def main():
    ensure_utf8()

    # Временный файл заданий, чтобы не трогать рабочий session/mcp_jobs.json.
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    os.environ["MCP_JOBS_FILE"] = path

    # Импортируем сервер ПОСЛЕ настройки env (путь к файлу читается в нём).
    import yandex_calendar_server as cal
    from rtk_app.jobs_store import JobsStore

    store = JobsStore(path=path, server="calendar")
    # Подменяем хранилище напоминаний на временное.
    cal._REMINDERS = store

    # --------------------------------------------------------------
    section("1. Человекочитаемая длительность периода")
    check("60 мин -> '1 ч'", cal._human_dur(60) == "1 ч")
    check("1 мин -> '1 мин'", cal._human_dur(1) == "1 мин")
    check("90 мин -> '1 ч 30 мин'", cal._human_dur(90) == "1 ч 30 мин")
    check("1440 мин -> '1 д'", cal._human_dur(1440) == "1 д")
    check("0 -> '0 мин'", cal._human_dur(0) == "0 мин")

    # --------------------------------------------------------------
    section("2. Расчёт следующего момента повтора (_next_reminder_time)")
    now = time.time()
    # repeat=0 — повторов нет.
    check("repeat=0 -> нет следующего",
          cal._next_reminder_time({"event_start": ""}, 0) == "")
    # Событие далеко впереди, повтор 1 мин — следующий момент есть (~сейчас+1м).
    far = _iso(now + 3600)
    nxt = cal._next_reminder_time({"event_start": far}, 1)
    check("повтор далеко до события -> есть следующий", bool(nxt))
    if nxt:
        delta = time.mktime(time.strptime(nxt, cal.TIME_FMT)) - now
        check("следующий момент ~ через 1 мин", 50 <= delta <= 70,
              "delta=%.0f c" % delta)
    # Событие уже началось — повтор прекращается.
    past = _iso(now - 60)
    check("событие наступило -> повторов нет",
          cal._next_reminder_time({"event_start": past}, 1) == "")

    # --------------------------------------------------------------
    section("3. Разовое напоминание срабатывает один раз")
    past_at = _iso(now - 5)
    job = store.add_job("reminder",
                        {"event_summary": "Разовое", "event_start": far,
                         "lead_minutes": 30, "repeat_minutes": 0},
                        at=past_at)
    msgs = cal._run_due_reminders()
    check("разовое: одно сообщение", len(msgs) == 1)
    check("разовое: в тексте есть название", "Разовое" in (msgs[0] if msgs else ""))
    check("разовое: задание завершено", store.get_job(job["id"])["done"] is True)
    check("разовое: повторно не срабатывает", cal._run_due_reminders() == [])

    # --------------------------------------------------------------
    section("4. Повторяющееся напоминание срабатывает снова")
    rjob = store.add_job("reminder",
                         {"event_summary": "Повтор", "event_start": far,
                          "lead_minutes": 30, "repeat_minutes": 1},
                         at=past_at)
    m1 = cal._run_due_reminders()
    check("повтор: первое срабатывание", len(m1) == 1)
    check("повтор: в тексте указана периодичность",
          "повтор каждые 1 мин" in (m1[0] if m1 else ""))
    j1 = store.get_job(rjob["id"])
    check("повтор: не завершено", j1["done"] is False)
    check("повтор: runs=1", int(j1["runs"]) == 1)
    check("повтор: next_run в будущем", str(j1["next_run"]) > _iso(now))
    # Сразу повторно НЕ сработает (next_run ещё не наступил).
    check("повтор: без ожидания не срабатывает", cal._run_due_reminders() == [])

    # Сымитируем «прошла минута»: сдвинем next_run в прошлое через стор.
    # (mark_fired сам засчитывает запуск, поэтому после второго прохода
    #  исполнителя суммарно runs = 1 [первый] + 1 [ручной сдвиг] + 1 [второй]).
    store.mark_fired(rjob["id"], _iso(now - 5), "тест")
    m2 = cal._run_due_reminders()
    check("повтор: второе срабатывание после паузы", len(m2) == 1)
    check("повтор: число срабатываний выросло",
          int(store.get_job(rjob["id"])["runs"]) == 3)

    # --------------------------------------------------------------
    section("5. Повторы прекращаются с наступлением события")
    # Сделаем событие «уже начавшимся» — следующий повтор назначаться не должен.
    store.mark_fired(rjob["id"], _iso(now - 5), "тест")
    # Обновим params: событие в прошлом.
    import json
    data = store._load()
    for j in data["servers"]["calendar"]["jobs"]:
        if j["id"] == rjob["id"]:
            j["params"]["event_start"] = _iso(now - 10)
    store._save(data)
    m3 = cal._run_due_reminders()
    check("после наступления события повторов нет",
          cal._next_reminder_time(
              {"event_start": _iso(now - 10)}, 1) == "")
    check("задание завершено после срабатывания", store.get_job(rjob["id"])["done"] is True)
    check("дальше не срабатывает", cal._run_due_reminders() == [])

    # Уборка временного файла.
    try:
        os.remove(path)
    except OSError:
        pass

    sys.exit(finish())


if __name__ == "__main__":
    main()
