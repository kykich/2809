# -*- coding: utf-8 -*-
"""Проверка напоминания с ПОВТОРОМ КАЖДУЮ МИНУТУ (сквозной сценарий, офлайн).

Фокусируется на том, что сообщил пользователь («напоминания не работают
корректно»), и проверяет ТРИ вещи, которые важны для напоминания:

  1. СОЗРЕВШЕЕ повторяющееся напоминание срабатывает;
  2. повтор назначается ровно через repeat_minutes (каждую минуту);
  3. НЕ выдаётся напоминание, если событие УЖЕ наступило (иначе приходит
     «напоминание о прошедшем событии» — это и есть баг).

Работает без сети: временный файл заданий + прямой вызов исполнителя
напоминаний (как tests/check_reminders.py). Запуск:
    python tests/check_reminder_repeat.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.harness import check, section, finish, ensure_utf8

TIME_FMT = "%Y-%m-%d %H:%M:%S"


def iso(sec):
    """Секунды эпохи -> строка TIME_FMT."""
    return time.strftime(TIME_FMT, time.localtime(sec))


def main():
    ensure_utf8()

    # Изолированный файл заданий — не трогаем рабочий session/mcp_jobs.json.
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    os.environ["MCP_JOBS_FILE"] = path

    import yandex_calendar_server as cal
    from rtk_app.jobs_store import JobsStore

    store = JobsStore(path=path, server="calendar")
    cal._REMINDERS = store

    now = time.time()

    # ------------------------------------------------------------------
    section("1. Созревшее напоминание (repeat=1) срабатывает один раз")
    event_start = iso(now + 30 * 60)          # событие через 30 минут
    job = store.add_job(
        "reminder",
        {"event_summary": "ПЕРЕКУР", "event_start": event_start,
         "lead_minutes": 30, "repeat_minutes": 1, "uid": ""},
        at=iso(now - 1),                      # первое срабатывание уже созрело
    )
    msgs = cal._run_due_reminders()
    check("сработало ровно одно напоминание", len(msgs) == 1)
    check("в тексте есть название события",
          "ПЕРЕКУР" in (msgs[0] if msgs else ""))
    check("в тексте указан повтор 'каждые 1 мин'",
          "повтор каждые 1 мин" in (msgs[0] if msgs else ""))
    j = store.get_job(job["id"])
    check("задание НЕ завершено (повтор активен)", j["done"] is False)
    check("runs = 1", int(j["runs"]) == 1)

    # ------------------------------------------------------------------
    section("2. Повтор назначается через ~1 минуту, а не сразу")
    nr = j["next_run"]
    delta = time.mktime(time.strptime(nr, TIME_FMT)) - now
    check("next_run примерно через 1 минуту", 50 <= delta <= 70,
          "next_run=%s, delta=%.0f c" % (nr, delta))
    # Без ожидания повторно НЕ срабатывает.
    check("повторно без ожидания не срабатывает", cal._run_due_reminders() == [])

    # Эмулируем, что прошла минута: сдвигаем next_run в прошлое.
    store.mark_fired(job["id"], iso(now - 1), "эмуляция")
    msgs2 = cal._run_due_reminders()
    check("через минуту сработало снова", len(msgs2) == 1)

    # ------------------------------------------------------------------
    section("3. НЕ выдаём напоминание о ПРОШЕДШЕМ событии")
    # Событие уже началось 10 минут назад, задание всё ещё активно.
    past_event = iso(now - 10 * 60)
    job2 = store.add_job(
        "reminder",
        {"event_summary": "ПРОШЛОЕ", "event_start": past_event,
         "lead_minutes": 30, "repeat_minutes": 1, "uid": ""},
        at=iso(now - 1),
    )
    msgs3 = cal._run_due_reminders()
    # КОРРЕКТНОЕ поведение: напоминание о прошедшем событии НЕ озвучивается.
    check("нет напоминания о прошедшем событии", len(msgs3) == 0,
          "получено: %r" % (msgs3,))
    j2 = store.get_job(job2["id"])
    check("задание-прошедшее завершено (done)", j2["done"] is True)

    # ------------------------------------------------------------------
    section("4. Повторы прекращаются ровно с наступлением события")
    # next_reminder_time: событие через 5 мин -> повтор будет (~через 1 мин).
    check("событие через 5 мин -> повтор есть",
          bool(cal._next_reminder_time({"event_start": iso(now + 300)}, 1)))
    # событие через 0.5 мин -> повтор уже не успеет (наступит раньше).
    check("событие через 0.5 мин -> повтора нет",
          cal._next_reminder_time({"event_start": iso(now + 30)}, 1) == "")
    # событие в прошлом -> повтора нет.
    check("событие в прошлом -> повтора нет",
          cal._next_reminder_time({"event_start": iso(now - 60)}, 1) == "")

    # Уборка.
    try:
        os.remove(path)
    except OSError:
        pass

    sys.exit(finish())


if __name__ == "__main__":
    main()
