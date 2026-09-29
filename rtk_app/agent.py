"""Агент чата — отдельная сущность, инкапсулирующая всю логику запросов к LLM.

Агент:
  * принимает запросы пользователя (вопрос + историю диалога);
  * позволяет выбрать, какие модели опросить, и задать температуру каждой;
  * сам строит итоговый набор сообщений (включая системный промпт);
  * обращается к выбранным большим языковым моделям через API;
  * собирает метрики (время, токены, стоимость);
  * возвращает готовый результат (текст, HTML-разметка, метаданные).

Логика обработки запроса спрятана внутри агента — внешний код
(HTTP-обработчик) лишь передаёт пользовательский ввод и получает ответ.
"""
import json
import time
from datetime import datetime, timedelta

from . import config, deepseek, gigachat, html_report

__all__ = ["Agent"]


def json_dumps_safe(obj):
    """Аккуратно сериализует объект в JSON (для промптов)."""
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return str(obj)


def parse_facts_json(text):
    """Извлекает словарь фактов (key-value) из ответа модели.

    Принимает строку, возможно с обрамлением в markdown-блок ```json … ```.
    Возвращает dict строк или None, если разобрать не удалось.
    """
    if not text:
        return None
    s = str(text).strip()
    # Снимаем markdown-обёртку ```json ... ```
    if s.startswith("```"):
        s = s.strip("`")
        if "\n" in s:
            first, rest = s.split("\n", 1)
            if first.strip().lower() in ("json", ""):
                s = rest
        s = s.strip()
    # Находим границы JSON-объекта.
    start = s.find("{")
    end = s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    frag = s[start:end + 1]
    try:
        data = json.loads(frag)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return {str(k): ("" if v is None else str(v)) for k, v in data.items()}

# Источники "голосов": (провайдер, имя модели, метка для отображения, css-класс)
SOURCES = (
    ("deepseek", config.DS_MODELS[0], "DeepSeek-flash", "ds-flash"),
    ("gigachat", config.GC_MODEL, "GigaChat", "gc-base"),
)

# Системный промпт агента (роль). Ставится в начало каждого диалога.
SYSTEM_PROMPT = ("Ты — дружелюбный ассистент. Отвечай по-русски, понятно и "
                 "структурированно, следуя запросам пользователя.")

# Общая системная инструкция для вспомогательных LLM-операций, ожидающих
# СТРОГО JSON-ответ (проверка инвариантов, переход задачи, факты и т.п.).
STRICT_JSON_SYSTEM = ("Отвечай СТРОГО одним JSON-объектом: без пояснений, "
                      "без markdown-обёртки и без лишнего текста.")


class Agent:
    """Единая точка общения пользователя с большими языковыми моделями."""

    def __init__(self, api_key, sources=None):
        """Создаёт агента.

        api_key — ключ DeepSeek; sources — последовательность кортежей
        (provider, model, label, css_class), если нужен свой набор моделей.
        """
        self.api_key = api_key
        self.sources = list(sources or SOURCES)
        self.enabled_models = [s[2] for s in self.sources]
        # Индекс доступных моделей по их метке (label) для быстрого выбора
        # по имени в запросе пользователя.
        self.by_label = {s[2]: s for s in self.sources}

    # ---- открытый интерфейс агента ----

    @property
    def label(self):
        """Краткое описание состава агента (для интерфейса)."""
        return " | ".join(self.enabled_models)

    def available(self):
        """Список моделей, которые умеет обслуживать агент (метаданные для UI)."""
        out = []
        for provider, model, label, cls in self.sources:
            out.append({
                "label": label,
                "cls": cls,
                "provider": provider,
                "model": model,
            })
        return out

    def _coerce_temperature(self, value):
        """Приводит значение температуры к float в допустимом диапазоне."""
        try:
            t = float(value)
        except (TypeError, ValueError):
            return None
        if t < config.TEMP_MIN:
            return float(config.TEMP_MIN)
        if t > config.TEMP_MAX:
            return float(config.TEMP_MAX)
        return t

    def _coerce_max_tokens(self, value):
        """Приводит значение max_tokens к int в допустимом диапазоне.

        None и нечисловые значения означают «не применять» (возвращаем None —
        модель сама выбирает лимит вывода). Выход за границы клампится.
        """
        try:
            v = int(value)
        except (TypeError, ValueError):
            return None
        if v < config.MAX_TOKENS_MIN:
            return int(config.MAX_TOKENS_MIN)
        if v > config.MAX_TOKENS_MAX:
            return int(config.MAX_TOKENS_MAX)
        return v

    def _selected_sources(self, selected):
        """Разрешает пользовательский выбор в кортежи источников.

        selected — список элементов вида {label, temperature}. Возвращает
        словарь {label: (source_tuple, temperature_or_None)} только для тех
        моделей, что реально доступны агенту. Если выборка пуста или не
        содержит подходящих меток — используются все источники (без явной
        температуры, т.е. системное значение модели).
        """
        chosen = {}
        if isinstance(selected, list) and selected:
            for opt in selected:
                if isinstance(opt, dict):
                    label = str(opt.get("label", ""))
                else:
                    label = str(opt)
                base = self.by_label.get(label)
                if base is None:
                    continue
                temp = self._coerce_temperature(
                    opt.get("temperature") if isinstance(opt, dict) else None)
                chosen[label] = (base, temp)
        if not chosen:
            for base in self.sources:
                chosen[base[2]] = (base, None)
        return chosen

    def answer(self, question, history=None, selected=None, max_tokens=None,
               compact=None, memory=None, profile=None, answer_title=None,
               task_state=None, invariants=None):
        """Обрабатывает запрос пользователя и возвращает результат.

        Принимает:
            question — строка с вопросом пользователя;
            history  — список сообщений {role, content} (допустим пустой);
            selected — список dict {label, temperature}: модели, которые нужно
                       опросить, и температура каждой (необязательно; если
                       пусто — опрашиваются все доступные модели);
            max_tokens — int или None: глобальное ограничение числа новых
                        токенов ответа для каждой модели. None — не применять
                        (модель сама выбирает лимит вывода);
            compact — dict {enabled, keep, summary} или None — настройки
                      сжатия истории.
            memory  — dict {"working": {...}, "longterm": {...}} или None:
                      данные памяти агента; фрагменты ответа, совпадающие с
                      ними, подсвечиваются цветом (рабочая — фисташковым,
                      долговременная — фуксией).
            profile — dict {"name", "character", "style"} или None: характер
                      (тон) и характер ответов (формат/длина) активного
                      профиля пользователя. Подставляются в системный промпт,
                      так что меняют поведение КАЖДОЙ модели.
            answer_title — строка-заголовок блока ответа. Если задана (ответ
                      даёт персона) — в шапке карточки показывается имя
                      персоны, а не метка модели.
            task_state — dict формализованного состояния задачи (Task State
                      Machine) или None: цель, этап (planning/execution/
                      validation/done), текущий шаг, ожидаемое действие и
                      признак паузы. Подставляется в системный промпт, чтобы
                      агент продолжал задачу без повторных объяснений.
            invariants — список dict {"text", "category"} или None: жёсткие
                      правила (архитектура/техрешения/стек/бизнес-правила),
                      которые агент НЕ вправе нарушать. Подставляются в
                      системный промпт, а после ответа выполняется
                      ДЕТЕРМИНИРОВАННАЯ пост-проверка (отдельный вызов
                      модели): если ответ нарушает инвариант, он заменяется
                      ОТКАЗОМ с пояснением.
        Возвращает dict, единообразный для успеха и ошибок:
            ok      — True, если хотя бы одна модель ответила;
            text    — текстовое представление ответов;
            html    — HTML-разметка для интерфейса;
            answers — список {label, text, temperature} с ответами моделей;
            meta    — строка служебных данных.
        Внутренние исключения перехватываются и не выходят за пределы агента.
        """
        trace = []                      # «ход запросов» агента для правой панели
        question = str(question or "").strip()
        trace.append({"kind": "enter",
                      "title": "Агент принял запрос пользователя"})
        print("[TRACE] Agent.answer() ВХОД. question=%r" % question, flush=True)
        if not question:
            step = {"kind": "exit", "ok": False,
                    "title": "Агент: пустой вопрос",
                    "detail": "вернул {ok:False, error:'Пустой вопрос.'}"}
            trace.append(step)
            print("[TRACE] Agent.answer() пустой вопрос -> ранний выход", flush=True)
            return Ok(self).value(ok=False, error="Пустой вопрос.", trace=trace)

        # Сжатие истории.
        # Основной путь: сервер уже прислал сжатую историю
        # ([summary] + последние keep сообщений). Тогда повторно сжимать
        # НЕЛЬЗЯ — иначе summary будет отброшен. Здесь _apply_compact
        # используется лишь как fallback, если история пришла полной,
        # но сжатие включено (например, при вызове агента вне сервера).
        already_compacted = (
            isinstance(history, list) and history
            and isinstance(history[0], dict)
            and history[0].get("role") == "system"
        )
        if (not already_compacted and compact
                and compact.get("enabled") and compact.get("summary")):
            history = self._apply_compact(history, compact)
            trace.append({"kind": "act", "title": "Агент: применено сжатие истории",
                          "detail": "сохранено %d последних сообщений, остальное — summary"
                                    % compact.get("keep", config.COMPACT_KEEP)})
        elif already_compacted:
            trace.append({"kind": "act",
                          "title": "Агент: контекст уже сжат (summary + последние)",
                          "detail": "сжатие применено на сервере, повторно не выполняется"})

        messages = self._build_messages(history, question, profile=profile,
                                        task_state=task_state,
                                        invariants=invariants)
        hlen = len(history) if isinstance(history, list) else 0
        prof_note = ""
        if isinstance(profile, dict) and (profile.get("character")
                                          or profile.get("style")):
            prof_note = " | профиль: %s" % (profile.get("name") or "без имени")
        task_note = ""
        if isinstance(task_state, dict) and task_state.get("active"):
            task_note = " | задача: %s%s" % (
                task_state.get("stage", ""),
                " (пауза)" if task_state.get("paused") else "")
        inv_list = [i for i in (invariants or [])
                    if isinstance(i, dict) and str(i.get("text") or "").strip()]
        inv_note = (" | инвариантов: %d" % len(inv_list)) if inv_list else ""
        trace.append({"kind": "act", "title": "Агент собрал сообщения для API",
                      "detail": "%d сообщений (%d из истории + текущий) | системный "
                                "промпт добавлен%s%s%s" % (len(messages), hlen,
                                                          prof_note, task_note,
                                                          inv_note)})
        print("[TRACE] Agent.answer() собрал %d сообщений для API" % len(messages),
              flush=True)

        sources = self._selected_sources(selected)
        trace.append({"kind": "branch", "title": "Агент -> запрос к LLM",
                      "detail": "агент опрашивает %d модель(ей) из выбранных"
                                % len(sources)})
        print("[TRACE] Agent.answer() опрашивает %d источника:"
              % len(sources), flush=True)

        blocks, text_parts, collected = [], [], []
        ok_any = False
        input_total = 0
        output_total = 0
        # Сколько фрагментов ответа заимствовано из памяти (по всем моделям).
        memory_used = {"working": 0, "longterm": 0}
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        mt = self._coerce_max_tokens(max_tokens)
        mt_note = ("max_tokens=%d" % mt) if mt is not None else "max_tokens=auto"

        for label, (base, temperature) in sources.items():
            provider, model, _lbl, cls = base
            temp_note = ("temperature=%.2f" % temperature
                         if temperature is not None else "temperature=auto")
            node = {"kind": "llm", "model": label,
                    "title": "LLM: %s" % label,
                    "detail": "отправка… " + temp_note + " · " + mt_note}
            trace.append(node)
            print("[TRACE]   -> вызов модели label=%r %s %s"
                  % (label, temp_note, mt_note), flush=True)
            single = self._call_one(provider, model, messages, label, temperature,
                                    max_tokens=mt)
            node["ok"] = single["ok"]
            node["detail"] = ("запрос выполнен за %.2f c · ввод %d/вывод %d ток"
                              % (single["elapsed"], single["prompt_tokens"],
                                 single["completion_tokens"]))
            node["dur"] = round(single["elapsed"], 2)
            node["dur_ms"] = int(single["elapsed"] * 1000)
            print("[TRACE]   <- результат %r: ok=%s" % (label, single["ok"]),
                  flush=True)
            input_total += single["prompt_tokens"]
            output_total += single["completion_tokens"]
            if single["ok"]:
                ok_any = True
            # ДЕТЕРМИНИРОВАННАЯ ПОСТ-ПРОВЕРКА ИНВАРИАНТОВ: отдельным
            # вызовом модели проверяем, не нарушает ли ответ инварианты.
            # При нарушении ответ ЗАМЕНЯЕТСЯ отказом с пояснением.
            check = None
            if inv_list and single["ok"] and str(single["content"]).strip():
                check = self.check_invariants(question, single["content"],
                                              inv_list, model=model)
                if check.get("violated"):
                    # Трассируем отказ (видно в правой панели).
                    trace.append({
                        "kind": "act",
                        "title": "Инвариант нарушен — ответ заменён отказом",
                        "detail": "модель %s: %s"
                                  % (label, check.get("invariant") or "инвариант")})
                    ref_html = self._invariant_refusal_html(check,
                                                            title=answer_title
                                                            or label)
                    ref_text = self._invariant_refusal_text(check,
                                                            title=answer_title
                                                            or label)
                    blocks.append(ref_html)
                    text_parts.append(ref_text)
                    collected.append({
                        "label": answer_title or label,
                        "model": label,
                        "text": ref_text,
                        "temperature": temperature,
                        "input": single["prompt_tokens"],
                        "output": single["completion_tokens"],
                        "cost": self._estimate_cost(provider, model,
                                                    single["prompt_tokens"],
                                                    single["completion_tokens"]),
                        "invariant_violation": check,
                    })
                    continue
            block_html, mem_counts = self._render_block(
                provider, model, label, cls, single, memory,
                title=answer_title)
            blocks.append(block_html)
            memory_used["working"] += mem_counts["working"]
            memory_used["longterm"] += mem_counts["longterm"]
            text_parts.append(self._render_text(provider, model, label, single,
                                                title=answer_title))
            collected.append({
                "label": answer_title or label,
                "model": label,
                "text": single["content"],
                "temperature": temperature,
                "input": single["prompt_tokens"],
                "output": single["completion_tokens"],
                "cost": self._estimate_cost(provider, model,
                                            single["prompt_tokens"],
                                            single["completion_tokens"]),
            })

        total_tokens = input_total + output_total
        # Оценка доли входных токенов, пришедшейся на контекст-историю
        # (система и текущий вопрос не считаются историей).
        history_tokens = self._history_token_estimate(messages, input_total)
        meta = self._build_meta(ts, total_tokens, ok_any)
        html = "".join(blocks)
        text = "\n\n".join(text_parts)
        trace.append({"kind": "exit", "ok": ok_any,
                      "title": "Агент возвращает ответ",
                      "detail": "собрано %d ответов · суммарно %d ток · ok=%s"
                                % (len(collected), total_tokens, ok_any)})
        print("[TRACE] Agent.answer() ГОТОВО. ok=%s, токенов=%d, история~%d"
              % (ok_any, total_tokens, history_tokens), flush=True)
        usage = {
            "input": input_total,
            "output": output_total,
            "total": total_tokens,
            "history": history_tokens,
        }
        return Ok(self).value(ok=ok_any, html=html, text=text,
                              answers=collected, meta=meta, trace=trace,
                              usage=usage, memory_used=memory_used)


    # ---- Ветка MCP: запрос идёт через инструменты MCP-сервера ----

    def mcp_select_system_prompt(self, tools):
        """Системный промпт для выбора MCP-инструмента под запрос.

        tools — список dict {name, description, params}. Модель должна вернуть
        СТРОГО JSON: {"tool": "<имя>", "arguments": {...}} либо
        {"tool": null, "answer": "<прямой ответ>"}, если инструмент не нужен.
        """
        # Текущая дата/время: без этого модель подставляет год из обучающих
        # данных или из примеров в описании инструментов (получался 2025).
        now = datetime.now()
        weekdays = ["понедельник", "вторник", "среда", "четверг",
                    "пятница", "суббота", "воскресенье"]
        today = now.strftime("%Y-%m-%d")
        weekday = weekdays[now.weekday()]
        # Явная таблица ближайших дней: модель часто ошибается, вручную
        # вычисляя день недели (например, «суббота» -> среда).
        upcoming = []
        for i in range(0, 7):
            d = now + timedelta(days=i)
            label = {0: "сегодня", 1: "завтра"}.get(i, "")
            upcoming.append("%s — %s%s" % (
                weekdays[d.weekday()], d.strftime("%Y-%m-%d"),
                (" (%s)" % label) if label else ""))
        lines = [
            "Ты — ассистент, который решает задачу, ВЫЗЫВАЯ инструменты MCP.",
            "Сегодня %s (%s), текущее время %s." % (today, weekday,
                                                    now.strftime("%H:%M")),
            "Ближайшие дни (день недели — дата): " + "; ".join(upcoming) + ".",
            "ВАЖНО: для дня недели («в субботу», «в воскресенье») бери дату "
            "из этой таблицы (ближайший такой день), НЕ считай вручную.",
            "Для относительных дат («сегодня», «завтра», «на этой неделе») "
            "вычисляй конкретные даты ОТНОСИТЕЛЬНО этой даты и подставляй "
            "именно текущий год (%s), а не год из примеров." % today[:4],
            "Если указано только время (например, «15:00»), длительность "
            "события по умолчанию — 1 час.",
            "Тебе доступны инструменты MCP-сервера:",
        ]
        for t in (tools or []):
            params = ", ".join(t.get("params") or []) or "без аргументов"
            desc = (t.get("description") or "").strip()
            lines.append("  * %s(%s) — %s" % (t.get("name"), params, desc))
        lines += [
            "",
            "По запросу пользователя выбери ОДИН наиболее подходящий инструмент",
            "и подбери аргументы. Ответь СТРОГО одним JSON-объектом без пояснений:",
            '  {"tool": "<имя инструмента>", "arguments": {<аргументы>}}',
            "Если ни один инструмент не подходит, верни:",
            '  {"tool": null, "answer": "<краткий ответ по существу>"}',
        ]
        return "\n".join(lines)

    def answer_via_mcp(self, question, tools, model=None, temperature=None,
                       max_tokens=None, server_id=None):
        """Отвечает на запрос через MCP: модель выбирает инструмент и аргументы,
        затем инструмент вызывается на MCP-сервере, а его результат возвращается
        как ответ.

        Возвращает dict того же вида, что и answer(): ok/html/text/answers/meta/
        usage/trace/memory_used (+ mcp — детали вызова).
        """
        from . import mcp_client

        trace = []
        question = str(question or "").strip()
        trace.append({"kind": "enter",
                      "title": "Агент: запрос через MCP"})
        if not question:
            trace.append({"kind": "exit", "ok": False,
                          "title": "Стоп: пустой вопрос"})
            return Ok(self).value(ok=False, error="Пустой вопрос.", trace=trace)

        provider, model_name = self._resolve_model(model)
        label = str(model or "").strip() or model_name
        mt = self._coerce_max_tokens(max_tokens)
        temp = self._coerce_temperature(temperature)

        # 1) Модель выбирает инструмент и аргументы.
        messages = [
            {"role": "system", "content": self.mcp_select_system_prompt(tools)},
            {"role": "user", "content": question},
        ]
        trace.append({"kind": "llm", "model": label,
                      "title": "LLM: выбор MCP-инструмента",
                      "detail": "инструментов: %d" % len(tools or [])})
        single = self._call_one(provider, model_name, messages, label, temp,
                                max_tokens=mt)
        input_total = single["prompt_tokens"]
        output_total = single["completion_tokens"]

        if not single["ok"]:
            trace.append({"kind": "exit", "ok": False,
                          "title": "Стоп: модель недоступна",
                          "detail": single.get("error") or ""})
            return Ok(self).value(
                ok=False, trace=trace,
                error=single.get("error") or "Модель выбора инструмента недоступна.",
                usage={"input": input_total, "output": output_total,
                       "total": input_total + output_total, "history": 0})

        decision = self._parse_mcp_decision(single["content"])
        tool_name = (decision or {}).get("tool")
        args = (decision or {}).get("arguments") or {}

        # 2a) Инструмент не выбран — отдаём прямой ответ модели.
        if not tool_name:
            direct = (decision or {}).get("answer") or single["content"]
            trace.append({"kind": "branch", "title": "MCP: инструмент не нужен",
                          "detail": "прямой ответ модели"})
            html = self._mcp_answer_html(question, direct, tool_name=None)
            text = "%s:\n%s" % (label, direct)
            trace.append({"kind": "exit", "ok": True,
                          "title": "Готово: ответ модели (без MCP)"})
            return Ok(self).value(
                ok=True, html=html, text=text,
                answers=[{"label": label, "model": label, "text": direct,
                          "temperature": temperature,
                          "input": input_total, "output": output_total,
                          "cost": self._estimate_cost(
                              provider, model_name, input_total, output_total)}],
                meta=self._build_meta(
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    input_total + output_total, True),
                trace=trace,
                usage={"input": input_total, "output": output_total,
                       "total": input_total + output_total, "history": 0},
                memory_used={"working": 0, "longterm": 0},
                mcp={"tool": None, "arguments": {}, "result": None})

        # 2b) Вызываем выбранный MCP-инструмент.
        trace.append({"kind": "branch", "title": "MCP -> вызов инструмента",
                      "detail": "%s(%s)" % (tool_name, json_dumps_safe(args))})
        call = mcp_client.mcp_call_tool(tool_name, args, server_id=server_id)
        if not call.get("ok"):
            err = call.get("error") or "инструмент вернул ошибку"
            trace.append({"kind": "exit", "ok": False,
                          "title": "Стоп: ошибка MCP-инструмента",
                          "detail": err})
            html = self._mcp_answer_html(question, "Ошибка MCP: %s" % err,
                                         tool_name=tool_name, error=True)
            text = "%s:\nОшибка MCP: %s" % (label, err)
            return Ok(self).value(
                ok=False, html=html, text=text, trace=trace,
                error=err,
                usage={"input": input_total, "output": output_total,
                       "total": input_total + output_total, "history": 0},
                mcp={"tool": tool_name, "arguments": args,
                     "result": call.get("text", "")})

        result_text = call.get("text", "")
        trace.append({"kind": "llm", "model": tool_name,
                      "title": "MCP: результат инструмента",
                      "detail": result_text[:200]})
        html = self._mcp_answer_html(question, result_text, tool_name=tool_name,
                                     arguments=args)
        text = "%s (MCP: %s):\n%s" % (label, tool_name, result_text)
        trace.append({"kind": "exit", "ok": True,
                      "title": "Готово: ответ через MCP"})
        return Ok(self).value(
            ok=True, html=html, text=text,
            answers=[{"label": label, "model": label, "text": text,
                      "temperature": temperature,
                      "input": input_total, "output": output_total,
                      "cost": self._estimate_cost(
                          provider, model_name, input_total, output_total),
                      "mcp_tool": tool_name}],
            meta=self._build_meta(
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                input_total + output_total, True),
            trace=trace,
            usage={"input": input_total, "output": output_total,
                   "total": input_total + output_total, "history": 0},
            memory_used={"working": 0, "longterm": 0},
            mcp={"tool": tool_name, "arguments": args, "result": result_text})

    def compose_message(self, report_text, model=None, fallback=""):
        """Формирует СООБЩЕНИЕ о запуске композиции MCP СИЛАМИ МОДЕЛИ.

        Композиция (календарь → замполит → завхоз) — детерминированный
        пайплайн MCP-серверов. Чтобы задействовать модель, выбранную в
        селекте «Модель MCP», текст-сообщение о результате прогона
        генерирует именно ЭТА модель: она получает сводку пайплайна и
        отвечает коротким понятным сообщением для чата.

        report_text — текстовая сводка результата композиции (серверы,
                       события, сохранённые данные).
        model       — метка модели («GigaChat»/«DeepSeek-flash») или None
                       (тогда используется модель по умолчанию).
        fallback    — запасной текст, если модель недоступна/не ответила.

        Возвращает dict: {ok, text, model, error}. При ошибке модели
        возвращает fallback (ok=False), чтобы сообщение всё равно показалось.
        """
        provider, model_name = self._resolve_model(model)
        label = str(model or "").strip() or model_name
        system = ("Ты — ассистент, который кратко отчитывается о запуске "
                  "композиции MCP-серверов. Отвечай по-русски, дружелюбно, "
                  "1–3 предложениями, без markdown-таблиц.")
        user = ("Запущена КОМПОЗИЦИЯ MCP-серверов. Сводка прогона:\n\n"
                + str(report_text or "") +
                "\n\nСформируй короткое сообщение для чата: что сделала "
                "композиция и какой итог (сколько событий обработано и что "
                "сохранено). Не выдумывай данные, которых нет в сводке.")
        try:
            res = self._chat_text(provider, model_name, [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ], temperature=0.3)
            content = res.get("content", "") if isinstance(res, dict) else str(res)
            text = str(content or "").strip()
            if text:
                print("[COMPOSE] сообщение сгенерировано моделью %s" % label,
                      flush=True)
                return {"ok": True, "text": text, "model": label, "error": None}
            print("[COMPOSE] модель %s вернула пустой текст — fallback" % label,
                  flush=True)
            return {"ok": False, "text": fallback, "model": label,
                    "error": "Пустой ответ модели."}
        except Exception as exc:
            print("[COMPOSE] сообщение: модель %s недоступна (%s) — fallback"
                  % (label, exc), flush=True)
            return {"ok": False, "text": fallback, "model": label,
                    "error": str(exc)}

    @staticmethod
    def _parse_mcp_decision(text):
        """Разбирает JSON-решение модели о вызове MCP-инструмента.

        Возвращает dict {"tool", "arguments"} либо {"tool": None, "answer"}.
        При неудаче — None.
        """
        if not text:
            return None
        s = str(text).strip()
        if s.startswith("```"):
            s = s.strip("`")
            if "\n" in s:
                first, rest = s.split("\n", 1)
                if first.strip().lower() in ("json", ""):
                    s = rest
            s = s.strip()

        def _pick(obj):
            if not isinstance(obj, dict):
                return None
            tool = obj.get("tool") or obj.get("name") or obj.get("tool_name")
            if tool:
                args = (obj.get("arguments") or obj.get("args")
                        or obj.get("params") or {})
                if not isinstance(args, dict):
                    args = {}
                return {"tool": str(tool), "arguments": args}
            ans = obj.get("answer") or obj.get("text") or obj.get("response")
            return {"tool": None, "answer": ("" if ans is None else str(ans))}

        try:
            data = json.loads(s)
            picked = _pick(data)
            if picked is not None:
                return picked
        except Exception:
            pass
        start = s.find("{")
        end = s.rfind("}")
        if start != -1 and end > start:
            try:
                return _pick(json.loads(s[start:end + 1]))
            except Exception:
                return None
        return None

    @staticmethod
    def _mcp_answer_html(question, result_text, tool_name=None,
                         arguments=None, error=False):
        """HTML-карточка ответа через MCP (инструмент + результат)."""
        parts = ['<div class="mcp-answer%s">' %
                 (" mcp-answer-error" if error else "")]
        if tool_name:
            args = json_dumps_safe(arguments or {})
            parts.append('<div class="mcp-answer-head">MCP-инструмент: '
                         '<b>%s</b>(%s)</div>' % (html_report.escape_html(tool_name),
                                                  html_report.escape_html(args)))
        else:
            parts.append('<div class="mcp-answer-head">Ответ модели '
                         '(инструмент не задействован)</div>')
        parts.append('<div class="mcp-answer-body"><pre>%s</pre></div>'
                     % html_report.escape_html(result_text or ""))
        parts.append("</div>")
        return "".join(parts)


    # ---- приватная логика запроса (инкапсулирована в агенте) ----

    @staticmethod
    def _apply_compact(history, compact):
        """Применяет сжатие к истории диалога.

        history — полный список сообщений.
        compact — dict {enabled, keep, summary}.
        Возвращает новый список: summary + последние keep сообщений.
        """
        if not history or not compact.get("summary"):
            return history
        keep = max(0, compact.get("keep", config.COMPACT_KEEP))
        # keep = 0 → сжатие не применяется: возвращаем полную историю.
        if keep <= 0:
            return history
        if keep >= len(history):
            return history
        recent = list(history[-keep:]) if keep > 0 else []
        summary_msg = {"role": "system", "content": compact["summary"]}
        return [summary_msg] + recent

    @staticmethod
    def profile_system_prompt(profile):
        """Собирает системную инструкцию из профиля (характер + стиль).

        profile — dict {"name", "character", "style"} или None. Возвращает
        строку-инструкцию либо "" (пустую строку), если задавать нечего.

        ХАРАКТЕР (character) задаёт ТОН общения (например, «дружелюбный»),
        а ХАРАКТЕР ОТВЕТОВ (style) — ФОРМАТ и ДЛИНУ (например, «кратко,
        по пунктам»). Инструкция подставляется в системный промпт, поэтому
        применяется к каждой модели одинаково.
        """
        if not isinstance(profile, dict):
            return ""
        character = str(profile.get("character") or "").strip()
        style = str(profile.get("style") or "").strip()
        name = str(profile.get("name") or "").strip()
        parts = []
        if character:
            parts.append("Характер общения (тон): %s." % character)
        if style:
            parts.append("Характер ответов (формат и длина): %s." % style)
        if not parts:
            return ""
        head = "Персона, от лица которой ты отвечаешь"
        if name:
            head += " («%s»)" % name
        return head + ": " + " ".join(parts)

    @staticmethod
    def task_system_prompt(task_state):
        """Собирает блок системного промпта из формализованного состояния задачи.

        task_state — dict (см. rtk_app.task_state.TaskState) или None.
        Возвращает строку-инструкцию либо "" (если задачи нет).

        Блок описывает цель, текущий этап (planning/execution/validation/
        done), текущий шаг и ожидаемое действие. Если задача НА ПАУЗЕ — явно
        указываем агенту продолжить с того же этапа/шага, НЕ прося
        пользователя объяснять задачу заново.
        """
        try:
            from .task_state import TaskState
            ts = TaskState(task_state)
        except Exception:
            return ""
        if not ts.active:
            return ""
        block = ts.system_prompt_block()
        # Инструкция: сообразовывать ответ с текущим этапом задачи.
        block += ("\nИнструкция: веди ответ сообразно этапу и шагу задачи; "
                  "если задача на паузе — по запросу продолжай с текущего "
                  "места, не требуя повторно объяснять задачу.")
        return block

    @staticmethod
    def invariants_system_prompt(invariants):
        """Собирает блок инвариантов для системного промпта (или "").

        invariants — список dict {"text", "category"} (или None). Возвращает
        строку-инструкцию с жёсткими правилами, сгруппированными по категориям,
        и требованием ОТКАЗАТЬСЯ от решения, которое нарушает любой инвариант.
        """
        items = [i for i in (invariants or []) if isinstance(i, dict)
                 and str(i.get("text") or "").strip()]
        if not items:
            return ""
        by_cat = {}
        for i in items:
            cat = str(i.get("category") or "business")
            by_cat.setdefault(cat, []).append(str(i.get("text")).strip())
        label_map = {c: label for c, label in config.INVARIANT_CATEGORIES}
        lines = ["ИНВАРИАНТЫ (жёсткие правила — НАРУШАТЬ НЕЛЬЗЯ):"]
        # Сначала известные категории в порядке config, затем прочие.
        ordered = [c for c, _l in config.INVARIANT_CATEGORIES]
        for code in ordered:
            if code in by_cat:
                lines.append("%s:" % label_map.get(code, code))
                for t in by_cat[code]:
                    lines.append("  - %s" % t)
                by_cat.pop(code)
        for code, texts in by_cat.items():
            lines.append("%s:" % label_map.get(code, code))
            for t in texts:
                lines.append("  - %s" % t)
        lines.append(
            "Если запрос пользователя противоречит хотя бы одному "
            "инварианту — ОТКАЖИСЬ предлагать нарушающее решение и кратко "
            "объясни, какой именно инвариант нарушен и почему.")
        return "\n".join(lines)

    def check_invariants(self, question, answer_text, invariants, model=None):
        """ДЕТЕРМИНИРОВАННАЯ пост-проверка ответа на нарушение инвариантов.

        Отдельным вызовом модели спрашивает: «нарушает ли ответ инварианты?».
        Возвращает dict:
            checked   — проводилась ли проверка (есть ли инварианты и ответ);
            violated  — True, если ответ нарушает хотя бы один инвариант;
            invariant — текст нарушенного инварианта (если нарушен);
            reason    — объяснение нарушения (кратко);
            raw       — сырой ответ модели-проверяющего (для отладки);
            error     — сообщение об ошибке (если проверка не удалась).
        При любой неопределённости (сбой/неразборчивый ответ) violated=False,
        чтобы не блокировать нормальную выдачу.
        """
        items = [i for i in (invariants or []) if isinstance(i, dict)
                 and str(i.get("text") or "").strip()]
        answer_text = str(answer_text or "").strip()
        question = str(question or "").strip()
        if not items or not answer_text:
            return {"checked": False, "violated": False, "invariant": "",
                    "reason": "", "raw": "", "error": None}

        rules = []
        for i, it in enumerate(items, 1):
            rules.append("%d) [%s] %s" % (
                i, it.get("category", "business"),
                str(it.get("text")).strip()))
        prompt = "\n".join([
            "Ты — строгий контролёр инвариантов. Проверь, нарушает ли",
            "ОТВЕТ АССИСТЕНТА хотя бы один из ИНВАРИАНТОВ (жёстких правил).",
            "Инварианты — это ограничения (архитектура, техрешения, стек,",
            "бизнес-правила), которые НАРУШАТЬ НЕЛЬЗЯ.",
            "",
            "ИНВАРИАНТЫ:",
            "\n".join(rules),
            "",
            "ЗАПРОС ПОЛЬЗОВАТЕЛЯ:",
            question or "(нет)",
            "",
            "ОТВЕТ АССИСТЕНТА:",
            answer_text,
            "",
            "Если ответ нарушает или предлагает решение, нарушающее хотя бы",
            "один инвариант — верни STRICT JSON:",
            '  {"violated": true, "invariant": "<текст инварианта>", '
            '"reason": "<кратко, чем нарушен>"}',
            "Если нарушений НЕТ — верни STRICT JSON:",
            '  {"violated": false, "invariant": "", "reason": ""}',
            "Только JSON, без пояснений и markdown.",
        ])
        provider, model_name = self._resolve_model(model)
        try:
            data, content = self._chat_json(provider, model_name, prompt,
                                            temperature=0.0)
            if not isinstance(data, dict):
                return {"checked": True, "violated": False, "invariant": "",
                        "reason": "", "raw": content,
                        "error": "Не удалось разобрать ответ контролёра."}
            violated = data.get("violated")
            if isinstance(violated, str):
                violated = violated.strip().lower() in ("true", "1", "да", "yes")
            else:
                violated = bool(violated)
            print("[INVARIANT] проверка: violated=%s" % violated, flush=True)
            return {"checked": True,
                    "violated": violated,
                    "invariant": str(data.get("invariant", "") or ""),
                    "reason": str(data.get("reason", "") or ""),
                    "raw": content,
                    "error": None}
        except Exception as exc:
            print("[INVARIANT] ошибка проверки: %s" % exc, flush=True)
            return {"checked": True, "violated": False, "invariant": "",
                    "reason": "", "raw": "", "error": str(exc)}

    @staticmethod
    def _invariant_refusal_html(check, title=None):
        """HTML-карточка ОТКАЗА от решения, нарушающего инвариант."""
        inv = (check or {}).get("invariant") or "(инвариант)"
        reason = (check or {}).get("reason") or ""
        title = title or "Ассистент"
        reason_html = ("<div class='invariant-refuse-reason'>%s</div>"
                       % html_report.escape_html(reason)) if reason else ""
        return (
            "<div class='variant variant-invariant-refuse'>"
            "<div class='variant-head'>"
            "<span class='variant-name'>%s</span>"
            "<span class='variant-badge'>отказ: нарушение инварианта</span>"
            "</div>"
            "<div class='variant-body'>"
            "<p><b>Не могу предложить такое решение</b> — оно нарушает "
            "инвариант:</p><blockquote>%s</blockquote>%s"
            "</div></div>"
            % (html_report.escape_html(title),
               html_report.escape_html(inv), reason_html)
        )

    @staticmethod
    def _invariant_refusal_text(check, title=None):
        """Текстовое представление отказа (для text-части результата)."""
        inv = (check or {}).get("invariant") or "(инвариант)"
        reason = (check or {}).get("reason") or ""
        title = title or "Ассистент"
        line = ("%s: отказ — решение противоречит инварианту: %s"
                % (title, inv))
        if reason:
            line += " Причина: %s" % reason
        return line

    def advance_task(self, task_state, question, answer="", model=None):
        """Определяет ПЕРЕХОД состояния задачи по ходу пользователя.

        Задаёт модели (по умолчанию GigaChat, либо переданную в model)
        вопрос: какой следующий этап/шаг у задачи, исходя из текущего
        состояния и нового сообщения. Возвращает dict:
            {"stage": "execution"|…, "step": "…", "expected": "…",
             "note": "…"} — предлагаемый переход; либо {} при неудаче.

        Агент сам НЕ меняет состояние — решение о применении перехода
        принимает вызывающий код (через SessionStore.advance_task, где
        проверяется корректность перехода).
        """
        try:
            from .task_state import TaskState, STAGES, STAGE_LABELS
        except Exception:
            return {}
        ts = TaskState(task_state)
        if not ts.active:
            return {}
        lines = [
            "Ты управляешь конечным автоматом ЗАДАЧИ (этапы: planning ->",
            "execution -> validation -> done). Определи НОВОЕ состояние",
            "задачи после нового сообщения пользователя.",
            "",
            "Текущее состояние задачи:",
            "Цель: %s" % (ts.goal or "не указана"),
            "Этап: %s" % ts.stage,
            "Шаг: %s" % (ts.step or "—"),
            "Ожидаемое действие: %s" % (ts.expected or "—"),
            "На паузе: %s" % ("да" if ts.paused else "нет"),
            "",
            "Новое сообщение пользователя: " + str(question or ""),
        ]
        if answer:
            lines += ["", "Ответ ассистента (кратко): " + str(answer)[:500]]
        lines += [
            "",
            "Верни СТРОГО JSON-объект с полями:",
            '  "stage": один из planning|execution|validation|done;',
            '  "step": краткое описание текущего шага;',
            '  "expected": что ожидается дальше (действие/ввод);',
            '  "note": короткое пояснение перехода.',
            "Разрешённые переходы: planning->execution;",
            "execution->validation; validation->done или validation->execution",
            "(если проверка нашла недочёт). Этап может остаться тем же.",
            "Только JSON, без пояснений и markdown.",
        ]
        prompt = "\n".join(lines)
        provider, model_name = self._resolve_model(model)
        try:
            data, _content = self._chat_json(provider, model_name, prompt,
                                             temperature=0.1)
            if not isinstance(data, dict):
                return {}
            stage = str(data.get("stage", "")).strip().lower()
            if stage not in STAGES:
                return {}
            return {
                "stage": stage,
                "step": str(data.get("step", "") or ""),
                "expected": str(data.get("expected", "") or ""),
                "note": str(data.get("note", "") or ""),
            }
        except Exception as exc:
            print("[TASK] не удалось определить переход: %s" % exc, flush=True)
            return {}

    def _resolve_model(self, model):
        """Определяет (провайдер, имя модели) по метке или откатывается.

        model — метка модели («GigaChat»/«DeepSeek-flash») либо имя модели,
        либо None. Возвращает кортеж (provider, model_name). Если метка
        неизвестна или не задана — используется GigaChat (модель по умолчанию
        для задач состояния).
        """
        label = str(model or "").strip()
        base = self.by_label.get(label)
        if base is None:
            # Может, передали ИМЯ модели, а не метку — поищем по имени.
            for provider, name, _lbl, _cls in self.sources:
                if name == label:
                    return provider, name
            return "gigachat", config.GC_MODEL
        provider, name, _lbl, _cls = base
        return provider, name

    def _chat_text(self, provider, model, messages, temperature=None,
                   max_tokens=None):
        """Вызывает одну модель и возвращает её ответ (dict с content/tokens).

        Единая точка для вспомогательных LLM-операций (состояние задачи,
        summary, facts): выбирает провайдера по имени. Исключения всплывают
        наружу — вызывающий код обрабатывает их сам.
        """
        if provider == "deepseek":
            return deepseek.chat(self.api_key, messages, model=model,
                                 temperature=temperature, max_tokens=max_tokens)
        return gigachat.chat(messages, model=model,
                             temperature=temperature, max_tokens=max_tokens)

    def _chat_json(self, provider, model, prompt, temperature=None,
                   max_tokens=None, system=STRICT_JSON_SYSTEM):
        """Вызывает модель и разбирает её ответ как JSON-объект (или None).

        Единая точка для вспомогательных LLM-операций, ожидающих строго
        JSON (инварианты, переход задачи, факты). Системная инструкция
        по умолчанию — STRICT_JSON_SYSTEM. Разбор — общим parse_facts_json,
        терпимым к ```json-обёрткам. Исключения всплывают наружу —
        вызывающий код сам решает, что делать при сбое.
        """
        res = self._chat_text(provider, model, [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ], temperature=temperature, max_tokens=max_tokens)
        content = res.get("content", "") if isinstance(res, dict) else str(res)
        return parse_facts_json(content), content

    def _build_messages(self, history, question, profile=None, task_state=None,
                        invariants=None):
        """Собирает полный список сообщений для API (системный промпт + диалог).

        ВАЖНО: из истории сохраняются НЕ только user/assistant, но и
        SYSTEM-сообщения (память агента, summary сжатия, состояние задачи).
        Раньше они отбрасывались, из-за чего модель НЕ получала память
        (рабочую и долговременную) и summary — будто памяти не существует.

        Все системные сообщения (промпт агента + память + summary + профиль +
        состояние задачи + инварианты) ОБЪЕДИНЯЮТСЯ в ОДНО ведущее
        system-сообщение: не все провайдеры корректно принимают несколько
        system-сообщений, а GigaChat ожидает системную инструкцию в начале.
        Диалог (user/assistant) идёт далее в исходном порядке, затем —
        текущий вопрос пользователя.
        """
        system_parts = [SYSTEM_PROMPT]
        # Характер/стиль профиля (персоны) — задают тон и формат ответов.
        prof_prompt = self.profile_system_prompt(profile)
        if prof_prompt:
            system_parts.append(prof_prompt)
        # Формализованное состояние задачи (Task State Machine): этап, шаг,
        # ожидаемое действие, пауза — чтобы агент продолжал задачу.
        task_prompt = self.task_system_prompt(task_state)
        if task_prompt:
            system_parts.append(task_prompt)
        # ИНВАРИАНТЫ: жёсткие правила, которые нарушать нельзя. Явно
        # добавляем их в системную часть, чтобы агент учитывал их в
        # рассуждениях и отказывался от решений, нарушающих правила.
        inv_prompt = self.invariants_system_prompt(invariants)
        if inv_prompt:
            system_parts.append(inv_prompt)
        dialog = []
        if isinstance(history, list):
            for m in history:
                if (isinstance(m, dict)
                        and isinstance(m.get("content"), str)
                        and m["content"]):
                    role = m.get("role")
                    if role == "system":
                        # Память агента и summary — в общую системную часть.
                        system_parts.append(m["content"])
                    elif role in ("user", "assistant"):
                        dialog.append({"role": role, "content": m["content"]})
        messages = [{"role": "system", "content": "\n\n".join(system_parts)}]
        messages.extend(dialog)
        messages.append({"role": "user", "content": question})
        return messages

    @staticmethod
    def _history_token_estimate(messages, input_tokens):
        """Оценка числа входных токенов, приходящихся на историю диалога.

        Точной разбивки по сообщениям API не даёт, поэтому распределяем
        фактическое число входных токенов пропорционально длине текста:
        история — это все сообщения, кроме системного промпта (первое) и
        текущего вопроса пользователя (последнее).
        """
        try:
            total_chars = sum(len(str(m.get("content", ""))) for m in messages)
            hist_chars = sum(len(str(m.get("content", "")))
                             for m in messages[1:-1])
        except Exception:
            return 0
        if total_chars <= 0 or input_tokens <= 0:
            return 0
        return int(round(input_tokens * hist_chars / total_chars))

    @staticmethod
    def _estimate_cost(provider, model, prompt_tokens, completion_tokens):
        """Оценка стоимости запроса в юанях.

        Возвращает число (¥) для моделей DeepSeek по известным ценам или
        None, если цена для модели неизвестна (например, GigaChat).
        """
        if provider != "deepseek":
            return None
        in_price = config.DS_PRICE_INPUT_PER_M.get(model)
        out_price = config.DS_PRICE_OUTPUT_PER_M.get(model)
        if in_price is None or out_price is None:
            return None
        return (prompt_tokens * in_price
                + completion_tokens * out_price) / 1_000_000.0

    def _call_one(self, provider, model, messages, label, temperature=None,
                  max_tokens=None):
        """Вызывает одну конкретную модель через API и фиксирует результат.

        Любые ошибки превращаются в "мягкий" результат с признаком ok=False,
        чтобы сбой одной модели не ломал весь агент.
        """
        t0 = time.perf_counter()
        try:
            if provider == "deepseek":
                res = deepseek.chat(self.api_key, messages,
                                    model=model, temperature=temperature,
                                    max_tokens=max_tokens)
            else:
                res = gigachat.chat(messages, model=model,
                                    temperature=temperature,
                                    max_tokens=max_tokens)
            return {
                "ok": True,
                "content": res.get("content", ""),
                "prompt_tokens": int(res.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(res.get("completion_tokens", 0) or 0),
                "elapsed": time.perf_counter() - t0,
                "error": None,
            }
        except Exception as exc:
            return {
                "ok": False,
                "content": "[Ошибка модели %s: %s]" % (label, exc),
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "elapsed": time.perf_counter() - t0,
                "error": str(exc),
            }

    def _render_block(self, provider, model, label, cls, single, memory=None,
                      title=None):
        """HTML-блок (карточка ответа одной модели) и счётчики памяти.

        В заголовке карточки показываем title (имя персоны, если ответ даёт
        персона) либо метку модели. Время, токены и стоимость в шапке ответа
        не выводим (метрики остаются в текстовом представлении и статистике).

        Фрагменты ответа, совпадающие с данными памяти агента (memory),
        подсвечиваются: рабочая — фисташковым, долговременная — фуксией.
        Возвращает кортеж (html, counts), где counts = {"working": N,
        "longterm": M} — сколько фрагментов заимствовано из каждой памяти.
        """
        content = single["content"]
        counts = {"working": 0, "longterm": 0}
        try:
            frag, counts = html_report.render_with_memory_counts(
                content, memory=memory)
        except Exception:
            frag = html_report.escape_html(content)

        block = (
            "<div class='variant variant-%s'>"
            "<div class='variant-head'>"
            "<span class='variant-name'>%s</span>"
            "</div>"
            "<div class='variant-body'>%s</div></div>"
            % (cls, title or label, frag)
        )
        return block, counts

    def _render_text(self, provider, model, label, single, title=None):
        """Текстовая строка-представление ответа одной модели.

        Показываем title (имя персоны) либо метку модели и её ответ — без
        времени, токенов и стоимости (метрики остаются в статистике сессии
        и служебных логах).
        """
        return "%s:\n%s" % (title or label, single["content"])

    def _build_meta(self, ts, total_tokens, ok_any):
        """Служебная строка с информацией о генерации."""
        sources = ", ".join(self.enabled_models)
        if not ok_any:
            return ("Сгенерировано: %s · есть ошибки моделей · моделей: %s"
                    % (ts, sources))
        return ("Сгенерировано: %s · суммарно токенов: %d · моделей: %s"
                % (ts, total_tokens, sources))

    # ---- вспомогательные операции с историей (summary / facts) ----

    def compact_history(self, messages, keep=None):
        """Генерирует summary для ВЫТЕСНЯЕМОЙ части истории диалога.

        messages — полный список сообщений диалога (без системного промпта).
        keep — сколько последних сообщений НЕ сжимать (остаются как есть).

        Возвращает строку summary (по части истории до последних keep
        сообщений) или None, если сжимать нечего / произошла ошибка.
        """
        keep = config.COMPACT_KEEP if keep is None else max(0, int(keep))
        # Сжимаем только то, что вытесняется из контекста: всё, кроме
        # последних keep сообщений. Если истории мало — сжимать нечего.
        tail = messages[-keep:] if keep > 0 else []
        head = messages[:-keep] if keep > 0 else list(messages)
        if len(head) < config.COMPACT_MIN:
            return None
        return self.compact_update("", head)

    def compact_update(self, prev_summary, new_messages):
        """Инкрементально ДОПИСЫВАЕТ вытесненные сообщения в summary (Вариант A).

        prev_summary — уже существующее summary (может быть пустым).
        new_messages — НОВЫЕ вытесненные сообщения (ещё не сжатые).

        Возвращает обновлённое summary (строку) или None при ошибке/пустоте.
        Новое summary = слияние прежнего текста и нового фрагмента.
        """
        new_messages = [m for m in (new_messages or []) if isinstance(m, dict)]
        if not new_messages:
            return None

        cap = config.COMPACT_MSG_CAP
        lines = [
            "Обнови краткое summary диалога, добавив в него новый фрагмент.",
            "Сохрани ключевые факты, темы, договорённости и решения,",
            "чтобы по summary можно было продолжить беседу.",
            "Не повторяйся, объедини прежнее и новое в единый связный текст.",
            "",
        ]
        if prev_summary:
            lines += ["Прежнее summary:", str(prev_summary), ""]
        lines.append("Новый фрагмент диалога (вытеснен из контекста):")
        for m in new_messages:
            role = m.get("role", "unknown")
            content = str(m.get("content", ""))
            if not content:
                continue
            if len(content) > cap:
                content = content[:cap] + " […]"
            if role == "user":
                lines.append("Пользователь: " + content)
            elif role == "assistant":
                lines.append("Ассистент: " + content)

        prompt = "\n\n".join(lines)
        messages_for_compact = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        try:
            t0 = time.perf_counter()
            res = gigachat.chat(
                messages_for_compact,
                model=config.GC_MODEL,
                temperature=0.3,
            )
            elapsed = time.perf_counter() - t0
            summary = res.get("content", "") if isinstance(res, dict) else str(res)
            if summary:
                print("[COMPACT] summary дополнен %.2f c, длина %d, +%d сообщ."
                      % (elapsed, len(summary), len(new_messages)), flush=True)
            return summary
        except Exception as exc:
            print("[COMPACT] ошибка: %s" % exc, flush=True)
            return None

    def update_facts(self, prev_facts, new_messages):
        """Обновляет блок facts (key-value) после нового фрагмента диалога.

        prev_facts — текущий словарь фактов (может быть пустым).
        new_messages — новые сообщения (последний ход пользователя/ассистента).

        Возвращает обновлённый словарь фактов или prev_facts при ошибке.
        Факты извлекаются моделью GigaChat в формате JSON key-value:
        цель, ограничения, предпочтения, решения, договорённости и т.п.
        """
        prev_facts = {str(k): str(v) for k, v in (prev_facts or {}).items()}
        new_messages = [m for m in (new_messages or []) if isinstance(m, dict)]
        if not new_messages:
            return prev_facts

        cap = config.COMPACT_MSG_CAP
        lines = [
            "Ты ведёшь блок фактов (key-value) о диалоге.",
            "Обнови факты, добавив/изменив важное из нового фрагмента диалога.",
            "Храни: цель, ограничения, предпочтения, решения, договорённости.",
            "Верни ТОЛЬКО валидный JSON-объект (словарь строка-строка),",
            "без пояснений и без markdown. Если факт устарел — убери его.",
            "",
        ]
        if prev_facts:
            lines += ["Текущие факты (JSON):",
                      json_dumps_safe(prev_facts), ""]
        else:
            lines += ["Текущих фактов нет.", ""]
        lines.append("Новый фрагмент диалога:")
        for m in new_messages:
            role = m.get("role", "unknown")
            content = str(m.get("content", ""))
            if not content:
                continue
            if len(content) > cap:
                content = content[:cap] + " […]"
            if role == "user":
                lines.append("Пользователь: " + content)
            elif role == "assistant":
                lines.append("Ассистент: " + content)

        prompt = "\n\n".join(lines)
        try:
            t0 = time.perf_counter()
            facts, _content = self._chat_json(
                "gigachat", config.GC_MODEL, prompt, temperature=0.2)
            elapsed = time.perf_counter() - t0
            if facts is not None:
                print("[FACTS] факты обновлены %.2f c: %d ключей"
                      % (elapsed, len(facts)), flush=True)
                return facts
            print("[FACTS] не удалось разобрать JSON, оставляем прежние факты",
                  flush=True)
            return prev_facts
        except Exception as exc:
            print("[FACTS] ошибка: %s" % exc, flush=True)
            return prev_facts


class Ok:
    """Маленький помощник конструирования единообразного результата агента."""

    def __init__(self, agent):
        self.agent = agent

    def value(self, **kwargs):
        d = {"ok": False, "html": "", "text": "", "answers": [],
             "meta": "", "model": self.agent.label,
             "usage": {"input": 0, "output": 0, "total": 0, "history": 0}}
        d.update(kwargs)
        d.setdefault("error", None)
        return d

    def error(self, message):
        return self.value(ok=False, error=message)
