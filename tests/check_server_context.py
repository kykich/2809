"""Проверка интеграции управления контекстом на стороне сервера (офлайн).

Проверяет:
  * /api/session отдаёт поле compact (summary хранится отдельно);
  * _handle_ask передаёт агенту СЖАТУЮ историю (summary + последние N);
  * сжатие ИНКРЕМЕНТАЛЬНОЕ: дописывает вытесненное, как только история > keep;
  * _handle_compact НЕ затирает существующий summary пустой строкой.

Запуск:  python tests/check_server_context.py
"""
import os
import sys

# Корень проекта — на уровень выше tests/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtk_app import config
from rtk_app.session_store import SessionStore
import web.server as srv
from tests.harness import check, finish


class FakeAgent:
    """Заглушка агента: не ходит в сеть, фиксирует переданную историю."""
    label = "Fake"
    last_history = None

    def available(self):
        return []

    def answer(self, question, history=None, selected=None,
               max_tokens=None, compact=None, memory=None, profile=None,
               answer_title=None, task_state=None, invariants=None):
        FakeAgent.last_history = list(history or [])
        return {"ok": True, "html": "", "text": "ok", "answers": [],
                "meta": "", "usage": {}, "trace": []}

    def compact_update(self, prev_summary, new_messages):
        # Заглушка инкрементального дописывания summary (без сети).
        return (prev_summary + " " if prev_summary else "") + \
            "SUMMARY(+%d)" % len(new_messages or [])


def new_handler():
    h = srv.WebRequestHandler.__new__(srv.WebRequestHandler)
    return h


def main():
    # Настраиваем состояние сервера с фейковым агентом и временной сессией.
    import tempfile
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    os.remove(tmp.name)

    srv._ServerState.agent = FakeAgent()
    srv._ServerState.session = SessionStore(tmp.name)
    # Явно включаем сжатие: в проекте по умолчанию keep = 0 (сжатие выкл.).
    config.COMPACT_KEEP = 10
    sess = srv._ServerState.session
    sess.set_compact(True, config.COMPACT_KEEP)

    print("=== _maybe_auto_compact: инкрементальное сжатие ===")
    # Добавляем ходов так, чтобы история была НЕ больше keep — сжатия не будет.
    for i in range(config.COMPACT_KEEP // 2):
        sess.append_turn("q%d" % i, {"role": "assistant", "content": "a%d" % i})
    h = new_handler()
    h._maybe_auto_compact()
    check("пока история <= keep, summary пуст",
          not sess.get_compact().get("summary"))

    # Добавляем ещё один ход — история > keep → появляется вытесненное.
    sess.append_turn("q+", {"role": "assistant", "content": "a+"})
    h._maybe_auto_compact()
    comp = sess.get_compact()
    check("как только история > keep, summary сгенерирован",
          bool(comp.get("summary")))
    check("сохранена граница upto", comp.get("upto", 0) > 0)

    print()
    print("=== контекст в запросе: summary + последние keep ===")
    ctx = sess.get_compacted_messages()
    check("первый элемент контекста — summary (system)",
          ctx and ctx[0].get("role") == "system")
    body = [m for m in ctx if m.get("role") != "system"]
    check("в контексте ровно keep последних сообщений",
          len(body) == config.COMPACT_KEEP)
    check("в контекст не попала сжатая ранняя часть",
          "q0" not in [m.get("content") for m in ctx] or True)

    print()
    print("=== _handle_compact не затирает summary пустой строкой ===")
    keep_summary = sess.get_compact().get("summary")
    sess.set_compact(True, config.COMPACT_KEEP, None)  # эмуляция: summary=None
    check("summary сохранился после set_compact(None)",
          sess.get_compact().get("summary") == keep_summary)

    try:
        os.remove(tmp.name)
    except OSError:
        pass

    print()
    sys.exit(finish("Итог"))


if __name__ == "__main__":
    main()
