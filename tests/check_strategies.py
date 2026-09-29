"""Проверка стратегий управления контекстом (офлайн, без сети).

Проверяет требования:
  1) переключение между стратегиями (взаимоисключающие);
  2) Sliding — только последние N сообщений (N=0 — вся история);
  3) Facts  — блок facts (key-value) + последние N сообщений;
  4) Branch — ветки диалога от checkpoint, независимое ведение, переключение,
     переименование и удаление веток;
  5) summary (COMPACT_*) работает ПОВЕРХ выбранной стратегии.

Запуск:  python tests/check_strategies.py
"""
import json
import os
import tempfile
import sys

# Корень проекта — на уровень выше tests/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtk_app import config
from rtk_app.session_store import SessionStore
from tests.harness import check, finish


def make_store():
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    os.remove(tmp.name)
    return SessionStore(tmp.name), tmp.name


def add_turns(store, n):
    for i in range(n):
        store.append_turn("Q%d" % i, {"role": "assistant", "content": "A%d" % i})


def main():
    print("=== Требование 1: переключение стратегий ===")
    s, path = make_store()
    add_turns(s, 5)                          # 10 сообщений
    check("по умолчанию стратегия 'none'",
          s.get_strategy()["strategy"] == "none")
    for name in ("sliding", "facts", "branch", "none"):
        s.set_strategy(name, 3)
        check("переключение на '%s' работает" % name,
              s.get_strategy()["strategy"] == name)
    s.set_strategy("bogus", 3)
    check("некорректная стратегия игнорируется", s.get_strategy()["strategy"] == "none")

    print()
    print("=== Требование 2: Sliding Window ===")
    s2, path2 = make_store()
    add_turns(s2, 8)                         # 16 сообщений
    s2.set_strategy("sliding", 4)
    ctx = s2.get_context_messages()
    body = [m for m in ctx if m.get("role") != "system"]
    check("Sliding N=4 -> ровно 4 последних сообщения", len(body) == 4)
    full = s2.snapshot()
    check("Sliding: это именно хвост истории",
          [m["content"] for m in body] == [m["content"] for m in full[-4:]])
    s2.set_strategy("sliding", 0)
    check("Sliding N=0 -> вся история", len(s2.get_context_messages()) == 16)

    print()
    print("=== Требование 3: Facts = данные ПАМЯТИ агента ===")
    s3, path3 = make_store()
    add_turns(s3, 6)                         # 12 сообщений
    # Факты хранятся в памяти агента: по умолчанию set_facts кладёт их
    # в РАБОЧУЮ память (longterm не трогаем).
    s3.set_facts({"цель": "написать отчёт", "ограничение": "до пятницы"})
    s3.set_strategy("facts", 4)
    ctx3 = s3.get_context_messages()
    check("Facts: первый блок — system с памятью (фактами)",
          ctx3 and ctx3[0].get("role") == "system"
          and "цель" in ctx3[0].get("content", "")
          and "написать отчёт" in ctx3[0].get("content", ""))
    body3 = [m for m in ctx3 if m.get("role") != "system"]
    check("Facts: память + последние N сообщений (4)", len(body3) == 4)
    s3.set_strategy("facts", 0)
    ctx3b = s3.get_context_messages()
    body3b = [m for m in ctx3b if m.get("role") != "system"]
    check("Facts N=0 -> память + вся история", len(body3b) == 12)
    check("Facts дополняются (merge: прежние ключи сохраняются)",
          s3.set_facts({"x": "1", "y": "2"}) ==
              {"цель": "написать отчёт", "ограничение": "до пятницы",
               "x": "1", "y": "2"})
    check("Facts сохраняются как память (поле memory.working, дополнение)",
          json.load(open(path3, encoding="utf-8")).get("memory", {})
              .get("working") == {"цель": "написать отчёт",
                                  "ограничение": "до пятницы",
                                  "x": "1", "y": "2"})
    # Выбор памяти у значения факта: раскладка по слоям.
    s3.set_memory_bulk("working", {"задача": "отчёт"})
    s3.set_memory_bulk("longterm", {"профиль": "аналитик"})
    check("факты объединяют рабочую и долговременную память",
          s3.get_facts() == {"задача": "отчёт", "профиль": "аналитик"})
    check("карта памяти фактов: ключ -> слой",
          s3.facts_memory_map() == {"задача": "working", "профиль": "longterm"})

    print()
    print("=== Требование 4: Branching (ветки) ===")
    s4, path4 = make_store()
    add_turns(s4, 3)                         # 6 сообщений (checkpoint)
    state = s4.create_branch(name="ветка", count=2)
    names = [b["name"] for b in state["branches"]]
    check("созданы 2 ветки от текущего (checkpoint)",
          names == ["main", "ветка-1", "ветка-2"])
    check("ветки скопировали историю (checkpoint = 6 сообщений)",
          all(b["size"] == 6 for b in state["branches"]))
    # Ведём диалог в активной ветке (ветка-1).
    s4.append_turn("Q-branch", {"role": "assistant", "content": "A-branch"})
    active_now = s4.active_branch
    check("диалог в активной ветке растёт", len(s4.snapshot()) == 8)
    # Переключаемся на main — там по-прежнему 6 сообщений (независимо).
    s4.switch_branch(0)
    check("переключение на 'main' -> независимая история (6)",
          s4.active_branch == 0 and len(s4.snapshot()) == 6)
    s4.append_turn("Q-main", {"role": "assistant", "content": "A-main"})
    check("в 'main' после ответа 8 сообщений", len(s4.snapshot()) == 8)
    # Возвращаемся к ветке-1 — там остались её 8 сообщений.
    s4.switch_branch(active_now)
    check("возврат в ветку-1 сохраняет её историю (8)",
          len(s4.snapshot()) == 8)
    check("удаление ветки уменьшает их число",
          len(s4.delete_branch(2)["branches"]) == 2)
    # Переименование ветки.
    renamed = s4.rename_branch(0, "Основной")
    check("переименование ветки меняет имя",
          renamed["branches"][0]["name"] == "Основной")
    check("пустое имя игнорируется (имя не теряется)",
          s4.rename_branch(0, "   ")["branches"][0]["name"] == "Основной")
    check("переименование не меняет размер истории",
          s4.rename_branch(1, "Идея")["branches"][1]["size"] == 8)
    # Перезагрузка с диска сохраняет ветки.
    s4r = SessionStore(path4)
    check("ветки сохраняются на диске и подхватываются",
          len(s4r.branches_state()["branches"]) == 2)
    check("переименование сохраняется на диске",
          s4r.branches_state()["branches"][0]["name"] == "Основной")

    print()
    print("=== Требование 5: summary работает поверх стратегии ===")
    s5, path5 = make_store()
    add_turns(s5, 8)                         # 16 сообщений
    s5.set_strategy("sliding", 0)
    s5.apply_summary("SUMMARY ...", upto=10, keep=6)
    ctx5 = s5.get_context_messages()
    check("summary подставлен первым даже при Sliding",
          ctx5 and ctx5[0].get("role") == "system"
          and "SUMMARY" in ctx5[0].get("content", ""))
    check("после summary остаются последние keep сообщений",
          len([m for m in ctx5 if m.get("role") != "system"]) == 6)

    print()
    for p in (path, path2, path3, path4, path5):
        try:
            os.remove(p)
        except OSError:
            pass

    sys.exit(finish("Итог"))


if __name__ == "__main__":
    main()
