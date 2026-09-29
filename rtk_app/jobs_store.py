# -*- coding: utf-8 -*-
"""Хранилище ЗАДАНИЙ ПО РАСПИСАНИЮ (общий JSON в папке session/).

Механизм «MCP-инструмент с отложенным/периодическим выполнением» (см.
docs/task4.md). MCP-серверы проекта запускаются как stdio-подпроцессы НА
ВРЕМЯ ВЫЗОВА, поэтому фонового демона нет: задания (jobs) сохраняются в
единый JSON-файл и ПЕРСИСТЕНТНЫ между вызовами, а исполняются тогда, когда
агент обращается к серверу (лениво — при любом вызове) или явным вызовом
run_due(). Так «24/7» эмулируется персистентностью состояния, а не потоком.

Файл: session/mcp_jobs.json — структура:
{
  "updated": "ISO/локальное время последней записи",
  "servers": {
     "currency": {                       # изолированное пространство сервера
         "jobs": [ {job}, … ],           # задания с расписанием
         "data": {                       # накопленные точки (агрегация)
             "USD>RUB": [ {"t": "…", "rate": 84.2, "src": "job-id"}, … ]
         }
     },
     "calendar": {
         "jobs": [ {job}, … ],
         "data": {}                      # у календаря данные — сами напоминания
     }
  }
}

Задание (job):
{
  "id": "j1",                  # уникальный в рамках сервера
  "kind": "rate" | "reminder", # тип задания (смысл params/исполнителя)
  "params": { … },             # параметры (валюта/цель/событие/за сколько)
  "created": "…",              # когда создано
  "next_run": "…",             # когда исполнять в следующий раз ('' — разовое)
  "every_minutes": 1440,       # периодичность (0/отсутствует — разовое)
  "last_run": "…",             # когда исполнено в последний раз
  "runs": 0,                   # сколько раз исполнено
  "done": false,               # разовое задание исполнено — true
  "last_result": "…"           # текст последнего результата (для сводки)
}

Методы для исполнителей: `due_jobs()` (что созрело), `mark_run()` (простой
сдвиг/завершение) и `mark_fired(job_id, next_run)` (явный следующий момент;
пустой next_run → задание завершается).

Время хранится строками, локальное, в формате "%Y-%m-%d %H:%M:%S", что
удобно и для сравнения (лексикографически), и для чтения.
"""
import json
import os
import time
import uuid

# BASE_DIR — корень проекта (на два уровня выше этого файла: rtk_app/ -> корень).
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Путь к файлу заданий по умолчанию (можно переопределить переменной окружения
# MCP_JOBS_FILE — используется тестами для изоляции).
DEFAULT_JOBS_FILE = os.path.join(BASE_DIR, "session", "mcp_jobs.json")

TIME_FMT = "%Y-%m-%d %H:%M:%S"

__all__ = ["JobsStore", "now_str", "parse_time"]


def now_str():
    """Текущее локальное время строкой в формате TIME_FMT."""
    return time.strftime(TIME_FMT)


def parse_time(value):
    """Разбирает строку времени TIME_FMT в struct_time. None, если пусто/бито."""
    if not value:
        return None
    try:
        return time.strptime(str(value).strip(), TIME_FMT)
    except (TypeError, ValueError):
        return None


class JobsStore:
    """Потокобезопасное хранилище заданий расписания (JSON).

    Один файл на все MCP-серверы проекта, внутри — изолированные
    пространства по имени сервера (server). Каждое пространство хранит свои
    jobs и накопленные data (точки курсов и т.п.).
    """

    def __init__(self, path=None, server=None):
        # path — путь к файлу (по умолчанию session/mcp_jobs.json либо env).
        self.path = (path
                     or os.environ.get("MCP_JOBS_FILE")
                     or DEFAULT_JOBS_FILE)
        # server — пространство этого экземпляра (для удобства инструментов).
        self.server = server or "default"
        # RLock: в stdio-сервере вызовы однопоточные, но защищаемся на всякий.
        import threading
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Загрузка/сохранение файла
    # ------------------------------------------------------------------
    def _load(self):
        """Читает весь файл заданий. Возвращает dict (пустую структуру при ошибке)."""
        if not self.path or not os.path.isfile(self.path):
            return {"servers": {}}
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            # Битый файл не должен ронять сервер — начинаем с чистого.
            return {"servers": {}}
        if not isinstance(data, dict):
            return {"servers": {}}
        if not isinstance(data.get("servers"), dict):
            data["servers"] = {}
        return data

    def _save(self, data):
        """Атомарно пишет файл заданий (через временный файл + replace)."""
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        data["updated"] = now_str()
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as exc:
            print("[jobs] не удалось сохранить задания: %s" % exc, flush=True)

    def _space(self, data):
        """Возвращает (и при необходимости создаёт) пространство сервера."""
        servers = data.setdefault("servers", {})
        space = servers.get(self.server)
        if not isinstance(space, dict):
            space = {"jobs": [], "data": {}}
            servers[self.server] = space
        if not isinstance(space.get("jobs"), list):
            space["jobs"] = []
        if not isinstance(space.get("data"), dict):
            space["data"] = {}
        return space

    # ------------------------------------------------------------------
    # Задания
    # ------------------------------------------------------------------
    def add_job(self, kind, params, every_minutes=0, at=None, in_minutes=None):
        """Создаёт задание и сохраняет его. Возвращает dict-задание.

        every_minutes — периодичность в минутах (0 — разовое задание);
        at            — абсолютное ВРЕМЯ первого запуска ("YYYY-MM-DD HH:MM[:SS]");
        in_minutes    — первый запуск через N минут от текущего момента.
        Если для разового задания не задано ни at, ни in_minutes — оно
        считается «созревшим сразу» (next_run = сейчас).
        """
        with self._lock:
            data = self._load()
            space = self._space(data)
            jid = "j%d" % (len(space["jobs"]) + 1)
            existing = {j.get("id") for j in space["jobs"]}
            while jid in existing:
                jid = "j" + uuid.uuid4().hex[:6]
            next_run = self._first_run(every_minutes, at, in_minutes)
            job = {
                "id": jid,
                "kind": str(kind),
                "params": dict(params or {}),
                "created": now_str(),
                "next_run": next_run,
                "every_minutes": max(0, int(every_minutes or 0)),
                "last_run": "",
                "runs": 0,
                "done": False,
                "last_result": "",
            }
            space["jobs"].append(job)
            self._save(data)
            return dict(job)

    def _first_run(self, every_minutes, at, in_minutes):
        """Вычисляет время первого запуска (строка TIME_FMT)."""
        if at:
            return self._normalize_at(at)
        if in_minutes is not None:
            try:
                mins = float(in_minutes)
            except (TypeError, ValueError):
                mins = 0
            return time.strftime(TIME_FMT, time.localtime(time.time() + mins * 60))
        if every_minutes and int(every_minutes) > 0:
            # Периодическое без явного старта — первый запуск через один период.
            return time.strftime(
                TIME_FMT, time.localtime(time.time() + int(every_minutes) * 60))
        # Разовое без времени — созрело сразу.
        return now_str()

    @staticmethod
    def _normalize_at(value):
        """Приводит строку «at» к формату TIME_FMT (добавляет секунды при необх.)."""
        s = str(value).strip().replace("T", " ")
        for fmt in (TIME_FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return time.strftime(TIME_FMT, time.strptime(s, fmt))
            except ValueError:
                continue
        return s  # оставляем как есть — сравнение покажет, созрело ли.

    def list_jobs(self):
        """Список всех заданий этого сервера (копии)."""
        with self._lock:
            data = self._load()
            space = self._space(data)
            return [dict(j) for j in space["jobs"]]

    def get_job(self, job_id):
        """Возвращает задание по id (копию) или None."""
        for j in self.list_jobs():
            if j.get("id") == job_id:
                return j
        return None

    def delete_job(self, job_id):
        """Удаляет задание по id. True, если удалено."""
        with self._lock:
            data = self._load()
            space = self._space(data)
            before = len(space["jobs"])
            space["jobs"] = [j for j in space["jobs"]
                             if j.get("id") != job_id]
            removed = len(space["jobs"]) != before
            if removed:
                self._save(data)
            return removed

    def due_jobs(self, at_time=None):
        """Возвращает список СОЗРЕВШИХ заданий (next_run <= теперь) этого сервера.

        Разовые уже исполненные (done) не возвращаются.
        """
        ref = at_time or now_str()
        out = []
        for j in self.list_jobs():
            if j.get("done"):
                continue
            nr = j.get("next_run")
            if nr and str(nr) <= ref:
                out.append(j)
        return out

    def mark_run(self, job_id, result=""):
        """Отмечает задание исполненным: обновляет last_run/runs/next_run/done.

        Для периодического задания next_run сдвигается на every_minutes вперёд
        (несколько пропущенных периодов не «накапливаются» — берём следующий
        будущий момент). Для разового — задание помечается done.
        """
        with self._lock:
            data = self._load()
            space = self._space(data)
            for j in space["jobs"]:
                if j.get("id") != job_id:
                    continue
                j["last_run"] = now_str()
                j["runs"] = int(j.get("runs", 0)) + 1
                j["last_result"] = str(result or "")
                every = int(j.get("every_minutes", 0) or 0)
                if every > 0:
                    j["next_run"] = self._advance(j.get("next_run"), every)
                else:
                    j["done"] = True
                break
            self._save(data)

    def mark_fired(self, job_id, next_run, result=""):
        """Отмечает запуск задания с ЯВНО заданным следующим моментом.

        Используется исполнителем, когда правило повтора/остановки сложнее,
        чем «сдвинуть на every_minutes» (например, напоминания до события:
        повторяются, пока не наступит событие, затем завершаются).

        next_run — строка TIME_FMT следующего запуска; ПУСТАЯ строка/None
        означает «больше не запускать» — задание помечается done.
        done     — производное: True, если next_run пуст.
        """
        with self._lock:
            data = self._load()
            space = self._space(data)
            for j in space["jobs"]:
                if j.get("id") != job_id:
                    continue
                j["last_run"] = now_str()
                j["runs"] = int(j.get("runs", 0)) + 1
                j["last_result"] = str(result or "")
                if next_run:
                    j["next_run"] = str(next_run)
                    j["done"] = False
                else:
                    j["done"] = True
                break
            self._save(data)

    @staticmethod
    def _advance(next_run, every_minutes):
        """Следующий момент запуска: не раньше «сейчас», шаг — every_minutes."""
        base = parse_time(next_run)
        base_ts = time.mktime(base) if base else time.time()
        step = max(1, int(every_minutes)) * 60
        now = time.time()
        # Пропускаем все периоды, которые уже прошли (догоняем до будущего).
        ts = base_ts
        while ts <= now:
            ts += step
        # Если база вообще «в прошлом далеко» — считаем от «сейчас» + шаг.
        if ts <= now:
            ts = now + step
        return time.strftime(TIME_FMT, time.localtime(ts))

    # ------------------------------------------------------------------
    # Накопленные данные (точки для агрегации)
    # ------------------------------------------------------------------
    def add_point(self, series, point):
        """Дописывает точку в серию data[series] (список). Возвращает число точек."""
        with self._lock:
            data = self._load()
            space = self._space(data)
            arr = space["data"].get(series)
            if not isinstance(arr, list):
                arr = []
                space["data"][series] = arr
            arr.append(dict(point or {}))
            self._save(data)
            return len(arr)

    def get_series(self, series):
        """Возвращает копию списка точек серии data[series]."""
        with self._lock:
            data = self._load()
            space = self._space(data)
            arr = space["data"].get(series)
            return [dict(p) for p in arr] if isinstance(arr, list) else []

    def series_names(self):
        """Список имён серий, по которым есть накопленные данные."""
        with self._lock:
            data = self._load()
            space = self._space(data)
            return list(space["data"].keys())

    def clear_series(self, series=None):
        """Очищает одну серию (или все, если series=None)."""
        with self._lock:
            data = self._load()
            space = self._space(data)
            if series is None:
                space["data"] = {}
            else:
                space["data"].pop(series, None)
            self._save(data)
