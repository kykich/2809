# -*- coding: utf-8 -*-
"""Регресс-проверка: память агента РЕАЛЬНО попадает в запрос к модели.

Ранее Agent._build_messages() фильтровал историю только по
role in ("user","assistant") и ТЕРЯЛ system-сообщения — память агента
(рабочую/долговременную) и summary сжатия. Из-за этого модель «не видела»
память и отвечала «у меня нет доступа к рабочей памяти».

Эта проверка собирает сообщения для API и убеждается, что данные рабочей и
долговременной памяти (а также summary) присутствуют в системной части.
Сеть/модели не задействованы.
"""
import os
import sys
import tempfile

# Корень проекта — на уровень выше tests/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtk_app.agent import Agent
from rtk_app.session_store import SessionStore
from tests.harness import check, finish


def _fresh_store():
    tmpdir = tempfile.mkdtemp(prefix="memreq_")
    return SessionStore(path=os.path.join(tmpdir, "session.json")), tmpdir


def _sys_blob(messages):
    """Всё содержимое system-сообщений собранного запроса, одной строкой."""
    return "\n".join(m.get("content", "") for m in messages
                     if m.get("role") == "system")


def main():
    agent = Agent(api_key="dummy")  # ключ не используется: сеть не вызывается

    # 1) Память (рабочая + долговременная) есть -> она должна быть в запросе.
    st, _ = _fresh_store()
    st.set_memory_key("working", "дедлайн", "1 октября")
    st.set_memory_key("working", "задача", "подготовить отчёт")
    st.set_memory_key("longterm", "имя", "Иван")
    history = st.get_context_messages()
    messages = agent._build_messages(history, "какой дедлайн?")
    blob = _sys_blob(messages)

    check("в запрос добавлено system-сообщение (память не потеряна)",
          any(m.get("role") == "system" for m in messages))
    check("данные РАБОЧЕЙ памяти дошли до модели (дедлайн)",
          "1 октября" in blob)
    check("данные РАБОЧЕЙ памяти дошли до модели (задача)",
          "подготовить отчёт" in blob)
    check("данные ДОЛГОВРЕМЕННОЙ памяти дошли до модели (имя)",
          "Иван" in blob)
    check("упомянуты оба слоя памяти",
          "Рабочая память" in blob and "Долговременная память" in blob)
    check("системный промпт агента идёт первым",
          messages and messages[0].get("role") == "system"
          and "ассистент" in messages[0].get("content", "").lower())
    check("текущий вопрос — последнее сообщение",
          messages and messages[-1] == {"role": "user",
                                        "content": "какой дедлайн?"})
    check("единственное system-сообщение (унифицировано)",
          sum(1 for m in messages if m.get("role") == "system") == 1)

    # 2) Пустая память -> лишнего system-сообщения с памятью нет,
    #    но системный промпт агента остаётся.
    st2, _ = _fresh_store()
    st2.append_turn("привет", {"role": "assistant", "content": "здравствуй"})
    msgs2 = agent._build_messages(st2.get_context_messages(), "и снова привет")
    check("при пустой памяти блока памяти нет",
          "Рабочая память" not in _sys_blob(msgs2)
          and "Долговременная память" not in _sys_blob(msgs2))
    check("системный промпт агента всё равно присутствует",
          msgs2 and msgs2[0].get("role") == "system")

    # 3) Диалог (user/assistant) сохраняется в правильном порядке.
    roles = [m["role"] for m in msgs2]
    check("порядок ролей: system, user, assistant, user",
          roles == ["system", "user", "assistant", "user"])

    print()
    sys.exit(finish("Итог"))


if __name__ == "__main__":
    main()
