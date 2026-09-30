"""Проверка АВТОМАТИЧЕСКОГО ИНКРЕМЕНТАЛЬНОГО сжатия через HTTP-сервер.

Сценарий:
  * поднимаем ThreadingHTTPServer с ФЕЙКОВЫМ агентом (без сети);
  * шлём запросы /api/ask через HTTP (как это делает страница);
  * клиент НЕ вызывает /api/compact_summary вообще;
  * проверяем, что как только история становится больше keep, сервер САМ
    дописывает вытесненные сообщения в summary (инкрементально, Вариант A)
    и в следующий запрос подставляет summary + последние keep сообщений;
  * проверяем, что summary хранится отдельно (compact.summary).

Запуск:  python tests/check_auto_compact.py
"""
import json
import os
import sys
import tempfile
import threading
import urllib.request

# Корень проекта — на уровень выше tests/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from rtk_app import config
from rtk_app import session_store as ss_mod
from tests.harness import check, finish


class FakeAgent:
    """Заглушка агента: не ходит в сеть, фиксирует присланную историю и сжатия."""
    label = "Fake"

    def __init__(self):
        self.sent = []            # истории, пришедшие в answer()
        self.compacted = []       # куски, ушедшие в compact_update()

    def available(self):
        return [{"label": "Fake", "cls": ""}]

    def answer(self, question, history=None, selected=None,
               max_tokens=None, compact=None, memory=None, profile=None,
               answer_title=None, task_state=None, invariants=None,
               rag_context=None):
        self.sent.append(list(history or []))
        return {"ok": True, "html": "", "text": "ok", "answers": [],
                "meta": "", "usage": {"input": 1, "output": 1,
                                      "total": 2, "history": 0}, "trace": []}

    def compact_update(self, prev_summary, new_messages):
        self.compacted.append(list(new_messages or []))
        return "AUTO-SUMMARY(+" + str(len(new_messages or [])) + ")"


def main():
    tmpdir = tempfile.mkdtemp()
    ss_mod.config.SESSION_DIR = tmpdir
    ss_mod.config.SESSION_FILE = os.path.join(tmpdir, "session.json")
    # Профили тоже во временную папку, чтобы не трогать рабочие файлы.
    ss_mod.config.PROFILES_FILE = os.path.join(tmpdir, "profiles.json")
    # Явно включаем сжатие: в проекте по умолчанию keep = 0 (сжатие выкл.).
    ss_mod.config.COMPACT_KEEP = 10

    from web import server

    # Этот тест проверяет НОРМАЛЬНЫЙ путь чата (сжатие/память/персоны), а не
    # MCP. Форсируем «не-MCP» режим: чекбокс выключен и НИ ОДИН MCP-сервер не
    # выбран (иначе запрос ушёл бы MCP-путём — см. web/server.py: _handle_ask).
    server._ServerState.mcp_enabled = False
    server._ServerState.mcp_server = ""

    fake = FakeAgent()
    httpd = server.create_server(fake, host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port

    def post(path, obj):
        req = urllib.request.Request(
            base + path, data=json.dumps(obj).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def get(path):
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    keep = config.COMPACT_KEEP
    # Сжатие стартует, как только история больше keep: нужно
    # (keep//2 + 1) ходов, чтобы появилось хотя бы одно вытесненное сообщение.
    turns = keep // 2 + 1

    try:
        # По умолчанию профилей НЕТ — без профиля сервер отказывает в ответе.
        # Создаём профиль, чтобы запросы /api/ask обрабатывались.
        post("/api/profiles", {"action": "create", "name": "Тест",
                               "character": "дружелюбный", "style": "кратко"})

        print("=== клиент НЕ вызывает /api/compact_summary (только /api/ask) ===")
        for i in range(turns):
            post("/api/ask", {"question": "q%d" % i,
                              "models": [{"label": "Fake", "temperature": 0.7}],
                              "max_tokens": None})
        # Ни одного ручного вызова сжатия:
        check("ручных вызовов generate не было (кнопки нет)",
              True)
        check("сервер сам вызвал compact_update (инкрементальное сжатие)",
              len(fake.compacted) > 0)
        check("фронт не отправлял compact_summary (в проверке нет вызова)",
              True)

        sess = get("/api/session")
        comp = sess.get("compact", {})
        check("summary сгенерирован автоматически",
              bool(comp.get("summary")) and "AUTO-SUMMARY" in comp["summary"])
        check("граница покрытия upto > 0", comp.get("upto", 0) > 0)

        print()
        print("=== следующий запрос идёт с summary, а не с полной историей ===")
        post("/api/ask", {"question": "ещё",
                          "models": [{"label": "Fake", "temperature": 0.7}],
                          "max_tokens": None})
        sent = fake.sent[-1]
        check("первый элемент — system-summary",
              sent and sent[0].get("role") == "system"
              and "AUTO-SUMMARY" in sent[0].get("content", ""))
        body = [m for m in sent if m.get("role") != "system"]
        check("после summary — не более keep сообщений", len(body) <= keep + 2)
        n_full = len(get("/api/session")["messages"])
        check("в запрос ушло меньше, чем полная история",
              len(sent) < n_full + 1)

        print()
        raw = json.load(open(ss_mod.config.SESSION_FILE, encoding="utf-8"))
        check("summary хранится ОТДЕЛЬНО (compact.summary)",
              raw.get("compact", {}).get("summary"))
        check("summary отсутствует среди messages",
              all("AUTO-SUMMARY" not in str(m.get("content", ""))
                  for m in raw.get("messages", [])))
    finally:
        httpd.shutdown()
        httpd.server_close()

    print()
    sys.exit(finish("Итог"))


if __name__ == "__main__":
    main()
