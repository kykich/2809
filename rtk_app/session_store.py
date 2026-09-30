"""
Хранилище сессии диалога на диске (JSON в папке session/).

Диалог ведётся на сервере и периодически/поэтапно сохраняется в файл
session/session.json. При запуске сервера хранилище подхватывает ранее
сохранённую историю (если она есть), тем самым сохраняя непрерывность
беседы между запусками.

Структура файла:
{
  "updated": "ISO-время последнего сохранения",
  "count":   число сообщений,
  "messages":[ ... элементы диалога ... ],
    "compact": {
    "enabled": true,       // сжатие включено (автоматическое)
    "keep": 0,             // сколько последних сообщений хранить полностью
    "summary": "",         // сжатое содержание ранней части истории
    "upto": 0              // сколько первых сообщений УЖЕ покрыты summary
  },
  "strategy": "none",       // активная стратегия управления контекстом
  "window": 10,             // окно N для Sliding/Facts
  "facts": {"цель": "…"},   // key-value память (стратегия Facts)
  "active_branch": 0,       // индекс активной ветки
  "branches": [ {           // ветки диалога (стратегия Branch)
      "name": "main",
      "messages": [ … ]     // собственные сообщения ветки
  } ],
  "memory": {               // ПАМЯТЬ АГЕНТА — три типа, хранятся ОТДЕЛЬНО
      "working":  {"ключ": "значение", …},   // рабочая: текущая задача/шаги
      "longterm": {"ключ": "значение", …}    // долговременная: профиль/решения
  }
}

Память агента — ТРИ независимых типа (задание A), которые хранятся РАЗДЕЛЬНО
(задание B1) и заполняются ЯВНО, с выбором «что и куда» (задание B2):
  * "short"    — краткосрочная: срез текущего диалога (messages / активная
                 ветка). Заполняется автоматически каждым ходом; в JSON-поле
                 memory НЕ дублируется, т.к. короткая память = сам диалог;
  * "working"  — рабочая: key-value данные ТЕКУЩЕЙ ЗАДАЧИ (активная задача,
                 подцели, шаги, статус). Пишется явно через set_memory();
  * "longterm" — долговременная: key-value профиль пользователя, принятые
                 решения и знания. Пишется явно через set_memory().

Элемент диалога (один ход):
  - пользователь: {"role": "user",  "content": "текст вопроса"}
  - ассистент:    {"role": "assistant", "content": "текст-конкат ответов",
                   "html": "готовая HTML-разметка ответа",
                   "answers": [{"label": "…", model/text…}, …]}
"""
import json
import os
import threading
import time

from . import config
from .task_state import TaskState
from .rag import RagStore

__all__ = ["SessionStore"]

VALID_STRATEGIES = ("none", "sliding", "facts", "branch")


class SessionStore:
    """Потокобезопасное хранилище одного активного диалога в JSON-файле."""

    def __init__(self, path=None):
        # path — путь к ФАЙЛУ СЕССИИ (легаси-формат). Если задан явно (напр.
        # во временном файле тестов), профили хранятся в ТОМ ЖЕ файле — так
        # каждый тест изолирован. Если path не задан (продакшн) — профили
        # идут в config.PROFILES_FILE, а легаси-сессия — в config.SESSION_FILE.
        explicit_path = path is not None
        self.path = path or config.SESSION_FILE
        self.lock = threading.RLock()
        self.messages = []
        self.compact = self._default_compact()
        # Стратегия управления контекстом.
        self.strategy = config.STRATEGY
        self.window = int(config.STRATEGY_WINDOW)
        # Факты (стратегия Facts) — НЕ отдельное хранилище: они живут в памяти
        # агента (memory_working / memory_longterm). Отдельного self.facts нет.
        # Ветки диалога (стратегия Branch). По умолчанию одна основная ветка.
        self.branches = [{"name": "main", "messages": []}]
        self.active_branch = 0
        # ПАМЯТЬ АГЕНТА: два ЯВНО заполняемых key-value слоя — рабочая
        # (данные текущей задачи) и долговременная (профиль/решения/знания).
        # Краткосрочная память = self.messages (диалог), поэтому отдельно
        # НЕ хранится — это и есть разделение типов (задание B1).
        self.memory_working = {}
        self.memory_longterm = {}
        # СОСТОЯНИЕ ЗАДАЧИ (Task State Machine): формализованный автомат
        # «этап -> шаг -> ожидаемое действие» с паузой/продолжением.
        self.task_state = TaskState()
        # ИНВАРИАНТЫ: правила (с категориями), которые ассистент НЕ вправе
        # нарушать. Хранятся ОТДЕЛЬНО от диалога (как task_state/память),
        # живут вместе с профилем. Каждый элемент: {"text": str,
        # "category": "architecture"|"tech"|"stack"|"business"}.
        self.invariants = []
        # СЧЁТЧИКИ ИСПОЛЬЗОВАНИЯ памяти: сколько фрагментов ответов моделей
        # было заимствовано из рабочей/долговременной памяти (сумма за сессию).
        self.memory_use = {"working": 0, "longterm": 0}
        # Сколько РАЗ (за сколько обменов) каждый вид памяти был задействован.
        self.memory_use_count = {"working": 0, "longterm": 0}
        # ПРОФИЛИ (персоны). self.profiles — список dict-профилей (источник
        # истины), self.active_profile — идентификатор активного профиля.
        # Все поля self.messages/memory_*/… — это СОСТОЯНИЕ АКТИВНОГО профиля
        # (снимок); при переключении профиля они заменяются.
        self.profiles = []
        self.active_profile = None
        self.profiles_path = self.path if explicit_path else config.PROFILES_FILE
        # RAG (Retrieval-Augmented Generation): индекс PDF-документов в SQLite
        # + настройки (enabled/docs_dir/top_k) в JSON. Настройки RAG — НЕ
        # профильные: они общие для сервера (как MCP), поэтому хранятся
        # отдельно от профилей. При вызове во временном файле (тесты) держим
        # индекс рядом с ним, чтобы тесты не трогали продакшн-индекс.
        if explicit_path:
            base = os.path.splitext(self.path)[0]
            self.rag = RagStore(index_file=base + "_rag.db",
                                settings_file=base + "_rag.json")
        else:
            self.rag = RagStore()
        self.load()

    @staticmethod
    def _default_compact():
        return {
            "enabled": bool(config.COMPACT_ENABLED),
            "keep": config.COMPACT_KEEP,
            "summary": "",
            "upto": 0,
        }

    # ==================================================================
    # ПРОФИЛИ (персоны)
    # ==================================================================
    # Профиль — именованный набор состояния: своя память (рабочая +
    # долговременная), свой диалог (messages/ветки), настройки сжатия и
    # стратегии, а также ХАРАКТЕР (тон) и ХАРАКТЕР ОТВЕТОВ (формат/длина),
    # задаваемые при создании. self.profiles — источник истины; поля
    # self.messages/memory_*/… отражают ТЕКУЩИЙ (активный) профиль.

    @staticmethod
    def _new_profile_dict(pid, name, character="", style="", model=""):
        """Создаёт словарь нового пустого профиля.

        model — метка модели (например, "GigaChat"), с которой работает
        персона: ответы персоны генерирует именно эта модель.
        """
        return {
            "id": str(pid),
            "name": str(name or "Профиль")[: int(config.PROFILE_NAME_CAP)],
            "model": str(model or "")[: int(config.PROFILE_NAME_CAP)],
            "character": str(character or "")[: int(config.PROFILE_ATTR_CAP)],
            "style": str(style or "")[: int(config.PROFILE_ATTR_CAP)],
            # Память профиля (изолирована от других профилей).
            "memory_working": {},
            "memory_longterm": {},
            # Состояние задачи профиля (Task State Machine).
            "task_state": {},
            # Инварианты профиля (отдельно от диалога и памяти).
            "invariants": [],
            # Диалог и ветки профиля.
            "messages": [],
            "branches": [{"name": "main", "messages": []}],
            "active_branch": 0,
            # Настройки контекста профиля.
            "compact": {
                "enabled": bool(config.COMPACT_ENABLED),
                "keep": config.COMPACT_KEEP,
                "summary": "",
                "upto": 0,
            },
            "strategy": config.STRATEGY,
            "window": int(config.STRATEGY_WINDOW),
            # Счётчики использования памяти профиля.
            "memory_use": {"working": 0, "longterm": 0},
            "memory_use_count": {"working": 0, "longterm": 0},
        }

    def _capture_locked(self):
        """Копирует текущее состояние (self.*) в активный профиль (в список)."""
        if not self.active_profile:
            return
        for p in self.profiles:
            if p["id"] == self.active_profile:
                p["memory_working"] = dict(self.memory_working)
                p["memory_longterm"] = dict(self.memory_longterm)
                p["task_state"] = self.task_state.to_dict()
                p["invariants"] = [dict(i) for i in self.invariants]
                p["messages"] = list(self.messages)
                p["branches"] = [{"name": b["name"],
                                  "messages": list(b["messages"])}
                                 for b in self.branches]
                p["active_branch"] = self.active_branch
                p["compact"] = dict(self.compact)
                p["strategy"] = self.strategy
                p["window"] = self.window
                p["memory_use"] = dict(self.memory_use)
                p["memory_use_count"] = dict(self.memory_use_count)
                return

    def _apply_locked(self, profile):
        """Загружает состояние профиля в текущие поля self.*."""
        self.active_profile = profile["id"]
        self.memory_working = dict(profile.get("memory_working") or {})
        self.memory_longterm = dict(profile.get("memory_longterm") or {})
        self.task_state = TaskState(profile.get("task_state"))
        inv = profile.get("invariants")
        self.invariants = [dict(i) for i in inv] if isinstance(inv, list) else []
        self.messages = list(profile.get("messages") or [])
        branches = profile.get("branches")
        if isinstance(branches, list) and branches:
            self.branches = [{"name": str(b.get("name", "branch")),
                              "messages": list(b.get("messages") or [])}
                             for b in branches]
        else:
            self.branches = [{"name": "main", "messages": list(self.messages)}]
        try:
            ab = int(profile.get("active_branch", 0))
        except (TypeError, ValueError):
            ab = 0
        self.active_branch = ab if 0 <= ab < len(self.branches) else 0
        comp = profile.get("compact")
        self.compact = dict(comp) if isinstance(comp, dict) \
            else self._default_compact()
        strat = profile.get("strategy")
        self.strategy = strat if strat in VALID_STRATEGIES else config.STRATEGY
        try:
            self.window = max(0, int(profile.get("window",
                                                 config.STRATEGY_WINDOW)))
        except (TypeError, ValueError):
            self.window = int(config.STRATEGY_WINDOW)
        mused = profile.get("memory_use")
        self.memory_use = {"working": int((mused or {}).get("working", 0) or 0),
                           "longterm": int((mused or {}).get("longterm", 0) or 0)}
        mcount = profile.get("memory_use_count")
        self.memory_use_count = {
            "working": int((mcount or {}).get("working", 0) or 0),
            "longterm": int((mcount or {}).get("longterm", 0) or 0)}

    def _reset_active_state_locked(self):
        """Сбрасывает состояние активного профиля в «пусто» (профилей нет)."""
        self.active_profile = None
        self.messages = []
        self.compact = self._default_compact()
        self.strategy = config.STRATEGY
        self.window = int(config.STRATEGY_WINDOW)
        self.branches = [{"name": "main", "messages": []}]
        self.active_branch = 0
        self.memory_working = {}
        self.memory_longterm = {}
        self.task_state = TaskState()
        self.invariants = []
        self.memory_use = {"working": 0, "longterm": 0}
        self.memory_use_count = {"working": 0, "longterm": 0}

    def _ensure_profile_locked(self):
        """Согласует ссылку на активный профиль со списком профилей.

        ВАЖНО: по умолчанию профилей НЕТ (список пуст, active_profile=None).
        Профиль создаётся пользователем явно (кнопкой «Создать профиль»).
        Здесь мы лишь следим, чтобы active_profile указывал на существующий
        профиль (или оставался None, если профилей нет).
        """
        ids = [p["id"] for p in self.profiles]
        if self.active_profile not in ids:
            self.active_profile = ids[0] if ids else None

    def _profile_by_id_locked(self, pid):
        for p in self.profiles:
            if p["id"] == pid:
                return p
        return None

    def profiles_state(self):
        """Снимок профилей для интерфейса.

        Возвращает dict:
          profiles — список {id, name, model, character, style, active, size};
          active   — id активного профиля.
        """
        with self.lock:
            self._capture_locked()
            out = []
            for p in self.profiles:
                out.append({
                    "id": p["id"],
                    "name": p["name"],
                    "model": p.get("model", ""),
                    "character": p.get("character", ""),
                    "style": p.get("style", ""),
                    "active": p["id"] == self.active_profile,
                    "size": len(p.get("messages") or []),
                })
            return {"profiles": out, "active": self.active_profile}

    def create_profile(self, name, character="", style="", model="",
                       activate=True):
        """Создаёт новый профиль (со своей пустой памятью) и сохраняет.

        name, character, style, model задаются при создании. model — метка
        модели, которой будет отвечать персона. Если activate=True — новый
        профиль становится активным (память/диалог переключаются на него).
        """
        with self.lock:
            self._ensure_profile_locked()
            name = str(name or "").strip()
            if not name:
                raise ValueError("Пустое имя профиля.")
            if len(self.profiles) >= int(config.PROFILE_MAX):
                raise ValueError("Достигнут предел числа профилей (%d)."
                                 % int(config.PROFILE_MAX))
            # Генерируем уникальный id.
            existing = {p["id"] for p in self.profiles}
            n = len(self.profiles) + 1
            pid = "p%d" % n
            while pid in existing:
                n += 1
                pid = "p%d" % n
            prof = self._new_profile_dict(pid, name, character, style, model)
            # Новый профиль наследует текущую стратегию/сжатие (но не память).
            prof["strategy"] = self.strategy
            prof["window"] = self.window
            self.profiles.append(prof)
            if activate:
                self._capture_locked()
                self._apply_locked(prof)
            self._save_locked()
            return self.profiles_state()

    def update_profile(self, pid, name=None, character=None, style=None,
                       model=None):
        """Обновляет имя/характер/стиль/модель профиля (память не трогает)."""
        with self.lock:
            p = self._profile_by_id_locked(pid)
            if p is None:
                raise ValueError("Профиль не найден: %r" % (pid,))
            if name is not None:
                nm = str(name).strip()
                if not nm:
                    raise ValueError("Пустое имя профиля.")
                p["name"] = nm[: int(config.PROFILE_NAME_CAP)]
            if model is not None:
                p["model"] = str(model)[: int(config.PROFILE_NAME_CAP)]
            if character is not None:
                p["character"] = str(character)[: int(config.PROFILE_ATTR_CAP)]
            if style is not None:
                p["style"] = str(style)[: int(config.PROFILE_ATTR_CAP)]
            self._save_locked()
            return self.profiles_state()

    def delete_profile(self, pid):
        """Удаляет профиль. Удалить можно и последний — тогда профилей снова нет."""
        with self.lock:
            p = self._profile_by_id_locked(pid)
            if p is None:
                return self.profiles_state()
            self.profiles = [x for x in self.profiles if x["id"] != pid]
            if self.active_profile == pid or not self.profiles:
                # Активным становится первый оставшийся, либо профиля нет.
                if self.profiles:
                    self._apply_locked(self.profiles[0])
                else:
                    self.active_profile = None
                    self._reset_active_state_locked()
            self._save_locked()
            return self.profiles_state()

    def switch_profile(self, pid):
        """Переключает активный профиль: заменяет память и диалог."""
        with self.lock:
            self._ensure_profile_locked()
            p = self._profile_by_id_locked(pid)
            if p is None:
                raise ValueError("Профиль не найден: %r" % (pid,))
            self._capture_locked()          # сохраняем текущий профиль
            self._apply_locked(p)           # загружаем выбранный
            self._save_locked()
            return self.profiles_state()

    def active_profile_attrs(self):
        """Возвращает (id, name, model, character, style) активного профиля."""
        with self.lock:
            self._ensure_profile_locked()
            p = self._profile_by_id_locked(self.active_profile)
            if p is None:
                return (None, "", "", "", "")
            return (p["id"], p["name"], p.get("model", ""),
                    p.get("character", ""), p.get("style", ""))

    # ---- чтение ----
    def load(self):
        """Читает сохранённые профили и сессию из файлов.

        Основной источник — profiles.json (список профилей + активный).
        ВАЖНО: по умолчанию профилей НЕТ — пользователь создаёт их сам.
        Легаси-сессию (session.json) подхватываем ТОЛЬКО если профилей ещё
        не было и в файле есть реальная история: тогда создаём один профиль
        и переносим в него сохранённую память/диалог (миграция).
        """
        with self.lock:
            self.messages = []
            self.compact = self._default_compact()
            self.strategy = config.STRATEGY
            self.window = int(config.STRATEGY_WINDOW)
            self.branches = [{"name": "main", "messages": []}]
            self.active_branch = 0
            self.memory_working = {}
            self.memory_longterm = {}
            self.task_state = TaskState()
            self.memory_use = {"working": 0, "longterm": 0}
            self.memory_use_count = {"working": 0, "longterm": 0}
            self.profiles = []
            self.active_profile = None
            # Сначала пробуем загрузить профили.
            if self._load_profiles_locked():
                return
            # Профилей нет — читаем старую одиночную сессию (если есть) и,
            # ТОЛЬКО если в ней реально что-то сохранено, переносим её в
            # один профиль (миграция). Пустой файл профиля не создаёт.
            self._legacy_profile_attrs = {}
            if self.path and os.path.isfile(self.path):
                self._load_legacy_session_locked()
            if self._legacy_has_content():
                legacy_attrs = self._legacy_profile_attrs
                self.profiles = [self._new_profile_dict(
                    "default",
                    legacy_attrs.get("name", "Профиль 1"),
                    legacy_attrs.get("character", ""),
                    legacy_attrs.get("style", ""))]
                self.active_profile = "default"
                # Переносим загруженное (из старой сессии) в профиль.
                self._capture_locked()

    def _legacy_has_content(self):
        """Есть ли в загруженной легаси-сессии реальные данные для миграции."""
        if self.messages:
            return True
        if self.memory_working or self.memory_longterm:
            return True
        if self.compact.get("summary"):
            return True
        return False

    def _load_profiles_locked(self):
        """Загружает profiles.json в self.profiles и активирует сохранённый.

        Возвращает True, если файл существует и успешно прочитан.
        """
        self._legacy_profile_attrs = {}
        if not self.profiles_path or not os.path.isfile(self.profiles_path):
            return False
        try:
            with open(self.profiles_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return False
        profiles = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(profiles, list) or not profiles:
            return False
        clean = []
        for p in profiles:
            if not isinstance(p, dict):
                continue
            pid = str(p.get("id") or "").strip()
            if not pid:
                continue
            prof = self._new_profile_dict(
                pid, p.get("name", "Профиль"),
                p.get("character", ""), p.get("style", ""),
                p.get("model", ""))
            # Память.
            mw = p.get("memory_working")
            if isinstance(mw, dict):
                prof["memory_working"] = {str(k): str(v) for k, v in mw.items()}
            ml = p.get("memory_longterm")
            if isinstance(ml, dict):
                prof["memory_longterm"] = {str(k): str(v) for k, v in ml.items()}
            # Состояние задачи профиля (Task State Machine).
            ts = p.get("task_state")
            if isinstance(ts, dict):
                prof["task_state"] = TaskState(ts).to_dict()
            # Инварианты профиля (отдельно от диалога и памяти).
            inv = p.get("invariants")
            if isinstance(inv, list):
                prof["invariants"] = [dict(i) for i in inv
                                      if isinstance(i, dict)]
            # Диалог.
            msgs = p.get("messages")
            if isinstance(msgs, list):
                prof["messages"] = [m for m in msgs
                                    if isinstance(m, dict)
                                    and m.get("role") in ("user", "assistant")]
            br = p.get("branches")
            if isinstance(br, list) and br:
                prof["branches"] = [
                    {"name": str(b.get("name", "branch")),
                     "messages": [m for m in (b.get("messages") or [])
                                  if isinstance(m, dict)
                                  and m.get("role") in ("user", "assistant")]}
                    for b in br if isinstance(b, dict)]
            try:
                prof["active_branch"] = int(p.get("active_branch", 0))
            except (TypeError, ValueError):
                prof["active_branch"] = 0
            # Настройки.
            comp = p.get("compact")
            if isinstance(comp, dict):
                prof["compact"].update({
                    "enabled": bool(comp.get("enabled",
                                             config.COMPACT_ENABLED)),
                    "keep": int(comp.get("keep", config.COMPACT_KEEP)),
                    "summary": str(comp.get("summary", "")),
                    "upto": int(comp.get("upto", 0)),
                })
            strat = p.get("strategy")
            if strat in VALID_STRATEGIES:
                prof["strategy"] = strat
            try:
                prof["window"] = max(0, int(p.get("window",
                                                  config.STRATEGY_WINDOW)))
            except (TypeError, ValueError):
                pass
            mu = p.get("memory_use")
            if isinstance(mu, dict):
                prof["memory_use"] = {
                    "working": int(mu.get("working", 0) or 0),
                    "longterm": int(mu.get("longterm", 0) or 0)}
            mc = p.get("memory_use_count")
            if isinstance(mc, dict):
                prof["memory_use_count"] = {
                    "working": int(mc.get("working", 0) or 0),
                    "longterm": int(mc.get("longterm", 0) or 0)}
            clean.append(prof)
        if not clean:
            return False
        self.profiles = clean
        active = data.get("active")
        ids = [p["id"] for p in self.profiles]
        self.active_profile = active if active in ids else self.profiles[0]["id"]
        # Загружаем состояние активного профиля в self.*.
        self._apply_locked(self._profile_by_id_locked(self.active_profile))
        return True

    def _load_legacy_session_locked(self):
        """Читает старую единичную session.json (миграция в профиль)."""
        self._legacy_profile_attrs = {}
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            msgs = data.get("messages") if isinstance(data, dict) else None
            if isinstance(msgs, list):
                # оставляем только корректные элементы диалога
                self.messages = [m for m in msgs
                                 if isinstance(m, dict)
                                 and m.get("role") in ("user", "assistant")]
            # Загружаем настройки сжатия
            compact = data.get("compact")
            if isinstance(compact, dict):
                self.compact["enabled"] = bool(compact.get(
                    "enabled", config.COMPACT_ENABLED))
                self.compact["keep"] = int(compact.get("keep", config.COMPACT_KEEP))
                self.compact["summary"] = str(compact.get("summary", ""))
                self.compact["upto"] = int(compact.get("upto", 0))
                # Согласованность: если summary есть, но граница покрытия
                # не задана (старый файл без поля upto) — выводим её из
                # правила «всё, кроме последних keep сообщений».
                # При keep = 0 сжатие не применяется — границу не выводим.
                if (self.compact["summary"] and self.compact["upto"] == 0
                        and self.compact["keep"] > 0):
                    keep = max(0, self.compact["keep"])
                    self.compact["upto"] = max(0, len(self.messages) - keep)
            # Стратегия управления контекстом.
            strat = data.get("strategy")
            if isinstance(strat, str) and strat in VALID_STRATEGIES:
                self.strategy = strat
            try:
                self.window = int(data.get("window", config.STRATEGY_WINDOW))
            except (TypeError, ValueError):
                self.window = int(config.STRATEGY_WINDOW)
            if self.window < 0:
                self.window = 0
            # Facts (key-value память) — устаревшее поле для совместимости.
            # Загружаем их ТОЛЬКО если отдельная память пуста (миграция
            # старых файлов: факты переезжают в рабочую память).
            legacy_facts = data.get("facts")
            if isinstance(legacy_facts, dict):
                legacy_facts = {str(k): str(v) for k, v in legacy_facts.items()}
            else:
                legacy_facts = {}
            # Память агента: рабочая и долговременная (key-value).
            # Читаем ЯВНО из отдельного блока memory — типы не смешиваются.
            memory = data.get("memory")
            if isinstance(memory, dict):
                working = memory.get("working")
                if isinstance(working, dict):
                    self.memory_working = {str(k): str(v)
                                           for k, v in working.items()}
                longterm = memory.get("longterm")
                if isinstance(longterm, dict):
                    self.memory_longterm = {str(k): str(v)
                                            for k, v in longterm.items()}
            # Миграция: старые файлы хранили факты отдельно — переносим в
            # рабочую память, если её нет.
            if legacy_facts and not self.memory_working \
                    and not self.memory_longterm:
                self.memory_working = dict(legacy_facts)
            # Состояние задачи (Task State Machine) из плоского поля.
            task_state = data.get("task_state")
            if isinstance(task_state, dict):
                self.task_state = TaskState(task_state)
            # Счётчики использования памяти (за сессию).
            mused = data.get("memory_use")
            if isinstance(mused, dict):
                try:
                    self.memory_use["working"] = max(
                        0, int(mused.get("working", 0) or 0))
                    self.memory_use["longterm"] = max(
                        0, int(mused.get("longterm", 0) or 0))
                except (TypeError, ValueError):
                    self.memory_use = {"working": 0, "longterm": 0}
            # Сколько РАЗ вид памяти был задействован (за сессию).
            mcount = data.get("memory_use_count")
            if isinstance(mcount, dict):
                try:
                    self.memory_use_count["working"] = max(
                        0, int(mcount.get("working", 0) or 0))
                    self.memory_use_count["longterm"] = max(
                        0, int(mcount.get("longterm", 0) or 0))
                except (TypeError, ValueError):
                    self.memory_use_count = {"working": 0, "longterm": 0}
            # Ветки диалога (стратегия Branch).
            branches = data.get("branches")
            if isinstance(branches, list) and branches:
                clean = []
                for b in branches:
                    if not isinstance(b, dict):
                        continue
                    bmsgs = b.get("messages")
                    bmsgs = [m for m in bmsgs if isinstance(m, dict)
                             and m.get("role") in ("user", "assistant")] \
                        if isinstance(bmsgs, list) else []
                    clean.append({"name": str(b.get("name", "branch")),
                                  "messages": bmsgs})
                if clean:
                    self.branches = clean
            try:
                ab = int(data.get("active_branch", 0))
            except (TypeError, ValueError):
                ab = 0
            self.active_branch = ab if 0 <= ab < len(self.branches) else 0
            # При активной стратегии Branch сообщения берём из активной ветки.
            if self.strategy == "branch":
                self.messages = list(self.branches[self.active_branch]["messages"])
        except Exception:
            # Битый файл не должен ронять сервер — стартуем с чистой историей
            self.messages = []

    def snapshot(self):
        """Копия списка сообщений текущей сессии (активной ветки)."""
        with self.lock:
            return list(self.messages)

    def snapshot_full(self):
        """Полная копия сессии: сообщения + настройки сжатия и стратегии."""
        with self.lock:
            return {
                "messages": list(self.messages),
                "compact": dict(self.compact),
                "strategy": self.strategy,
                "window": self.window,
                "facts": self.get_facts(),
                "branches": [{"name": b["name"], "messages": list(b["messages"])}
                             for b in self.branches],
                "active_branch": self.active_branch,
                "memory": self.memory_state(),
                "invariants": [dict(i) for i in self.invariants],
            }

    def full_state(self):
        """Единый снимок состояния сессии для API (/api/session и /api/ask).

        Возвращает dict со всеми полями, которые клиент обновляет после
        каждого действия: messages/compact/context/strategy/facts/branches/
        memory/task/invariants/profiles. Единая точка сборки устраняет риск,
        что разные обработчики вернут РАЗНЫЙ набор полей и клиент с сессией
        разойдутся (см. docs/OPTIMIZATION_REVIEW.md, §1.4).

        Все геттеры потокобезопасны; каждый берёт self.lock отдельно (RLock),
        поэтому вызов изнутри уже захваченной блокировки безопасен.
        """
        return {
            "messages": self.snapshot(),
            "compact": self.get_compact(),
            "context": self.context_stats(),
            "strategy": self.get_strategy(),
            "facts": self.get_facts(),
            "branches": self.branches_state(),
            "memory": self.memory_state(),
            "task": self.get_task_state(),
            "invariants": self.invariants_state(),
            "profiles": self.profiles_state(),
            "rag": self.rag_status(),
        }

    # ==================================================================
    # RAG (Retrieval-Augmented Generation)
    # ==================================================================
    # Тонкие обёртки над RagStore: единая точка доступа из server/agent и
    # включение снимка RAG в full_state() (как у MCP/facts/веток).
    def rag_status(self):
        """Снимок состояния RAG для интерфейса (enabled/docs_dir/top_k/…)."""
        try:
            return self.rag.status()
        except Exception:                        # noqa: BLE001
            return {"enabled": bool(config.RAG_ENABLED),
                    "docs_dir": config.RAG_DOCS_DIR,
                    "top_k": config.RAG_TOP_K,
                    "embed_model": config.RAG_EMBED_MODEL,
                    "files": 0, "chunks": 0, "available": False,
                    "errors": []}

    def rag_settings(self):
        """Текущие настройки RAG (enabled/docs_dir/top_k/…)."""
        return self.rag.load_settings()

    def set_rag_settings(self, enabled=None, docs_dir=None, top_k=None):
        """Меняет настройки RAG. Возвращает обновлённый снимок статуса."""
        self.rag.update_settings(enabled=enabled, docs_dir=docs_dir,
                                 top_k=top_k)
        return self.rag_status()

    def rag_reindex(self, docs_dir=None):
        """(Пере)индексация папки PDF. Возвращает отчёт прогона."""
        with self.lock:
            return self.rag.index_docs(docs_dir=docs_dir)

    def rag_clear(self):
        """Очистка RAG-индекса. Возвращает обновлённый снимок статуса."""
        with self.lock:
            self.rag.clear()
            return self.rag_status()

    def rag_context(self, question):
        """Контекст RAG для запроса: (текст, фрагменты) или ("", []).

        Выполняется ТОЛЬКО если RAG включён и в индексе есть чанки. Ошибки
        эмбеддингов/поиска не должны ломать ответ модели — возвращаем пусто.
        """
        try:
            settings = self.rag.load_settings()
            if not settings.get("enabled"):
                return "", []
            if not self.rag.status().get("available"):
                return "", []
            return self.rag.build_context(question,
                                          top_k=settings.get("top_k"),
                                          docs_dir=settings.get("docs_dir"))
        except Exception as exc:                 # noqa: BLE001
            print("[RAG] поиск не выполнен: %s" % exc, flush=True)
            return "", []

    def has_history(self):
        """True, если в сессии уже есть сохранённый диалог."""
        with self.lock:
            return bool(self.messages)

    # ---- запись ----
    def append_turn(self, question, assistant_entry):
        """Добавляет в историю ход «вопрос -> ответ(ы)» и сохраняет на диск."""
        with self.lock:
            user_msg = {"role": "user", "content": str(question)}
            entry = dict(assistant_entry or {})
            entry.setdefault("role", "assistant")
            self.messages.append(user_msg)
            self.messages.append(entry)
            # При стратегии Branch история живёт в активной ветке.
            if self.strategy == "branch":
                self.branches[self.active_branch]["messages"] = list(self.messages)
            self._save_locked()

    def save(self):
        """Принудительное сохранение текущего состояния на диск."""
        with self.lock:
            self._save_locked()

    def _save_locked(self):
        # Синхронизируем активную ветку с текущей историей.
        if self.strategy == "branch" and \
                0 <= self.active_branch < len(self.branches):
            self.branches[self.active_branch]["messages"] = list(self.messages)
        # Переносим текущее состояние в активный профиль и сохраняем ВСЕ
        # профили в profiles.json — это основной источник состояния.
        self._ensure_profile_locked()
        self._capture_locked()
        self._save_profiles_locked()

    def _active_snapshot_locked(self):
        """«Плоский» снимок АКТИВНОГО профиля (общий для обоих писателей).

        Единая сборка словаря состояния (messages, compact, strategy, memory,
        task_state, …, facts, branches) используется и в _save_profiles_locked
        (как совместимые плоские поля), и в _save_legacy_locked — устранение
        ~50 строк дублирования (см. docs/OPTIMIZATION_REVIEW.md, §1.2).
        Вызывается при уже захваченном self.lock.
        """
        return {
            "count": len(self.messages),
            "messages": list(self.messages),
            "compact": dict(self.compact),
            "strategy": self.strategy,
            "window": self.window,
            "facts": self.get_facts(),
            "branches": [{"name": b["name"], "messages": list(b["messages"])}
                         for b in self.branches],
            "active_branch": self.active_branch,
            "memory": {"working": dict(self.memory_working),
                       "longterm": dict(self.memory_longterm)},
            "task_state": self.task_state.to_dict(),
            "memory_use": dict(self.memory_use),
            "memory_use_count": dict(self.memory_use_count),
        }

    @staticmethod
    def _write_json_locked(path, data, what):
        """Атомарно пишет data (JSON) в путь path. Логирует, но не бросает."""
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except Exception as exc:                    # не роняем запрос из-за диска
            print("[session] не удалось сохранить %s: %s" % (what, exc),
                  flush=True)

    def _save_profiles_locked(self):
        """Записывает profiles.json (все профили + активный).

        ДОПОЛНИТЕЛЬНО, для обратной совместимости и удобства отладки, в файл
        кладутся «плоские» поля АКТИВНОГО профиля (messages, compact, memory,
        strategy, branches, …): так старые инструменты/тесты, читающие
        session.json напрямую, продолжают видеть состояние активного профиля.
        """
        path = self.profiles_path or config.PROFILES_FILE
        data = {"updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                "active": self.active_profile,
                "profiles": self.profiles}
        data.update(self._active_snapshot_locked())
        self._write_json_locked(path, data, "профили")
        # ДОПОЛНИТЕЛЬНО пишем ЛЕГАСИ-файл сессии (self.path), если он
        # отличается от файла профилей: старые инструменты/тесты, читающие
        # session.json напрямую, продолжают видеть состояние активного
        # профиля в привычном формате.
        if self.path and os.path.abspath(self.path) != os.path.abspath(path):
            self._save_legacy_locked()

    def _save_legacy_locked(self):
        """Пишет легаси-формат session.json (плоские поля активного профиля)."""
        data = {"updated": time.strftime("%Y-%m-%d %H:%M:%S")}
        data.update(self._active_snapshot_locked())
        self._write_json_locked(self.path, data, "сессию")

    # ---- сброс ----
    def reset(self):
        """Очищает сессию: начинает новый разговор с чистого листа.

        Краткосрочную память (диалог) и РАБОЧУЮ память текущей задачи чистим —
        они относятся к завершаемому разговору. ДОЛГОВРЕМЕННУЮ память
        (профиль/решения/знания) СОХРАНЯЕМ: она переносится между сессиями.
        """
        with self.lock:
            self.messages = []
            self.compact = self._default_compact()
            self.branches = [{"name": "main", "messages": []}]
            self.active_branch = 0
            # Рабочая память завершённой задачи больше не нужна.
            self.memory_working = {}
            # Состояние задачи сбрасываем — начинается новый разговор.
            self.task_state = TaskState()
            # Счётчики использования памяти — обнуляем для нового разговора.
            self.memory_use = {"working": 0, "longterm": 0}
            self.memory_use_count = {"working": 0, "longterm": 0}
            # Стратегию (strategy/window) и долговременную память НЕ сбрасываем.
            self._save_locked()

    # ---- Настройки сжатия ----

    def set_compact(self, enabled, keep=None, summary=None, upto=None):
        """Обновляет настройки сжатия и сохраняет на диск."""
        with self.lock:
            if enabled is not None:
                self.compact["enabled"] = bool(enabled)
            if keep is not None:
                self.compact["keep"] = int(keep)
            if summary is not None:
                self.compact["summary"] = str(summary)
            if upto is not None:
                self.compact["upto"] = int(upto)
            self._save_locked()

    def get_compact(self):
        """Возвращает текущие настройки сжатия (копия)."""
        with self.lock:
            return dict(self.compact)

    def get_compacted_messages(self):
        """Возвращает историю для отправки в запрос (сжатие подставлено).

        Правило управления контекстом:
          * если сжатие включено и есть summary — в запрос уходят
            [summary] + последние keep сообщений (как есть);
          * сообщения, уже покрытые summary (индекс < upto), в запрос
            НЕ попадают — вместо них идёт summary;
          * если сжатие выключено — возвращаются все сообщения как есть.
        """
        with self.lock:
            return self._apply_summary_over(list(self.messages))

    def context_stats(self):
        """Статистика управления контекстом (для интерфейса).

        Возвращает dict:
          total      — всего сообщений в полной истории;
          keep       — сколько последних сообщений передаётся полностью;
          compacted  — сколько сообщений покрыто summary (сжато), >= 0;
          sent       — сколько сообщений реально уйдёт в запрос
                       (summary + последние keep);
          from_summary — сколько сообщений заменено summary в запросе
                       (это же число = compacted, т.е. «использовано из summary»);
          has_summary  — сгенерирован ли summary вообще;
          summary_len  — длина текста summary.
        """
        with self.lock:
            total = len(self.messages)
            keep = max(0, self.compact["keep"])
            upto = max(0, self.compact.get("upto", 0))
            # keep = 0 → сжатие не применяется (ни подстановка, ни покрытие).
            has_summary = bool(self.compact["enabled"] and self.compact["summary"]
                               and keep > 0)
            if has_summary:
                # Граница покрытия: сохранённое upto. Если оно не задано
                # (0) при непустой истории — выводим «всё, кроме последних keep».
                if upto <= 0 and total > 0:
                    upto = max(0, total - keep)
                # Покрыто summary ровно `upto` сообщений (но не больше общего).
                compacted = min(upto, total)
                recent = min(keep, total)
                sent = recent + 1  # +1 = само сообщение summary
                from_summary = compacted
            else:
                compacted = 0
                sent = total
                from_summary = 0
            return {
                "total": total,
                "keep": keep,
                "compacted": compacted,
                "sent": sent,
                "from_summary": from_summary,
                "has_summary": has_summary,
                "summary_len": len(self.compact["summary"]) if has_summary else 0,
                "strategy": self.strategy,
                "window": max(0, self.window),
                "facts_count": len(self.memory_working) + len(self.memory_longterm),
                "branches": len(self.branches),
                "active_branch": self.active_branch,
                # Сколько элементов РАБОЧЕЙ и ДОЛГОВРЕМЕННОЙ памяти реально
                # уходит в запрос (используется в интерфейсе для подсветки:
                # Рабочая — фисташковый, Долговременная — фуксия).
                "working_count": len(self.memory_working),
                "longterm_count": len(self.memory_longterm),
                # СЧЁТЧИКИ ИСПОЛЬЗОВАНИЯ данных памяти (за сессию): сколько
                # фрагментов ответов моделей заимствовано из каждой памяти.
                "memory_used_working": int(self.memory_use.get("working", 0)),
                "memory_used_longterm": int(self.memory_use.get("longterm", 0)),
                # Сколько РАЗ (за сколько ответов) каждый вид памяти пригодился.
                "memory_use_count_working": int(
                    self.memory_use_count.get("working", 0)),
                "memory_use_count_longterm": int(
                    self.memory_use_count.get("longterm", 0)),
            }

    def head_to_compact(self):
        """Возвращает НОВЫЕ вытесненные (ещё не сжатые) сообщения.

        Это сообщения, которые ещё не покрыты summary (индекс >= upto),
        но уже вытеснены за пределы последних keep сообщений — их нужно
        ДОПИСАТЬ в существующее summary (инкрементальное сжатие).

        Возвращает кортеж (head, end_index):
          head      — список новых вытесненных сообщений (может быть пуст);
          end_index — индекс, до которого (не включая) история будет покрыта
                      summary после дописывания (новая граница upto).
        """
        with self.lock:
            keep = max(0, self.compact["keep"])
            # keep = 0 → сжатие не применяется: сжимать нечего.
            if keep <= 0:
                return [], max(0, self.compact.get("upto", 0))
            upto = max(0, self.compact.get("upto", 0))
            end = len(self.messages) - keep
            if end <= upto:
                return [], upto
            head = list(self.messages[upto:end])
            return head, end

    def should_auto_compact(self):
        """Есть ли вытесненные (несжатые) сообщения для дописывания в summary.

        Логика инкрементальная: сжатие начинается, как только история
        становится больше keep (первое вытесненное сообщение), и продолжается
        по мере дальнейшего вытеснения. Достаточно хотя бы одного сообщения.
        """
        # Сжатие не применяется при keep = 0 — сжимать нечего.
        if max(0, self.compact["keep"]) <= 0:
            return False
        head, _end = self.head_to_compact()
        return len(head) >= 1

    def apply_summary(self, summary, upto=None, keep=None):
        """Сохраняет summary и границу покрытия (до какого сообщения)."""
        with self.lock:
            self.compact["enabled"] = True
            if keep is not None:
                self.compact["keep"] = max(0, int(keep))
            if upto is not None:
                self.compact["upto"] = max(0, int(upto))
            if summary is not None:
                self.compact["summary"] = str(summary)
            self._save_locked()

    # ---- Стратегии управления контекстом ----

    def set_strategy(self, strategy=None, window=None):
        """Устанавливает активную стратегию и/или окно N и сохраняет."""
        with self.lock:
            if strategy is not None and strategy in VALID_STRATEGIES:
                self.strategy = strategy
            if window is not None:
                try:
                    w = int(window)
                except (TypeError, ValueError):
                    w = self.window
                self.window = w if w >= 0 else 0
            # При входе в режим Branch синхронизируем активную ветку.
            if self.strategy == "branch":
                self.branches[self.active_branch]["messages"] = list(self.messages)
            self._save_locked()
            return {"strategy": self.strategy, "window": self.window}

    def get_strategy(self):
        """Текущие стратегия и окно (копия)."""
        with self.lock:
            return {"strategy": self.strategy, "window": self.window}

    def _summary_messages(self):
        """Возвращает [summary] + последние keep сообщений (если сжатие активно).

        Единая точка применения сжатия (см. _apply_summary_over).
        """
        return self._apply_summary_over(list(self.messages))

    def get_context_messages(self):
        """История для запроса с учётом АКТИВНОЙ стратегии (и сжатия).

        Комбинируем: сначала применяется стратегия (sliding/facts/branch),
        затем — сжатие summary (если включено и есть summary). Фактически
        summary применяется к выбранной стратегией истории.
        """
        with self.lock:
            strategy = self.strategy if self.strategy in VALID_STRATEGIES else "none"
            window = max(0, self.window)
            if strategy == "sliding":
                # Sliding Window: только последние N сообщений (N=0 — вся история).
                base = list(self.messages[-window:]) if window > 0 else list(self.messages)
            elif strategy == "facts":
                # Facts: факты хранятся в ПАМЯТИ агента (рабочая +
                # долговременная), поэтому отдельный блок facts не нужен —
                # они попадут в контекст через memory_message() ниже.
                recent = list(self.messages[-window:]) if window > 0 else list(self.messages)
                base = recent
            elif strategy == "branch":
                # Ветки работают поверх активной истории (она уже = активная ветка).
                base = list(self.messages)
            else:
                base = list(self.messages)
            # Память агента: рабочая + долговременная подмешиваются как
            # системное сообщение (краткосрочная = сам диалог `base`).
            mem_msg = self.memory_message()
            if mem_msg:
                base = [mem_msg] + base
            # СОСТОЯНИЕ ЗАДАЧИ: формализованный автомат (этап/шаг/ожидаемое
            # действие + признак паузы) — тоже системным сообщением, чтобы
            # агент продолжал задачу без повторных объяснений.
            task_msg = self.task_message()
            if task_msg:
                base = [task_msg] + base
            # Сжатие summary — поверх выбранной стратегией истории.
            return self._apply_summary_over(base)

    def _apply_summary_over(self, base):
        """Накладывает summary на произвольный список сообщений (если активно).

        Единая реализация правила сжатия (используется и get_compacted_messages,
        и _summary_messages, и get_context_messages). Если сжатие выключено,
        summary пуст или keep <= 0 — возвращает base без изменений.
        """
        if not self.compact["enabled"] or not self.compact["summary"]:
            return base
        keep = max(0, self.compact["keep"])
        if keep <= 0:
            return base
        recent = list(base[-keep:])
        return [{"role": "system", "content": self.compact["summary"]}] + recent

    def _facts_message(self):
        """(Устарело) Блок facts формируется памятью агента.

        Оставлено для обратной совместимости: собирает сообщение из фактов
        (объединения рабочей и долговременной памяти).
        """
        facts = self.get_facts()
        if not facts:
            return None
        lines = ["Известные факты о диалоге (key: value):"]
        for k, v in facts.items():
            lines.append("- %s: %s" % (k, v))
        return {"role": "system", "content": "\n".join(lines)}

    # ---- Память агента: краткосрочная / рабочая / долговременная ----
    #
    # Три типа памяти (задание A) хранятся РАЗДЕЛЬНО (задание B1):
    #   * "short"    — краткосрочная: сам текущий диалог (messages/ветки),
    #                  заполняется автоматически; отдельного поля не имеет;
    #   * "working"  — рабочая: key-value текущей задачи (memory_working);
    #   * "longterm" — долговременная: key-value профиль/решения/знания
    #                  (memory_longterm).
    # Запись в working/longterm — только ЯВНАЯ, с указанием типа (задание B2):
    # вызывающий код сам решает, в какой слой сохранить каждый ключ.

    def add_memory_usage(self, working=0, longterm=0):
        """Учитывает использование памяти за один обмен (запрос/ответ).

        working/longterm — сколько фрагментов ответа заимствовано из
        соответствующей памяти в этом обмене. Из них формируем:
          * memory_use       — СУММА фрагментов за сессию (для подсветки);
          * memory_use_count — сколько РАЗ вид памяти был задействован
                               (считаем +1 за обмен, если из памяти взяли
                               хотя бы один фрагмент). Это и есть «количество
                               раз использования вида памяти».
        Возвращает обновлённые счётчики.
        """
        with self.lock:
            try:
                w = max(0, int(working or 0))
                l = max(0, int(longterm or 0))
            except (TypeError, ValueError):
                w = l = 0
            self.memory_use["working"] += w
            self.memory_use["longterm"] += l
            # +1 «раз использования» за обмен, если память реально пригодилась.
            if w > 0:
                self.memory_use_count["working"] += 1
            if l > 0:
                self.memory_use_count["longterm"] += 1
            self._save_locked()
            return {"fragments": dict(self.memory_use),
                    "counts": dict(self.memory_use_count)}

    def memory_state(self):
        """Снимок всех трёх типов памяти (для интерфейса/отладки).

        Возвращает dict:
          short    — {items: N} краткая сводка краткосрочной памяти (диалога);
          working  — копия рабочей памяти (key-value);
          longterm — копия долговременной памяти (key-value).
        """
        with self.lock:
            return {
                "short": {
                    "kind": "диалог",
                    "items": len(self.messages),
                    "active_branch": self.active_branch,
                    "branches": len(self.branches),
                },
                "working": dict(self.memory_working),
                "longterm": dict(self.memory_longterm),
            }

    def get_memory(self, mem_type):
        """Возвращает копию памяти указанного типа.

        mem_type: "short" | "working" | "longterm".
        Для "short" возвращает сводку диалога (память ведётся сообщениями).
        Для "working"/"longterm" — копию соответствующего key-value словаря.
        """
        with self.lock:
            if mem_type == "short":
                return {
                    "kind": "диалог",
                    "items": len(self.messages),
                    "active_branch": self.active_branch,
                    "branches": len(self.branches),
                }
            if mem_type == "working":
                return dict(self.memory_working)
            if mem_type == "longterm":
                return dict(self.memory_longterm)
            return None

    @staticmethod
    def _clean_memory_dict(data):
        """Нормализует словарь памяти: строковые ключи/значения, лимиты.

        Возвращает (clean, dropped): clean — очищенный словарь в пределах
        config.MEMORY_MAX_KEYS, dropped — сколько ключей отброшено по лимиту.
        Значения обрезаются до config.MEMORY_VALUE_CAP символов.
        """
        clean = {}
        if not isinstance(data, dict):
            return clean, 0
        cap = int(config.MEMORY_VALUE_CAP)
        limit = int(config.MEMORY_MAX_KEYS)
        keys = list(data.keys())
        dropped = max(0, len(keys) - limit)
        for k in keys[:limit]:
            key = str(k).strip()
            if not key:
                continue
            val = data[k]
            val = "" if val is None else str(val)
            if len(val) > cap:
                val = val[:cap]
            clean[key] = val
        return clean, dropped

    def set_memory_key(self, mem_type, key, value):
        """Явно записывает пару key=value в память указанного типа.

        Возвращает dict записанного элемента либо возбуждает ValueError
        при неверном типе/пустом ключе.
        """
        mem_type = str(mem_type or "").strip()
        if mem_type not in config.MEMORY_TYPES:
            raise ValueError(
                "Неизвестный тип памяти: %r. Допустимо: %s"
                % (mem_type, ", ".join(config.MEMORY_TYPES)))
        key = str(key or "").strip()
        if not key:
            raise ValueError("Пустой ключ памяти.")
        val = "" if value is None else str(value)
        cap = int(config.MEMORY_VALUE_CAP)
        if len(val) > cap:
            val = val[:cap]
        with self.lock:
            if mem_type == "short":
                # Краткосрочную память вручную не пишем — это сам диалог.
                raise ValueError(
                    "Тип 'short' (текущий диалог) заполняется автоматически "
                    "и не редактируется через память. Используйте 'working' "
                    "для задач или 'longterm' для профиля/знаний.")
            if mem_type == "working":
                if key not in self.memory_working \
                        and len(self.memory_working) >= int(config.MEMORY_MAX_KEYS):
                    # вытесняем самый старый ключ (FIFO), чтобы не расти бесконечно
                    self.memory_working.pop(next(iter(self.memory_working)))
                self.memory_working[key] = val
            else:  # longterm
                if key not in self.memory_longterm \
                        and len(self.memory_longterm) >= int(config.MEMORY_MAX_KEYS):
                    self.memory_longterm.pop(next(iter(self.memory_longterm)))
                self.memory_longterm[key] = val
            self._save_locked()
        return {"type": mem_type, "key": key, "value": val}

    def delete_memory_key(self, mem_type, key):
        """Явно удаляет ключ из памяти указанного типа. True, если удалён."""
        mem_type = str(mem_type or "").strip()
        if mem_type not in config.MEMORY_TYPES:
            raise ValueError("Неизвестный тип памяти: %r" % (mem_type,))
        key = str(key or "").strip()
        if not key:
            return False
        with self.lock:
            if mem_type == "working":
                existed = self.memory_working.pop(key, None) is not None
            elif mem_type == "longterm":
                existed = self.memory_longterm.pop(key, None) is not None
            else:
                raise ValueError("Тип 'short' (диалог) не редактируется вручную.")
            if existed:
                self._save_locked()
            return existed

    def set_memory_bulk(self, mem_type, data):
        """Явно ЗАМЕНЯЕТ память указанного типа целиком словарём data.

        Используется интерфейсом для сохранения отредактированной панели
        «Рабочая/Долговременная память». Возвращает итоговый словарь.
        """
        mem_type = str(mem_type or "").strip()
        if mem_type not in config.MEMORY_TYPES:
            raise ValueError("Неизвестный тип памяти: %r" % (mem_type,))
        if mem_type == "short":
            raise ValueError("Тип 'short' (диалог) не редактируется вручную.")
        clean, _dropped = self._clean_memory_dict(data)
        with self.lock:
            if mem_type == "working":
                self.memory_working = clean
            else:
                self.memory_longterm = clean
            self._save_locked()
            return dict(clean)

    def memory_message(self):
        """Системное сообщение с памятью для запроса к LLM.

        В контекст добавляются ТОЛЬКО рабочая и долговременная память
        (краткосрочная и так присутствует как история диалога). Возвращает
        dict-сообщение или None, если обе памяти пусты.

        Агенту даётся инструкция помечать маркерами фрагменты ответа,
        опирающиеся на память, чтобы интерфейс подсветил их цветом:
          * [[R]]…[[/R]] — данные из РАБОЧЕЙ памяти (фисташковый);
          * [[L]]…[[/L]] — данные из ДОЛГОВРЕМЕННОЙ памяти (фуксия).
        """
        with self.lock:
            lines = []
            if self.memory_working:
                lines.append("Рабочая память (данные текущей задачи):")
                for k, v in self.memory_working.items():
                    lines.append("- %s: %s" % (k, v))
            if self.memory_longterm:
                if lines:
                    lines.append("")
                lines.append("Долговременная память (профиль, решения, знания):")
                for k, v in self.memory_longterm.items():
                    lines.append("- %s: %s" % (k, v))
            if not lines:
                return None
            lines.append("")
            lines.append("Правило разметки ответа: если фрагмент ответа "
                         "основан на данных из ПАМЯТИ, оберни именно этот "
                         "фрагмент маркерами:")
            lines.append("- из рабочей памяти — [[R]]…[[/R]];")
            lines.append("- из долговременной памяти — [[L]]…[[/L]].")
            lines.append("Маркеры ставь только вокруг заимствованных из памяти "
                         "слов/фраз; остальной текст — без маркеров.")
            return {"role": "system",
                    "content": "Память агента:\n" + "\n".join(lines)}

    # ---- СОСТОЯНИЕ ЗАДАЧИ (Task State Machine) ----
    #
    # Формализованное состояние задачи хранится в self.task_state (объект
    # TaskState) и входит в снимок активного профиля: у КАЖДОЙ персоны своя
    # задача. Задача — автомат «этап (planning/execution/validation/done) ->
    # текущий шаг -> ожидаемое действие» с паузой/продолжением.

    def get_task_state(self):
        """Снимок состояния задачи активного профиля (dict)."""
        with self.lock:
            return self.task_state.to_dict()

    def set_task_state(self, data):
        """Восстанавливает состояние задачи из dict и сохраняет."""
        with self.lock:
            self.task_state = TaskState(data)
            self._save_locked()
            return self.task_state.to_dict()

    def start_task(self, goal, step="", expected="", stage="planning"):
        """Заводит новую задачу (сбрасывает прежнюю) и сохраняет."""
        with self.lock:
            self.task_state.start(goal, step=step, expected=expected,
                                  stage=stage)
            self._save_locked()
            return self.task_state.to_dict()

    def advance_task(self, stage=None, step=None, expected=None, note=""):
        """Переводит задачу на новый этап/шаг с проверкой корректности.

        Возвращает dict с полями:
            ok       — был ли переход допустим;
            task     — снимок состояния задачи;
            error    — текст ошибки, если переход отклонён (иначе None).
        """
        with self.lock:
            ok, snap = self.task_state.advance(stage, step, expected, note)
            if ok:
                self._save_locked()
            return {"ok": ok,
                    "task": snap,
                    "error": None if ok else self.task_state.note}

    def pause_task(self, note=""):
        """Ставит задачу на паузу (позиция сохраняется)."""
        with self.lock:
            ok, snap = self.task_state.pause(note)
            if ok:
                self._save_locked()
            return {"ok": ok, "task": snap}

    def resume_task(self, note=""):
        """Снимает задачу с паузы (продолжаем с того же этапа/шага)."""
        with self.lock:
            ok, snap = self.task_state.resume(note)
            if ok:
                self._save_locked()
            return {"ok": ok, "task": snap}

    def finish_task(self, note=""):
        """Завершает задачу (только из этапа validation)."""
        with self.lock:
            ok, snap = self.task_state.finish(note)
            if ok:
                self._save_locked()
            return {"ok": ok,
                    "task": snap,
                    "error": None if ok else self.task_state.note}

    def reset_task(self):
        """Полностью сбрасывает состояние задачи активного профиля."""
        with self.lock:
            snap = self.task_state.reset()
            self._save_locked()
            return snap

    def task_prompt_block(self):
        """Блок системного промпта с формализованным состоянием задачи."""
        with self.lock:
            return self.task_state.system_prompt_block()

    def task_message(self):
        """Системное сообщение с состоянием задачи (или None).

        Возвращает dict-сообщение для передачи в контекст модели: благодаря
        ему агент «помнит» этап/шаг/ожидаемое действие и после паузы
        продолжает работу без повторных объяснений.
        """
        with self.lock:
            block = self.task_state.system_prompt_block()
            if not block:
                return None
            return {"role": "system", "content": block}

    # ---- ИНВАРИАНТЫ (правила, которые ассистент НЕ вправе нарушать) ----
    #
    # Инварианты хранятся ОТДЕЛЬНО от диалога (свой раздел в профиле), имеют
    # КАТЕГОРИЮ (архитектура / техрешения / стек / бизнес-правила) и явно
    # учитываются агентом. При конфликте запроса с инвариантом ассистент
    # ОТКАЗЫВАЕТСЯ предлагать решение и объясняет отказ (см. task2.md).

    @staticmethod
    def _valid_categories():
        return {c for c, _label in config.INVARIANT_CATEGORIES}

    @staticmethod
    def _category_label(code):
        for c, label in config.INVARIANT_CATEGORIES:
            if c == code:
                return label
        return code

    def _clean_invariant(self, text, category):
        """Нормализует один инвариант: текст (обрезка) + категория (валидация)."""
        text = str(text if text is not None else "").strip()
        if not text:
            raise ValueError("Пустой текст инварианта.")
        text = text[: int(config.INVARIANT_TEXT_CAP)]
        cat = str(category if category is not None else "").strip().lower()
        if cat not in self._valid_categories():
            # Неизвестная категория → относим к бизнес-правилам.
            cat = "business"
        return {"text": text, "category": cat}

    def invariants_state(self):
        """Снимок инвариантов для интерфейса (список + категории для UI)."""
        with self.lock:
            return {
                "invariants": [dict(i) for i in self.invariants],
                "categories": [{"id": c, "label": label}
                               for c, label in config.INVARIANT_CATEGORIES],
                "count": len(self.invariants),
                "max": int(config.INVARIANT_MAX),
            }

    def get_invariants(self):
        """Копия списка инвариантов."""
        with self.lock:
            return [dict(i) for i in self.invariants]

    def add_invariant(self, text, category="business"):
        """Добавляет ОДИН инвариант (с проверкой предела и валидацией)."""
        with self.lock:
            if len(self.invariants) >= int(config.INVARIANT_MAX):
                raise ValueError("Достигнут предел числа инвариантов (%d)."
                                 % int(config.INVARIANT_MAX))
            item = self._clean_invariant(text, category)
            item["id"] = self._next_invariant_id_locked()
            self.invariants.append(item)
            self._save_locked()
            return self.invariants_state()

    def set_invariants(self, items):
        """ЗАМЕНЯЕТ весь список инвариантов (для сохранения панели целиком)."""
        with self.lock:
            if not isinstance(items, list):
                items = []
            clean = []
            for it in items:
                if not isinstance(it, dict):
                    continue
                try:
                    item = self._clean_invariant(
                        it.get("text"), it.get("category"))
                except ValueError:
                    continue
                item["id"] = str(it.get("id") or self._next_invariant_id_locked())
                clean.append(item)
                if len(clean) >= int(config.INVARIANT_MAX):
                    break
            self.invariants = clean
            self._save_locked()
            return self.invariants_state()

    def update_invariant(self, inv_id, text=None, category=None):
        """Обновляет текст и/или категорию инварианта по его id."""
        with self.lock:
            inv_id = str(inv_id or "").strip()
            for item in self.invariants:
                if item.get("id") == inv_id:
                    if text is not None:
                        new = self._clean_invariant(text, item.get("category"))
                        item["text"] = new["text"]
                    if category is not None:
                        item["category"] = self._clean_invariant(
                            item.get("text", "x"), category)["category"]
                    self._save_locked()
                    return self.invariants_state()
            raise ValueError("Инвариант не найден: %r" % (inv_id,))

    def delete_invariant(self, inv_id):
        """Удаляет инвариант по id. True, если он был удалён."""
        with self.lock:
            inv_id = str(inv_id or "").strip()
            before = len(self.invariants)
            self.invariants = [i for i in self.invariants
                               if i.get("id") != inv_id]
            removed = len(self.invariants) != before
            if removed:
                self._save_locked()
            return removed

    def clear_invariants(self):
        """Очищает список инвариантов."""
        with self.lock:
            self.invariants = []
            self._save_locked()
            return self.invariants_state()

    def _next_invariant_id_locked(self):
        existing = {i.get("id") for i in self.invariants}
        n = len(self.invariants) + 1
        while ("inv%d" % n) in existing:
            n += 1
        return "inv%d" % n

    def invariants_prompt_block(self):
        """Текст-блок инвариантов для системного промпта (или "")."""
        with self.lock:
            if not self.invariants:
                return ""
            by_cat = {}
            for i in self.invariants:
                by_cat.setdefault(i.get("category", "business"), []).append(
                    i.get("text", ""))
            lines = ["ИНВАРИАНТЫ (жёсткие правила — НАРУШАТЬ НЕЛЬЗЯ):"]
            for code, label in config.INVARIANT_CATEGORIES:
                items = by_cat.get(code)
                if not items:
                    continue
                lines.append("%s:" % label)
                for t in items:
                    lines.append("  - %s" % t)
            lines.append(
                "Если запрос противоречит хотя бы одному инварианту — "
                "ОТКАЖИСЬ предлагать такое решение и кратко объясни, какой "
                "именно инвариант нарушен и почему.")
            return "\n".join(lines)

    def invariants_message(self):
        """Системное сообщение с инвариантами (или None, если их нет)."""
        block = self.invariants_prompt_block()
        if not block:
            return None
        return {"role": "system", "content": block}

    # ---- Facts (key-value память) ----
    #
    # ВАЖНО: «факты» (стратегия Facts) — это НЕ отдельное хранилище, а
    # ДАННЫЕ ПАМЯТИ агента. Каждый факт хранится в одном из слоёв памяти —
    # рабочей (memory_working) или долговременной (memory_longterm) — по
    # выбору пользователя. Поэтому get_facts() возвращает объединение обоих
    # слоёв, а set_facts() кладёт факты в рабочую память.

    def set_facts(self, facts):
        """ДОПОЛНЯЕТ рабочую память фактами (не перезаписывает её).

        Используется для авто-обновления фактов агентом после каждого хода.
        ВАЖНО: рабочая память НЕ очищается после каждого запроса — новые/
        изменённые факты ДОПОЛНЯЮТ уже накопленные данные, а прежние ключи
        сохраняются, если модель их не вернула. Ключи, лежащие в
        ДОЛГОВРЕМЕННОЙ памяти, не трогаем (сохраняем выбор пользователя).
        """
        with self.lock:
            if isinstance(facts, dict):
                # Дополняем рабочую память: существующие ключи не удаляем,
                # при совпадении — обновляем значение.
                for k, v in facts.items():
                    key = str(k)
                    if key not in self.memory_longterm:
                        self.memory_working[key] = str(v)
            self._save_locked()
            return self.get_facts()

    def merge_memory(self, mem_type, data):
        """ДОПОЛНЯЕТ память указанного типа словарём data (merge, не replace).

        Существующие ключи сохраняются; совпадающие — обновляются значением
        из data. Используется, чтобы данные памяти накапливались, а не
        терялись при каждом обновлении. Возвращает итоговый словарь слоя.
        """
        mem_type = str(mem_type or "").strip()
        if mem_type not in config.MEMORY_TYPES:
            raise ValueError("Неизвестный тип памяти: %r" % (mem_type,))
        if mem_type == "short":
            raise ValueError("Тип 'short' (диалог) не редактируется вручную.")
        clean, _dropped = self._clean_memory_dict(data)
        with self.lock:
            target = self.memory_working if mem_type == "working" \
                else self.memory_longterm
            for k, v in clean.items():
                # При переполнении вытесняем самый старый ключ (FIFO).
                if k not in target and len(target) >= int(config.MEMORY_MAX_KEYS):
                    target.pop(next(iter(target)))
                target[k] = v
            self._save_locked()
            return dict(target)

    def get_facts(self):
        """Факты = объединение рабочей и долговременной памяти (key-value).

        Ключи рабочей памяти идут первыми, затем — долговременной.
        """
        with self.lock:
            merged = {}
            merged.update(self.memory_working)
            merged.update(self.memory_longterm)
            return merged

    def facts_memory_map(self):
        """Соответствие ключ-факт -> слой памяти ("working"|"longterm")."""
        with self.lock:
            out = {}
            for k in self.memory_working:
                out[k] = "working"
            for k in self.memory_longterm:
                out[k] = "longterm"
            return out

    # ---- Ветки диалога (Branching) ----

    def branches_state(self):
        """Состояние веток: список имён, активная ветка, размеры."""
        with self.lock:
            return {
                "branches": [{"name": b["name"], "size": len(b["messages"])}
                             for b in self.branches],
                "active_branch": self.active_branch,
            }

    def create_branch(self, name=None, count=None, from_checkpoint=True):
        """Создаёт одну или несколько веток от текущего состояния диалога.

        from_checkpoint=True — новые ветки копируют текущую историю (checkpoint).
        count — сколько веток создать (по умолчанию config.BRANCH_DEFAULT_COUNT).
        Возвращает состояние веток после операции.
        """
        with self.lock:
            # Переключаемся в режим Branch и синхронизируем активную ветку.
            self.branches[self.active_branch]["messages"] = list(self.messages)
            n = int(count) if count else int(config.BRANCH_DEFAULT_COUNT)
            if n < 1:
                n = 1
            snapshot = list(self.messages) if from_checkpoint else []
            base_n = len(self.branches)
            for i in range(n):
                bname = (name + ("-%d" % (i + 1) if n > 1 else "")) if name \
                    else ("ветка-%d" % (base_n + i))
                self.branches.append({"name": bname, "messages": list(snapshot)})
            self.strategy = "branch"
            # Активной делаем первую новую ветку (можно переключить).
            self.active_branch = len(self.branches) - n
            self.messages = list(self.branches[self.active_branch]["messages"])
            self._save_locked()
            return self.branches_state()

    def switch_branch(self, index):
        """Переключает активную ветку по индексу и сохраняет."""
        with self.lock:
            # Перед переключением сохраняем текущую историю в её ветку.
            if 0 <= self.active_branch < len(self.branches):
                self.branches[self.active_branch]["messages"] = list(self.messages)
            try:
                idx = int(index)
            except (TypeError, ValueError):
                idx = self.active_branch
            if 0 <= idx < len(self.branches):
                self.active_branch = idx
                self.messages = list(self.branches[idx]["messages"])
                self.strategy = "branch"
            self._save_locked()
            return self.branches_state()

    def delete_branch(self, index):
        """Удаляет ветку по индексу (нельзя удалить последнюю)."""
        with self.lock:
            try:
                idx = int(index)
            except (TypeError, ValueError):
                return self.branches_state()
            if len(self.branches) <= 1 or not (0 <= idx < len(self.branches)):
                return self.branches_state()
            del self.branches[idx]
            if self.active_branch >= len(self.branches):
                self.active_branch = len(self.branches) - 1
            self.messages = list(self.branches[self.active_branch]["messages"])
            self._save_locked()
            return self.branches_state()

    def rename_branch(self, index, name):
        """Переименовывает ветку по индексу и сохраняет на диск.

        Пустое имя или имя из пробелов игнорируется. Возвращает состояние
        веток после операции.
        """
        with self.lock:
            try:
                idx = int(index)
            except (TypeError, ValueError):
                return self.branches_state()
            new_name = str(name if name is not None else "").strip()
            # Обрезаем чрезмерно длинные имена, чтобы не ломать интерфейс.
            if len(new_name) > 80:
                new_name = new_name[:80]
            if not new_name or not (0 <= idx < len(self.branches)):
                return self.branches_state()
            self.branches[idx]["name"] = new_name
            self._save_locked()
            return self.branches_state()
