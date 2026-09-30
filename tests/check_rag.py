# -*- coding: utf-8 -*-
"""Автономная проверка RAG-слоя (rtk_app/rag.py + session_store).

Проверяет БЕЗ внешних зависимостей (офлайн):
  1. Нарезку текста на чанки (размер, перекрытие, пустой текст);
  2. Упаковку/распаковку векторов и косинусную близость;
  3. Извлечение текстового слоя PDF (pypdf): только текст, лимит размера,
     PDF без текстового слоя, отсутствие библиотеки;
  4. Настройки RAG в session_store (set/get/сохранение в JSON);
  5. Статус RAG (пустой индекс, очистка, отсутствие падений);
  6. Формирование блока RAG в системном промпте агента;
  7. Отсутствие падения при недоступной Ollama (graceful degradation).

Эмбеддинги Ollama в этих тестах НЕ вызываются (кроме проверки деградации,
где ошибка ожидаема). Запуск: python -m tests.check_rag
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.harness import check, section, finish, ensure_utf8  # noqa: E402
from rtk_app import rag  # noqa: E402
from rtk_app.agent import Agent  # noqa: E402
from rtk_app.session_store import SessionStore  # noqa: E402


def _tmp_paths():
    """Уникальные временные пути для индекса и настроек."""
    d = tempfile.mkdtemp(prefix="rag_test_")
    return (os.path.join(d, "idx.db"), os.path.join(d, "set.json"), d)


def test_chunking():
    section("1. Нарезка текста на чанки")
    short = "короткий текст"
    c = rag.chunk_text(short, size=600, overlap=100)
    check("короткий текст — один чанк", len(c) == 1)

    long_text = "".join("abcdefghij" for _ in range(200))  # 2000 символов
    c = rag.chunk_text(long_text, size=600, overlap=100)
    check("длинный текст — несколько чанков", len(c) >= 3)
    check("размер чанка <= заданного", all(len(x) <= 600 for x in c))
    # Перекрытие: следующий чанк начинается раньше конца предыдущего.
    merged_ok = (c[0][-100:] == c[1][:100])
    check("соседние чанки перекрываются (100 символов)", merged_ok)

    check("пустой текст — пустой список", rag.chunk_text("") == [])
    check("пробельный текст — пустой список", rag.chunk_text("   \n  ") == [])
    # Защита: overlap >= size не должен зацикливаться.
    c = rag.chunk_text(long_text, size=50, overlap=100)
    check("overlap >= size не зацикливается", len(c) > 0 and len(c) < 1000)


def test_vectors():
    section("2. Векторы: упаковка и косинусная близость")
    v = [0.1, -0.2, 0.3, 0.4]
    blob = rag._pack_vector(v)
    back = rag._unpack_vector(blob, 4)
    check("упаковка/распаковка сохраняет длину", len(back) == 4)
    check("значения близки к исходным",
          all(abs(a - b) < 1e-5 for a, b in zip(v, back)))

    check("косинус одинаковых векторов ~1", abs(rag._cosine(v, v) - 1.0) < 1e-6)
    check("косинус пустых векторов = 0", rag._cosine([], []) == 0.0)
    check("косинус нулевого вектора = 0", rag._cosine([0, 0], [1, 2]) == 0.0)
    neg = [-x for x in v]
    check("косинус противоположных ~-1", abs(rag._cosine(v, neg) + 1.0) < 1e-6)


def test_pdf_extract():
    section("3. Извлечение текста из PDF")
    # Отсутствие библиотеки pypdf маскируем: тест должен пройти и без неё
    # (тогда extract вернёт понятную ошибку).
    PdfReader = rag._load_pdf_reader()
    if PdfReader is None:
        check("pypdf недоступен — возвращается ошибка",
              rag.extract_pdf_text("nope.pdf")[1] is not None)
        check("нет падения без pypdf", True)
        return

    # Невалидный PDF — должна быть ошибка, без исключения.
    _idx, _set, d = _tmp_paths()
    bad = os.path.join(d, "bad.pdf")
    with open(bad, "wb") as fh:
        fh.write(b"%PDF-1.4 not really a pdf")
    pages, err = rag.extract_pdf_text(bad)
    check("невалидный PDF — ошибка, без падения", err is not None and pages == [])
    check("несуществующий файл — ошибка", rag.extract_pdf_text("no.pdf")[1] is not None)

    # Лимит размера: файл больше лимита пропускается.
    big = os.path.join(d, "big.pdf")
    with open(big, "wb") as fh:
        fh.write(b"x" * (2 * 1024 * 1024))
    pages, err = rag.extract_pdf_text(big, max_mb=1)
    check("PDF больше лимита — пропущен", "больше" in (err or ""))


def test_store_status_and_settings():
    section("4. RagStore: настройки и статус (без Ollama)")
    idx, setf, _d = _tmp_paths()
    store = rag.RagStore(index_file=idx, settings_file=setf)

    st = store.status()
    check("статус пустого индекса: 0 файлов", st["files"] == 0)
    check("статус пустого индекса: 0 чанков", st["chunks"] == 0)
    check("статус: available=False при пустом индексе", st["available"] is False)
    check("модель эмбеддингов указана", st["embed_model"])

    s = store.update_settings(enabled=True, docs_dir="/tmp/docs", top_k=7)
    check("настройки: enabled применён", s["enabled"] is True)
    check("настройки: docs_dir применён", s["docs_dir"] == "/tmp/docs")
    check("настройки: top_k применён", s["top_k"] == 7)

    # top_k клампится в диапазон 1..20.
    store.update_settings(top_k=999)
    check("top_k клампится сверху (<=20)", store.load_settings()["top_k"] <= 20)
    store.update_settings(top_k=0)
    check("top_k клампится снизу (>=1)", store.load_settings()["top_k"] >= 1)

    # Настройки переживают пересоздание стора (запись в JSON).
    store2 = rag.RagStore(index_file=idx, settings_file=setf)
    check("настройки сохраняются в JSON", store2.load_settings()["top_k"] >= 1)

    # Поиск на пустом индексе — пусто, без падения.
    check("поиск на пустом индексе — пусто", store2.search("вопрос") == [])
    ctx, hits = store2.build_context("вопрос")
    check("build_context на пустом индексе — пустой контекст",
          ctx == "" and hits == [])

    # Очистка не падает.
    check("очистка индекса возвращает ok", store2.clear().get("ok") is True)


def test_session_store_rag():
    section("5. SessionStore: интеграция RAG и graceful degradation")
    d = tempfile.mkdtemp(prefix="rag_sess_")
    sess_file = os.path.join(d, "session.json")
    s = SessionStore(path=sess_file)

    check("full_state содержит блок rag", "rag" in s.full_state())
    st = s.rag_status()
    check("rag_status: по умолчанию выключен", st["enabled"] is False)

    s.set_rag_settings(enabled=True, docs_dir=os.path.join(d, "docs"), top_k=3)
    check("rag_status: включение сохранено", s.rag_status()["enabled"] is True)
    check("rag_settings: top_k сохранён", s.rag_settings()["top_k"] == 3)

    # Пустой индекс: контекст НЕ строится и НЕ падает (даже при enabled=True).
    ctx, hits = s.rag_context("любой вопрос")
    check("rag_context при пустом индексе — пусто", ctx == "" and hits == [])

    # Очистка не падает и возвращает снимок.
    st = s.rag_clear()
    check("rag_clear возвращает снимок статуса", "chunks" in st)


def test_agent_prompt():
    section("6. Агент: блок RAG в системном промпте")
    a = Agent(api_key="dummy")

    msgs = a._build_messages([{"role": "user", "content": "привет"}],
                             "вопрос",
                             rag_context="[фрагмент: doc.pdf, стр. 2]\nТекст.")
    sys_text = msgs[0]["content"]
    check("RAG-блок добавлен в системный промпт",
          "КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ" in sys_text)
    check("в промпте есть инструкция по RAG",
          "ИНСТРУКЦИЯ ПО RAG" in sys_text)
    check("в промпте упомянут источник (doc.pdf)", "doc.pdf" in sys_text)

    # Без контекста блок НЕ добавляется.
    msgs2 = a._build_messages([], "вопрос")
    check("без RAG-контекста блок отсутствует",
          "КОНТЕКСТ ИЗ БАЗЫ ЗНАНИЙ" not in msgs2[0]["content"])

    check("rag_system_prompt(None) пуст", a.rag_system_prompt(None) == "")
    check("rag_system_prompt('  ') пуст", a.rag_system_prompt("   ") == "")
    check("rag_system_prompt(текст) непуст",
          "ИНСТРУКЦИЯ ПО RAG" in a.rag_system_prompt("контекст"))


def test_embed_degradation():
    section("7. Деградация при недоступной Ollama")
    # Указываем заведомо недоступный адрес — запрос должен упасть, но
    # embed_available() вернёт {ok: False} без исключения.
    old_url = rag.config.RAG_EMBED_URL
    rag.config.RAG_EMBED_URL = "http://127.0.0.1:59999/api/embeddings"
    try:
        res = rag.embed_available(timeout=2)
        check("embed_available: ok=False при недоступной Ollama",
              res.get("ok") is False)
        check("embed_available: есть описание ошибки", bool(res.get("error")))
    finally:
        rag.config.RAG_EMBED_URL = old_url


def main():
    ensure_utf8()
    test_chunking()
    test_vectors()
    test_pdf_extract()
    test_store_status_and_settings()
    test_session_store_rag()
    test_agent_prompt()
    test_embed_degradation()
    return finish("Проверка RAG")


if __name__ == "__main__":
    sys.exit(main())
