# -*- coding: utf-8 -*-
"""Автономная проверка хранилища заданий расписания (rtk_app/jobs_store.py).

Сеть и MCP не требуются: проверяем САМ МЕХАНИЗМ заданий (docs/task4.md):
  * сериализацию заданий в JSON и чтение обратно;
  * вычисление первого запуска (every_minutes / at / in_minutes);
  * выбор СОЗРЕВШИХ заданий (due_jobs);
  * перепланирование периодических и завершение разовых (mark_run);
  * накопление точек и агрегацию (данные курсов).

Запуск:
    python tests/check_jobs.py
"""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.harness import check, section, finish, ensure_utf8
from rtk_app.jobs_store import JobsStore, now_str, TIME_FMT


def _tmp_store(server="test"):
    """Создаёт JobsStore на ВРЕМЕННОМ файле (изоляция теста)."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)   # пусть создаётся сам
    os.environ["MCP_JOBS_FILE"] = path
    return JobsStore(path=path, server=server), path


def main():
    ensure_utf8()

    # --------------------------------------------------------------
    section("1. Создание и сохранение задания в JSON")
    store, path = _tmp_store("calendar")
    job = store.add_job("reminder", {"event_summary": "Встреча"},
                        every_minutes=0, in_minutes=10)
    check("задание создано с id", bool(job.get("id")))
    check("тип задания сохранён", job.get("kind") == "reminder")
    check("файл JSON создан на диске", os.path.isfile(path))
    # Новое хранилище на том же файле видит задание (персистентность).
    store2 = JobsStore(path=path, server="calendar")
    jobs = store2.list_jobs()
    check("задание читается из файла", len(jobs) == 1)
    check("имя события сохранилось",
          jobs[0]["params"].get("event_summary") == "Встреча")

    # --------------------------------------------------------------
    section("2. Созывание (due_jobs) по времени")
    # Разовое задание «через 10 минут» ещё НЕ созрело.
    check("будущее задание не созрело", len(store2.due_jobs()) == 0)
    # Разовое задание, созревшее СРАЗУ (без времени).
    now_job = store2.add_job("rate", {"from_currency": "USD"}, every_minutes=0)
    check("разовое без времени созрело сразу",
          any(j["id"] == now_job["id"] for j in store2.due_jobs()))

    # --------------------------------------------------------------
    section("3. Перепланирование периодического задания")
    # Периодическое: every=60, первый запуск — «сейчас» (at в прошлом).
    past = time.strftime(TIME_FMT, time.localtime(time.time() - 3600))
    pjob = store2.add_job("rate", {"from_currency": "USD", "to_currency": "RUB"},
                          every_minutes=60, at=past)
    due = store2.due_jobs()
    check("просроченное периодическое созрело",
          any(j["id"] == pjob["id"] for j in due))
    store2.mark_run(pjob["id"], "ok")
    after = store2.get_job(pjob["id"])
    check("после запуска runs увеличился", int(after["runs"]) == 1)
    check("периодическое не помечено done", after.get("done") is False)
    check("next_run сдвинут в будущее",
          str(after.get("next_run")) > now_str())
    check("после запуска задание больше не созрело",
          not any(j["id"] == pjob["id"] for j in store2.due_jobs()))

    # --------------------------------------------------------------
    section("4. Завершение разового задания")
    store2.mark_run(now_job["id"], "выполнено")
    done_job = store2.get_job(now_job["id"])
    check("разовое помечено done", done_job.get("done") is True)
    check("done-задание не попадает в due",
          not any(j["id"] == now_job["id"] for j in store2.due_jobs()))

    # --------------------------------------------------------------
    section("5. Накопление точек и агрегация данных")
    cstore, cpath = _tmp_store("currency")
    series = "USD>RUB"
    cstore.add_point(series, {"t": "2026-01-01 10:00:00", "rate": 80.0})
    cstore.add_point(series, {"t": "2026-01-02 10:00:00", "rate": 90.0})
    cstore.add_point(series, {"t": "2026-01-03 10:00:00", "rate": 85.0})
    pts = cstore.get_series(series)
    check("точки сохранены (3)", len(pts) == 3)
    rates = [p["rate"] for p in pts]
    check("минимум корректный", min(rates) == 80.0)
    check("максимум корректный", max(rates) == 90.0)
    check("среднее корректное", abs(sum(rates) / len(rates) - 85.0) < 1e-9)
    check("изменение (last-first) корректно", (rates[-1] - rates[0]) == 5.0)
    # Персистентность данных: новое хранилище видит серию.
    check("серия читается из файла",
          JobsStore(path=cpath, server="currency").get_series(series) == pts)
    check("имя серии в списке",
          series in JobsStore(path=cpath, server="currency").series_names())

    # --------------------------------------------------------------
    section("6. Изоляция пространств серверов и отмена заданий")
    check("пространства calendar/currency не смешиваются",
          len(JobsStore(path=path, server="calendar").list_jobs()) >= 2)
    check("отмена задания работает", store2.delete_job(pjob["id"]) is True)
    check("отменённого задания нет", store2.get_job(pjob["id"]) is None)
    check("отмена несуществующего -> False",
          store2.delete_job("no-such") is False)

    # Очистим временные файлы.
    for p in (path, cpath):
        try:
            os.remove(p)
        except OSError:
            pass

    sys.exit(finish())


if __name__ == "__main__":
    main()
