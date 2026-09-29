"""Проверка механизма управления контекстом (офлайн, без сети).

Проверяет требования:
  1) последние N сообщений хранятся/передаются «как есть»;
  2) сжатие ИНКРЕМЕНТАЛЬНОЕ: начинается с N+1-го сообщения и дописывает
     вытесненные сообщения в summary по мере вытеснения (Вариант A);
  3) summary хранится ОТДЕЛЬНО и подставляется в запрос вместо истории.

Запуск:  python tests/check_context.py
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
    os.remove(tmp.name)          # пусть SessionStore сам создаст файл
    return SessionStore(tmp.name), tmp.name


def add_turns(store, n, text="вопрос"):
    """Добавляет n ходов (каждый ход = user + assistant)."""
    for i in range(n):
        store.append_turn("%s %d" % (text, i), {
            "role": "assistant",
            "content": "ответ %d" % i,
        })


def main():
    print("=== Требование 1: последние N сообщений хранятся «как есть» ===")
    store, path = make_store()
    add_turns(store, 8)                      # 16 сообщений
    # keep задаём явно (по умолчанию в проекте keep = 0 — сжатие выключено).
    keep = 10
    store.set_compact(True, keep)
    store.apply_summary("SUMMARY: ранняя часть диалога.", upto=6, keep=keep)
    ctx = store.get_compacted_messages()
    # Должно быть: 1 system-summary + последние keep сообщений
    body = [m for m in ctx if m.get("role") != "system"]
    check("контекст = summary + последние %d сообщений" % keep,
          len(body) == keep)
    full = store.snapshot()
    check("последние N совпадают с хвостом полной истории",
          [m["content"] for m in body] ==
          [m["content"] for m in full[-keep:]])
    check("summary подставлен первым (role=system)",
          ctx and ctx[0].get("role") == "system"
          and "SUMMARY" in ctx[0].get("content", ""))

    print()
    print("=== Требование 2: инкрементальное сжатие начинается с N+1 ===")
    keep2 = 10
    # Ровно keep сообщений — сжимать нечего.
    store_eq, path_eq = make_store()
    store_eq.set_compact(True, keep2)
    add_turns(store_eq, keep2 // 2)          # ровно keep2 сообщений
    n_eq = len(store_eq.snapshot())
    head_eq, _ = store_eq.head_to_compact()
    check("при N сообщений (ровно keep) вытесненных нет",
          n_eq == keep2 and len(head_eq) == 0)
    check("при N сообщений сжатие НЕ срабатывает",
          not store_eq.should_auto_compact())

    # keep+1 сообщение — появилось ровно 1 вытесненное.
    store_plus, path_plus = make_store()
    store_plus.set_compact(True, keep2)
    add_turns(store_plus, keep2 // 2)        # ровно keep2 сообщений
    store_plus.messages.append({"role": "user", "content": "лишнее"})
    n_plus = len(store_plus.snapshot())
    head_plus, end_plus = store_plus.head_to_compact()
    check("с N+1-го сообщения появляется 1 вытесненное",
          n_plus == keep2 + 1 and len(head_plus) == 1)
    check("сжатие срабатывает сразу (>=1 вытесненного)",
          store_plus.should_auto_compact())
    check("граница покрытия end = len - keep",
          end_plus == n_plus - keep2)

    # Добавили ещё 2 сообщения — вытесненных стало 3 (инкрементально копится).
    store_plus.messages.append({"role": "user", "content": "q+2"})
    store_plus.messages.append({"role": "assistant", "content": "a+2"})
    store_plus.messages.append({"role": "user", "content": "q+3"})
    n_more = len(store_plus.snapshot())
    head_more, _ = store_plus.head_to_compact()
    check("вытесненных копится по мере вытеснения (%d)" % len(head_more),
          len(head_more) == n_more - keep2)

    print()
    print("=== Требование 3: summary хранится отдельно и подставляется ===")
    data = json.load(open(path, encoding="utf-8"))
    check("summary хранится отдельно (поле compact.summary)",
          isinstance(data.get("compact"), dict)
          and data["compact"].get("summary"))
    check("граница покрытия сохранена (compact.upto)",
          data["compact"].get("upto") == 6)
    check("полная история тоже сохранена (messages)",
          isinstance(data.get("messages"), list)
          and len(data["messages"]) == 16)
    # summary НЕ должен попадать в messages (он хранится отдельно)
    check("summary отсутствует среди messages",
          all("SUMMARY" not in str(m.get("content", ""))
              for m in data["messages"]))

    # Перезагрузка с диска сохраняет summary и сжатие
    store_reload = SessionStore(path)
    ctx2 = store_reload.get_compacted_messages()
    check("после перезагрузки с диска контекст остаётся сжатым",
          ctx2 and ctx2[0].get("role") == "system"
          and "SUMMARY" in ctx2[0].get("content", ""))

    print()
    for p in (path, path_eq, path_plus):
        try:
            os.remove(p)
        except OSError:
            pass

    sys.exit(finish("Итог"))


if __name__ == "__main__":
    main()

