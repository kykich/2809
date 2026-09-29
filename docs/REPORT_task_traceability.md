# Трассировка требований: `task.md` → реализация → тест

> ℹ️ **Примечание.** Автономный тест автомата задачи (`tests/check_task_state.py`)
> и относящиеся к нему интерфейсные прогоны («Тест автомата», «Прогон по модели»,
> «Тест ТЗ») из проекта удалены. Реализация самого автомата задачи
> (`rtk_app/task_state.py`, `session_store.py`, `agent.py`) сохранена и
> доступна через панель «Состояние задачи» в интерфейсе. Упоминания теста в
> разделах ниже оставлены как историческая справка о проектировании.

**Тема задачи:** Контролируемые переходы состояний — явные переходы между
состояниями задачи, при которых ассистент не может «перепрыгнуть» этап.

Документ связывает каждый пункт исходного ТЗ (`task.md`) с конкретным кодом
(реализацией) и проверкой (тестом). Ниже — таблица трассировки, затем
детализация по каждому пункту.

---

## Сводная таблица трассировки

| Пункт `task.md` | Требование | Реализация (код) | Тест (проверка) |
|---|---|---|---|
| 1 | У задачи есть допустимые состояния | `rtk_app/task_state.py`: `STAGES`, `STAGE_LABELS` | панель «Состояние задачи» (UI) |
| 2 | Есть разрешённые переходы между ними | `rtk_app/task_state.py`: `ALLOWED_TRANSITIONS`, `TaskState.can_transition()`, `is_valid_transition()` | панель «Состояние задачи» (UI) |
| 3 | Ассистент не может «перепрыгнуть» этап | `TaskState.advance()` (отклонение недопустимого перехода) | панель «Состояние задачи» (UI) |
| 4.1 | Нельзя делать реализацию до утверждённого плана | `ALLOWED_TRANSITIONS["planning"] = {"planning", "execution"}` | `test_clean_machine`: «запрет прыжка planning->done», «planning не прыгает в validation» |
| 4.2 | Нельзя делать финал без валидации | `TaskState.finish()` — только из `validation` | `test_clean_machine`, `test_store_persistence`: «finish не из validation запрещён» |
| 5.1 | Попытки перейти в недопустимое состояние | `advance()` возвращает `(False, …)`, пишет запись `reject` в журнал | `test_clean_machine`, `test_transition_table` |
| 5.2 | Реакция ассистента | `system_prompt_block()` (этап/шаг → промпт); авто-переход только при корректности (`web/server.py::_maybe_advance_task`) | `test_prompt_block`, `check_task_state.py` (весь прогон) |
| 5.3 | Корректность продолжения после паузы | `TaskState.pause()` / `resume()` — сохраняют этап/шаг/цель | `test_pause_resume_keeps_position`, `test_store_persistence` |
| 6 | Ассистент с контролируемым жизненным циклом задачи | `TaskState` + `task_message()` + интеграция в `agent.py` и `session_store.py` | панель «Состояние задачи» (UI) |

---

## Детализация по пунктам

### 1) У задачи есть допустимые состояния

**Требование `task.md`:** «у задачи есть допустимые состояния».

**Реализация** — `rtk_app/task_state.py`:

```python
# Этапы задачи — строго в порядке прохождения (линейный автомат).
STAGES = ("planning", "execution", "validation", "done")

STAGE_LABELS = {
    "planning": "планирование",
    "execution": "выполнение",
    "validation": "проверка",
    "done": "завершено",
}
```

Состояние задачи (конечный автомат) описывается классом `TaskState` с полями:
`stage` (этап), `step` (текущий шаг), `expected` (ожидаемое действие),
`paused` (пауза), `goal` (цель), `history` (журнал переходов).

**Тест** — `check_task_state.py`:

- `test_clean_machine` — «пустое состояние не активно», «старт: этап planning».
- `test_transition_table` — «все этапы описаны в таблице»
  (`all(s in ALLOWED_TRANSITIONS for s in STAGES)`).

---

### 2) Есть разрешённые переходы между ними

**Требование `task.md`:** «есть разрешённые переходы между ними».

**Реализация** — `rtk_app/task_state.py`:

```python
ALLOWED_TRANSITIONS = {
    "planning":   {"planning", "execution"},
    "execution":  {"execution", "validation"},
    "validation": {"validation", "done", "execution"},
    "done":       {"done"},
}
```

Проверка допустимости — `TaskState.can_transition()` и статический
`TaskState.is_valid_transition(from_stage, to_stage)`.

**Тест** — `check_task_state.py::test_transition_table`:

- «переход %s->%s валиден» — для каждой пары из таблицы;
- «done не ведёт никуда, кроме себя» (`ALLOWED_TRANSITIONS["done"] == {"done"}`).

---

### 3) Ассистент не может «перепрыгнуть» этап

**Требование `task.md`:** «ассистент не может “перепрыгнуть” этап».

**Реализация** — `TaskState.advance()` в `rtk_app/task_state.py`: если целевой
этап недопустим, переход **отклоняется** (возврат `(False, snapshot)`), в
журнал пишется запись с типом `"reject"`, поле `note` получает пояснение
«некорректный переход … отклонён», а текущий этап не меняется:

```python
def advance(self, stage=None, step=None, expected=None, note=""):
    if not self.active:
        return False, self.to_dict()
    target = normalize_stage(stage) if stage is not None else self.stage
    if not self.can_transition(target):
        self.note = ("некорректный переход %s -> %s отклонён"
                     % (self.stage, target))
        self._log("reject", target, self.note)
        return False, self.to_dict()
    ...
```

**Тест** — `check_task_state.py::test_clean_machine`:

- «запрет прыжка planning->done»;
- «этап не изменился после запрета»;
- «нельзя вернуться к planning из validation»;
- «из done переходы запрещены».

---

### 4.1) Нельзя делать реализацию до утверждённого плана

**Требование `task.md`:** «нельзя делать реализацию до утверждённого плана».

**Реализация:** из этапа `planning` разрешён переход только в `execution`
(или уточнение внутри `planning`) — см. `ALLOWED_TRANSITIONS["planning"]`.
Прыжок сразу к `validation`/`done` невозможен.

**Тест** — `check_task_state.py::test_clean_machine`, `test_transition_table`:

- «запрет прыжка planning->done»;
- «planning не прыгает в validation»
  (`not TaskState.is_valid_transition("planning", "validation")`).

---

### 4.2) Нельзя делать финал без валидации

**Требование `task.md`:** «нельзя делать финал без валидации».

**Реализация** — `TaskState.finish()` в `rtk_app/task_state.py`: завершить
задачу (`done`) можно **только** из этапа `validation`; иначе возвращается
`(False, …)` с пояснением «завершить можно только после проверки».

```python
def finish(self, note=""):
    if not self.is_active():
        return False, self.to_dict()
    if self.stage != "validation":
        self.note = ("завершить можно только после проверки "
                     "(текущий этап: %s)" % self.stage)
        self._log("reject-done", "done", self.note)
        return False, self.to_dict()
    ...
```

**Тест** — `check_task_state.py`:

- `test_clean_machine`: «finish из validation разрешён», «этап стал done»;
- `test_store_persistence`: «finish не из validation запрещён», «finish из
  validation ок».

---

### 5.1) Попытки перейти в недопустимое состояние

**Требование `task.md`:** проверить «попытки перейти в недопустимое состояние».

**Реализация:** все попытки проходят через `TaskState.advance()`/`finish()`,
которые проверяют `can_transition()` и возвращают признак отказа `False`;
неудачные попытки фиксируются в журнале (`kind = "reject"` /
`"reject-done"`).

**Тест** — `check_task_state.py`: сценарии с намеренно недопустимыми
переходами и проверкой, что результат — `False` и этап не изменился.

---

### 5.2) Реакция ассистента

**Требование `task.md`:** проверить «реакцию ассистента».

**Реализация:**

- состояние задачи попадает в системный промпт модели через
  `TaskState.system_prompt_block()` → `SessionStore.task_message()` →
  `Agent.task_system_prompt()` (ассистент «видит» этап/шаг и ведёт ответ
  сообразно ему);
- при авто-переходе по ходу диалога (`web/server.py::_maybe_advance_task`)
  предложенный моделью переход применяется **только если он корректен**
  (проверка в `SessionStore.advance_task` → `TaskState.advance`), иначе
  отклоняется — ассистент не может «перескочить» этап.

**Тест** — `check_task_state.py::test_prompt_block`:

- «в блоке есть цель», «в блоке есть этап execution»,
- «в блоке есть шаг», «в блоке указана пауза».

Дополнительно — интерактивный прогон `web/server.py::_handle_task_selftest`
(режим `llm`): модель ведёт задачу по этапам, а сервер принимает/отклоняет
переходы по правилам автомата.

---

### 5.3) Корректность продолжения после паузы

**Требование `task.md`:** проверить «корректность продолжения после паузы».

**Реализация** — `TaskState.pause()` / `resume()` в `rtk_app/task_state.py`:

- пауза возможна на **любом** незавершённом этапе (`is_active()`);
- при `resume()` этап/шаг/цель **сохраняются**, работа продолжается с того же
  места — ассистент не просит объяснять задачу заново (это же фиксирует
  `system_prompt_block()` строкой «задача НА ПАУЗЕ. Продолжай с текущего
  этапа/шага…»).

**Тест** — `check_task_state.py`:

- `test_pause_resume_keeps_position` — пауза/продолжение на каждом из этапов
  `planning`/`execution`/`validation` без потери позиции и цели; «журнал
  содержит pause и resume»;
- `test_store_persistence` — «продолжение после перезапуска ок», «позиция не
  потеряна» (пауза и позиция переживают перезапуск процесса).

---

### 6) Результат: ассистент с контролируемым жизненным циклом задачи

**Требование `task.md`:** «ассистент с контролируемым жизненным циклом задачи».

**Реализация (сводно):**

| Слой | Модуль | Что делает |
|---|---|---|
| Логика автомата | `rtk_app/task_state.py` | `TaskState`: этапы, переходы, пауза/продолжение, журнал, промпт-блок |
| Хранилище | `rtk_app/session_store.py` | `start_task/advance_task/pause_task/resume_task/finish_task/reset_task`, снимок в JSON (в т.ч. у каждого профиля) |
| Агент | `rtk_app/agent.py` | `task_system_prompt()`, `advance_task()` (LLM предлагает переход) |
| Сервер | `web/server.py` | `POST /api/task` (start/advance/pause/resume/finish/reset/state), авто-переход `_maybe_advance_task` |
| Интерфейс | `index.html`, `js/app.js` | панель «Состояние задачи»: этапы, кнопки, пауза/продолжение |

**Тест** — `check_task_state.py` целиком (5 разделов):

1. Автомат: базовые переходы и запреты;
2. Автомат: пауза/продолжение (без повторных объяснений);
3. Промпт: состояние задачи передаётся модели;
4. Хранилище: сохранение и продолжение между запусками;
5. Автомат: таблица переходов.

---

## Как запускать проверки

Автономный тест логики автомата (`check_task_state.py`) и интерфейсные
прогоны («Тест автомата», «Прогон по модели», «Тест ТЗ») **удалены** из
проекта. Панель «Состояние задачи» в интерфейсе остаётся доступной для ручной
работы с автоматом (этапы, пауза/продолжение).

Связанные проверки управления контекстом (офлайн):

```bash
python check_context.py          # сжатие истории: summary + последние keep
python check_server_context.py   # интеграция сжатия на стороне сервера
python check_strategies.py       # стратегии контекста (sliding/facts/branch)
```

---

## Ключевые исходные файлы

| Файл | Роль |
|---|---|
| `task.md` | Исходное ТЗ |
| `rtk_app/task_state.py` | Конечный автомат задачи (логика переходов) |
| `rtk_app/session_store.py` | Хранение/персистентность состояния задачи |
| `rtk_app/agent.py` | Встраивание состояния задачи в промпт; LLM-переходы |
| `web/server.py` | API `/api/task`; авто-переход |
| `js/app.js`, `index.html` | Панель и кнопки «Состояние задачи» |
