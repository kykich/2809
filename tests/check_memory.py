# -*- coding: utf-8 -*-
"""Офлайн-проверка модели памяти агента (без сети).

Проверяет требования задания:
  A. три типа памяти: краткосрочная / рабочая / долговременная;
  B1. типы хранятся РАЗДЕЛЬНО (разные поля файла session.json);
  B2. запись в рабочую/долговременную память — ЯВНАЯ (по типу и ключу).

Использует временный путь к файлу сессии, сеть/модели не задействованы.
Запуск:  python tests/check_memory.py
"""
import os
import sys
import tempfile

# Корень проекта — на уровень выше tests/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtk_app.session_store import SessionStore


def _fresh_store():
    tmpdir = tempfile.mkdtemp(prefix="memtest_")
    return SessionStore(path=os.path.join(tmpdir, "session.json")), tmpdir


def test_three_types_are_separate():
    st, _ = _fresh_store()
    # Краткосрочная память = сам диалог (заполняется автоматически).
    st.append_turn("привет", {"role": "assistant", "content": "здравствуй"})
    # Рабочая память — текущая задача (записываем ЯВНО).
    st.set_memory_key("working", "задача", "реализовать память агента")
    st.set_memory_key("working", "шаг", "написать тесты")
    # Долговременная память — профиль/решения/знания (тоже ЯВНО).
    st.set_memory_key("longterm", "имя", "Иван")
    st.set_memory_key("longterm", "решение", "хранить память в JSON")

    state = st.memory_state()
    assert state["short"]["items"] == 2, state["short"]
    assert state["working"]["задача"] == "реализовать память агента"
    assert state["working"]["шаг"] == "написать тесты"
    assert state["longterm"]["имя"] == "Иван"
    # Краткосрочная (диалог) НЕ смешивается с рабочей/долговременной.
    assert "задача" not in state["longterm"]
    assert "имя" not in state["working"]
    print("[OK] A/B1: три типа памяти хранятся раздельно")


def test_short_is_not_manually_writable():
    st, _ = _fresh_store()
    try:
        st.set_memory_key("short", "ключ", "значение")
        raise AssertionError("запись в 'short' должна быть запрещена")
    except ValueError:
        pass
    print("[OK] B2: краткосрочная память ('short') не редактируется вручную")


def test_explicit_routing():
    st, _ = _fresh_store()
    # Один и тот же ключ в разных слоях не конфликтует — куда пишем, задаём явно.
    st.set_memory_key("working", "цель", "задача-1")
    st.set_memory_key("longterm", "цель", "миссия компании")
    assert st.get_memory("working")["цель"] == "задача-1"
    assert st.get_memory("longterm")["цель"] == "миссия компании"
    # Удаляем ЯВНО из одного слоя — второй не затрагивается.
    assert st.delete_memory_key("working", "цель") is True
    assert "цель" not in st.get_memory("working")
    assert st.get_memory("longterm")["цель"] == "миссия компании"
    print("[OK] B2: явный выбор слоя — ключи не пересекаются")


def test_persistence_roundtrip():
    st, tmpdir = _fresh_store()
    path = os.path.join(tmpdir, "session.json")
    st.set_memory_key("working", "задача", "сделать отчёт")
    st.set_memory_key("longterm", "профиль", "разработчик Python")
    # Новый экземпляр читает те же данные из файла.
    st2 = SessionStore(path=path)
    w = st2.get_memory("working")
    l = st2.get_memory("longterm")
    assert w.get("задача") == "сделать отчёт", w
    assert l.get("профиль") == "разработчик Python", l
    print("[OK] B1: слои сохраняются в JSON и восстанавливаются раздельно")


def test_reset_keeps_longterm():
    st, _ = _fresh_store()
    st.set_memory_key("working", "задача", "v1")
    st.set_memory_key("longterm", "имя", "Иван")
    st.append_turn("вопрос", {"role": "assistant", "content": "ответ"})
    st.reset()
    # Рабочая память и диалог сброшены; долговременная — сохранена.
    assert st.get_memory("working") == {}
    assert st.snapshot() == []
    assert st.get_memory("longterm")["имя"] == "Иван"
    print("[OK] reset: рабочая память чистится, долговременная переносится")


def test_memory_in_prompt():
    st, _ = _fresh_store()
    st.set_memory_key("working", "задача", "написать код")
    st.set_memory_key("longterm", "язык", "русский")
    msgs = st.get_context_messages()
    sys_msgs = [m for m in msgs if m.get("role") == "system"]
    blob = "\n".join(m.get("content", "") for m in sys_msgs)
    assert "рабочая память" in blob.lower()
    assert "долговременная память" in blob.lower()
    assert "написать код" in blob and "русский" in blob
    print("[OK] память подмешивается в контекст запроса (working+longterm)")


def test_facts_merge_not_wipe():
    """Факты ДОПОЛНЯЮТ рабочую память, а не стирают её после запроса."""
    st, _ = _fresh_store()
    st.set_memory_key("working", "пользователь", "Иван")
    # Авто-обновление фактов приносит новый ключ — прежний должен остаться.
    st.set_facts({"цель": "отчёт"})
    w = st.get_memory("working")
    assert w.get("пользователь") == "Иван", w
    assert w.get("цель") == "отчёт", w
    # Дополнение через merge тоже сохраняет прежние ключи.
    st.merge_memory("working", {"шаг": "собрать данные"})
    w2 = st.get_memory("working")
    assert w2.get("пользователь") == "Иван" and w2.get("цель") == "отчёт" \
        and w2.get("шаг") == "собрать данные", w2
    print("[OK] факты ДОПОЛНЯЮТ рабочую память (без очистки)")


def test_memory_usage_counts():
    """Счётчик «раз использования» вида памяти растёт за обмены с памятью."""
    st, _ = _fresh_store()
    # Обмен 1: задействованы обе памяти.
    st.add_memory_usage(working=2, longterm=1)
    # Обмен 2: только рабочая.
    st.add_memory_usage(working=3, longterm=0)
    # Обмен 3: память не использовалась.
    st.add_memory_usage(working=0, longterm=0)
    ctx = st.context_stats()
    # Рабочая: 2 обмена с ненулевым использованием -> count = 2.
    assert ctx["memory_use_count_working"] == 2, ctx
    # Долговременная: 1 обмен -> count = 1.
    assert ctx["memory_use_count_longterm"] == 1, ctx
    # Фрагменты суммируются отдельно: рабочая 2+3=5, долговременная 1.
    assert ctx["memory_used_working"] == 5, ctx
    assert ctx["memory_used_longterm"] == 1, ctx
    print("[OK] счётчик «раз использования» памяти учитывается за обмены")


def test_usage_counts_persist_and_reset():
    st, tmpdir = _fresh_store()
    path = os.path.join(tmpdir, "session.json")
    st.add_memory_usage(working=1, longterm=0)
    st2 = SessionStore(path=path)
    assert st2.context_stats()["memory_use_count_working"] == 1
    st2.reset()
    assert st2.context_stats()["memory_use_count_working"] == 0
    print("[OK] счётчики использования сохраняются и сбрасываются")


if __name__ == "__main__":
    test_three_types_are_separate()
    test_short_is_not_manually_writable()
    test_explicit_routing()
    test_persistence_roundtrip()
    test_reset_keeps_longterm()
    test_memory_in_prompt()
    test_facts_merge_not_wipe()
    test_memory_usage_counts()
    test_usage_counts_persist_and_reset()
    print("\nВСЕ ПРОВЕРКИ ПАМЯТИ ПРОЙДЕНЫ.")
