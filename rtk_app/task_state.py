"""Состояние задачи (Task State Machine) — формализованный конечный автомат.

Задача агента представлена КОНЕЧНЫМ АВТОМАТОМ, у которого есть:
  * ЭТАП ЗАДАЧИ (stage) — крупная фаза: planning -> execution ->
    validation -> done;
  * ТЕКУЩИЙ ШАГ (step) — конкретное действие внутри этапа (что делаем
    сейчас);
  * ОЖИДАЕМОЕ ДЕЙСТВИЕ (expected) — что агент ждёт дальше от пользователя
    или от себя (вопрос, подтверждение, данные, завершение шага …).

Дополнительно хранится:
  * paused     — задача на ПАУЗЕ (можно приостановить на ЛЮБОМ этапе);
  * note       — последнее пояснение (итог перехода, причина паузы …);
  * goal       — исходная цель задачи (чтобы после паузы НЕ переспрашивать);
  * history    — журнал переходов (для «продолжения без повторных
    объяснений»: агент видит, что уже сделано).

ГЛАВНОЕ требование — КОРРЕКТНОСТЬ ПЕРЕХОДОВ:
  * задачу можно поставить НА ПАУЗУ на любом этапе;
  * после снятия паузы работа ПРОДОЛЖАЕТСЯ с того же этапа/шага, а цель и
    журнал сохраняются, поэтому агент НЕ просит объяснять задачу заново;
  * запрещены некорректные прыжки между этапами (см. ALLOWED_TRANSITIONS).

Модуль не зависит от сети/LLM — это чистая логика автомата. Он лишь хранит
состояние и проверяет допустимость переходов; решение «какой этап/шаг
дальше» принимает агент (в т.ч. с помощью LLM) и вызывает advance().
"""
from datetime import datetime

__all__ = ["TaskState", "STAGES", "STAGE_LABELS",
           "ALLOWED_TRANSITIONS", "normalize_stage"]

# Этапы задачи — строго в порядке прохождения (линейный автомат).
STAGES = ("planning", "execution", "validation", "done")

# Человекочитаемые названия этапов (для интерфейса и промпта).
STAGE_LABELS = {
    "planning": "планирование",
    "execution": "выполнение",
    "validation": "проверка",
    "done": "завершено",
}

# Допустимые переходы между этапами.
#   planning  -> execution  : план готов, переходим к выполнению;
#   execution -> validation : работа сделана, переходим к проверке;
#   validation-> done       : проверка успешна, задача завершена;
#   validation-> execution  : проверка нашла недочёт, возвращаемся к работе;
#   любой этап -> сам себя   : уточнение шага внутри этапа;
#   done -> (ничего)         : завершённую задачу не переоткрываем.
ALLOWED_TRANSITIONS = {
    "planning": {"planning", "execution"},
    "execution": {"execution", "validation"},
    "validation": {"validation", "done", "execution"},
    "done": {"done"},
}


def normalize_stage(stage):
    """Приводит строку к известному этапу (иначе — 'planning')."""
    s = str(stage or "").strip().lower()
    return s if s in STAGES else "planning"


class TaskState:
    """Формализованное состояние задачи как конечный автомат."""

    def __init__(self, state=None):
        """Создаёт состояние. state — dict для восстановления (или None)."""
        self.active = False
        self.stage = "planning"
        self.step = ""
        self.expected = ""
        self.paused = False
        self.goal = ""
        self.note = ""
        self.history = []
        if isinstance(state, dict):
            self.from_dict(state)

    # ---- (де)сериализация ----

    def to_dict(self):
        """Снимок состояния для хранения в JSON / отправки в интерфейс."""
        return {
            "active": bool(self.active),
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "step": self.step,
            "expected": self.expected,
            "paused": bool(self.paused),
            "goal": self.goal,
            "note": self.note,
            "history": list(self.history),
            "stages": list(STAGES),
            "stage_labels": dict(STAGE_LABELS),
        }

    def from_dict(self, data):
        """Восстанавливает состояние из dict (с защитой от мусора)."""
        if not isinstance(data, dict):
            return self
        self.active = bool(data.get("active", False))
        self.stage = normalize_stage(data.get("stage"))
        self.step = str(data.get("step", "") or "")
        self.expected = str(data.get("expected", "") or "")
        self.paused = bool(data.get("paused", False))
        self.goal = str(data.get("goal", "") or "")
        self.note = str(data.get("note", "") or "")
        hist = data.get("history")
        if isinstance(hist, list):
            self.history = [h for h in hist if isinstance(h, dict)]
        else:
            self.history = []
        return self

    # ---- проверки ----

    def is_active(self):
        """Заведена ли задача (не завершена и не пустая)."""
        return self.active and self.stage != "done"

    def is_done(self):
        """Завершена ли задача."""
        return self.active and self.stage == "done"

    def can_transition(self, to_stage):
        """Допустим ли переход из текущего этапа в to_stage."""
        return normalize_stage(to_stage) in ALLOWED_TRANSITIONS.get(
            self.stage, set())

    @staticmethod
    def is_valid_transition(from_stage, to_stage):
        """Проверка допустимости перехода между двумя этапами (без состояния)."""
        f = normalize_stage(from_stage)
        t = normalize_stage(to_stage)
        return t in ALLOWED_TRANSITIONS.get(f, set())

    # ---- операции автомата ----

    def start(self, goal, step="", expected="", stage="planning", note=""):
        """Заводит НОВУЮ задачу (сбрасывает прежнюю) на этапе planning."""
        self.active = True
        self.stage = normalize_stage(stage)
        self.goal = str(goal or "").strip()
        self.step = str(step or "").strip()
        self.expected = str(expected or "").strip()
        self.paused = False
        self.note = str(note or "").strip()
        self.history = []
        self._log("start", self.stage, "задача заведена: %s" % self.goal)
        return self.to_dict()

    def advance(self, stage=None, step=None, expected=None, note=""):
        """Переводит задачу в новый этап/шаг с проверкой КОРРЕКТНОСТИ."""
        if not self.active:
            return False, self.to_dict()
        target = normalize_stage(stage) if stage is not None else self.stage
        if not self.can_transition(target):
            self.note = ("некорректный переход %s -> %s отклонён"
                         % (self.stage, target))
            self._log("reject", target, self.note)
            return False, self.to_dict()
        was_paused = self.paused
        self.paused = False
        self.stage = target
        if step is not None:
            self.step = str(step or "").strip()
        if expected is not None:
            self.expected = str(expected or "").strip()
        self.note = str(note or "").strip()
        self._log("advance", target,
                  ("продолжение после паузы: " if was_paused else "")
                  + (self.note or "переход на этап %s" % target))
        return True, self.to_dict()

    def pause(self, note=""):
        """Ставит задачу НА ПАУЗУ (на любом незавершённом этапе)."""
        if not self.is_active():
            return False, self.to_dict()
        self.paused = True
        self.note = str(note or "").strip() or "пауза"
        self._log("pause", self.stage, self.note)
        return True, self.to_dict()

    def resume(self, note=""):
        """СНИМАЕТ задачу с паузы: продолжаем с ТОГО ЖЕ этапа/шага."""
        if not self.is_active() or not self.paused:
            return False, self.to_dict()
        self.paused = False
        self.note = str(note or "").strip() or "продолжение работы"
        self._log("resume", self.stage, self.note)
        return True, self.to_dict()

    def finish(self, note=""):
        """Завершает задачу (этап done). Допустимо только из validation."""
        if not self.is_active():
            return False, self.to_dict()
        if self.stage != "validation":
            self.note = ("завершить можно только после проверки "
                         "(текущий этап: %s)" % self.stage)
            self._log("reject-done", "done", self.note)
            return False, self.to_dict()
        self.stage = "done"
        self.paused = False
        self.step = ""
        self.expected = ""
        self.note = str(note or "").strip() or "задача завершена"
        self._log("done", "done", self.note)
        return True, self.to_dict()

    def reset(self):
        """Полностью сбрасывает состояние задачи (сброс вместе с сессией)."""
        self.active = False
        self.stage = "planning"
        self.step = ""
        self.expected = ""
        self.paused = False
        self.goal = ""
        self.note = ""
        self.history = []
        return self.to_dict()

    # ---- журнал ----

    def _log(self, kind, stage, detail):
        """Добавляет запись в журнал переходов (с ограничением длины)."""
        self.history.append({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "kind": kind,
            "stage": stage,
            "stage_label": STAGE_LABELS.get(stage, stage),
            "paused": bool(self.paused),
            "detail": str(detail or ""),
        })
        if len(self.history) > 50:
            self.history = self.history[-50:]

    # ---- промпт ----

    def system_prompt_block(self):
        """Блок системного промпта с формализованным состоянием задачи."""
        if not self.active:
            return ""
        lines = ["ЗАДАЧА (формализованное состояние):",
                 "Цель: %s" % (self.goal or "не указана"),
                 "Этап: %s (%s)" % (self.stage,
                                    STAGE_LABELS.get(self.stage, self.stage))]
        if self.step:
            lines.append("Текущий шаг: %s" % self.step)
        if self.expected:
            lines.append("Ожидаемое действие: %s" % self.expected)
        if self.paused:
            lines.append("СТАТУС: задача НА ПАУЗЕ. Продолжай с текущего "
                         "этапа/шага, НЕ проси объяснять задачу заново.")
        if self.stage == "done":
            lines.append("Статус: задача завершена.")
        tail = self.history[-6:]
        if tail:
            lines.append("Ход задачи:")
            for h in tail:
                lines.append("- [%s] %s: %s" % (h.get("ts", ""),
                                                h.get("stage_label", ""),
                                                h.get("detail", "")))
        return "\n".join(lines)