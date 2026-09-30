"""RAG (Retrieval-Augmented Generation) поверх локальных PDF-документов.

Назначение
----------
Дать агенту доступ к БАЗЕ ЗНАНИЙ из PDF-файлов: документы индексируются
(текстовый слой -> чанки -> эмбеддинги), а при запросе в чат выполняется
ПОИСК по индексу и найденный контекст ПОДМЕШИВАЕТСЯ в промпт модели.

Режим работы — A2 (см. переписку/план): при ВКЛЮЧЁННОМ RAG поиск выполняется
ВСЕГДА, найденный контекст добавляется в системный промпт. Модель не
«решает», искать ли — она просто получает готовый контекст. Это надёжнее
нативного tool-calling (у пользователя только модель эмбеддингов Ollama,
без chat-моделей с поддержкой tools).

Компоненты
----------
  * эмбеддинги  — ЛОКАЛЬНАЯ модель Ollama nomic-embed-text-v2-moe
                   (POST /api/embeddings, dim=768, контекст 512 токенов);
  * извлечение  — pypdf, ТОЛЬКО текстовый слой (графику игнорируем, OCR не
                   нужен: PDF сконвертированы из Word и имеют текстовый слой);
  * хранилище   — SQLite (session/rag_index.db): таблица chunks с текстом
                   и вектором (BLOB float32). JSON — только для настроек
                   и метаданных (session/rag.json).

Данный модуль НЕ зависит от session_store (низкоуровневый), но использует
конфигурацию из rtk_app.config. Потокобезопасность: каждая публичная
функция сама открывает короткую сессию SQLite; запись индекса защищена
блокировкой модуля (индексация выполняется редко и последовательно).
"""
import json
import math
import os
import struct
import threading
import time
import urllib.request

from . import config

__all__ = [
    "RagStore",
    "embed_text",
    "embed_available",
]

# Блокировка на время (пере)индексации: две параллельные индексации одного
# и того же файла приводили бы к гонке за индекс. Чтение (поиск) не блокируем.
_index_lock = threading.Lock()

# Библиотека pypdf загружается лениво: без установленной библиотеки остальной
# функционал RAG (настройки, статус) должен работать, а индексация — выдать
# понятную ошибку.
_PDF_READER = None
_PDF_IMPORT_ERROR = None


def _load_pdf_reader():
    """Ленивая загрузка PdfReader из pypdf. None, если библиотеки нет."""
    global _PDF_READER, _PDF_IMPORT_ERROR
    if _PDF_READER is not None or _PDF_IMPORT_ERROR is not None:
        return _PDF_READER
    try:
        from pypdf import PdfReader  # noqa: WPS433 (импорт по требованию)
        _PDF_READER = PdfReader
    except Exception as exc:                     # noqa: BLE001
        _PDF_IMPORT_ERROR = str(exc)
        _PDF_READER = None
    return _PDF_READER


# ----------------------------------------------------------------------
# Эмбеддинги (Ollama)
# ----------------------------------------------------------------------
def embed_text(text, model=None, timeout=None):
    """Считает вектор эмбеддинга для текста через локальную Ollama.

    Возвращает список float (длина RAG_EMBED_DIM) либо бросает исключение
    при недоступности Ollama/модели. Запрос — /api/embeddings (один prompt).
    """
    text = str(text or "").strip()
    if not text:
        raise ValueError("пустой текст для эмбеддинга")
    payload = {
        "model": model or config.RAG_EMBED_MODEL,
        "prompt": text,
    }
    req = urllib.request.Request(
        config.RAG_EMBED_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(
            req, timeout=timeout or config.RAG_EMBED_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    vec = data.get("embedding")
    if not isinstance(vec, list) or not vec:
        raise RuntimeError("Ollama не вернула эмбеддинг")
    return [float(x) for x in vec]


def embed_available(timeout=5):
    """Проверяет доступность модели эмбеддингов (без индексации).

    Возвращает dict {ok, model, dim?, error?}. Используется кнопкой
    «Проверить» и статусом блока RAG.
    """
    try:
        vec = embed_text("проверка", timeout=timeout)
        return {"ok": True, "model": config.RAG_EMBED_MODEL, "dim": len(vec)}
    except Exception as exc:                     # noqa: BLE001
        return {"ok": False, "model": config.RAG_EMBED_MODEL,
                "error": str(exc)}


# ----------------------------------------------------------------------
# Векторы: упаковка/распаковка и косинусная близость
# ----------------------------------------------------------------------
def _pack_vector(vec):
    """Упаковывает список float в BLOB (float32, little-endian)."""
    return struct.pack("<%df" % len(vec), *vec)


def _unpack_vector(blob, dim):
    """Распаковывает BLOB в список float заданной размерности."""
    if not blob:
        return []
    count = len(blob) // 4
    if dim and count != dim:
        count = min(count, dim)
    return list(struct.unpack("<%df" % count, blob[:count * 4]))


def _cosine(a, b):
    """Косинусная близость двух векторов (списки float)."""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = 0.0
    na = 0.0
    nb = 0.0
    for i in range(n):
        va = a[i]
        vb = b[i]
        dot += va * vb
        na += va * va
        nb += vb * vb
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ----------------------------------------------------------------------
# Извлечение текста из PDF (только текстовый слой)
# ----------------------------------------------------------------------
def extract_pdf_text(path, max_mb=None):
    """Извлекает ТЕКСТОВЫЙ СЛОЙ PDF. Картинки/графику игнорирует.

    Возвращает (pages, error):
      pages — список строк (по странице), может содержать пустые;
      error — строка с причиной, если файл пропущен, иначе None.
    PDF без текстового слоя (все страницы пустые) → error «PDF без
    текстового слоя», OCR НЕ применяется (по требованию задачи).
    """
    PdfReader = _load_pdf_reader()
    if PdfReader is None:
        return [], "нет библиотеки pypdf (%s)" % (_PDF_IMPORT_ERROR or "?")
    limit_mb = max_mb or config.RAG_MAX_PDF_MB
    try:
        size_mb = os.path.getsize(path) / (1024.0 * 1024.0)
    except OSError as exc:
        return [], "не удалось прочитать файл: %s" % exc
    if size_mb > limit_mb:
        return [], "файл больше %d МБ (%.1f МБ) — пропущен" % (limit_mb, size_mb)
    try:
        reader = PdfReader(path)
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:                    # noqa: BLE001
                pages.append("")
    except Exception as exc:                     # noqa: BLE001
        return [], "ошибка чтения PDF: %s" % exc
    if not any(p.strip() for p in pages):
        return [], "PDF без текстового слоя"
    return pages, None


# ----------------------------------------------------------------------
# Нарезка текста на чанки
# ----------------------------------------------------------------------
def chunk_text(text, size=None, overlap=None):
    """Режет текст на чанки с перекрытием (для индексации).

    Размер чанка заведомо меньше контекста модели эмбеддингов (512 токенов):
    RAG_CHUNK_CHARS символов укладываются в окно с запасом для русского.
    Перекрытие сохраняет смысл на границах чанков.
    """
    text = str(text or "")
    size = int(size or config.RAG_CHUNK_CHARS)
    overlap = int(overlap if overlap is not None else config.RAG_CHUNK_OVERLAP)
    if size <= 0:
        size = config.RAG_CHUNK_CHARS
    if overlap < 0:
        overlap = 0
    if overlap >= size:
        overlap = max(0, size // 4)
    chunks = []
    n = len(text)
    start = 0
    while start < n:
        end = start + size
        piece = text[start:end]
        if piece.strip():
            chunks.append(piece)
        if end >= n:
            break
        start = end - overlap
    return chunks


# ----------------------------------------------------------------------
# Хранилище индекса (SQLite)
# ----------------------------------------------------------------------
class RagStore(object):
    """Индекс чанков и векторов в SQLite + настройки RAG в JSON.

    Схема БД:
      chunks(id INTEGER PK, file TEXT, page INTEGER, chunk INTEGER,
             text TEXT, vec BLOB)
      files(path TEXT PK, mtime REAL, size INTEGER, chunks INTEGER,
            indexed REAL, error TEXT)
    Векторы — BLOB float32 (dim = config.RAG_EMBED_DIM). Поиск — косинусный
    перебор (десятки тысяч чанков: приемлемо, без внешних ANN-библиотек).
    """

    def __init__(self, index_file=None, settings_file=None):
        self.index_file = index_file or config.RAG_INDEX_FILE
        self.settings_file = settings_file or config.RAG_SETTINGS_FILE
        self._local = threading.local()
        self._ensure_dir()

    # ---- низкоуровневый доступ к SQLite ----
    def _ensure_dir(self):
        for p in (self.index_file, self.settings_file):
            d = os.path.dirname(p)
            if d and not os.path.isdir(d):
                try:
                    os.makedirs(d, exist_ok=True)
                except OSError:
                    pass

    def _conn(self):
        """Соединение SQLite для текущего потока (ленивое)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            import sqlite3
            conn = sqlite3.connect(self.index_file, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
            self._init_schema(conn)
            self._local.conn = conn
        return conn

    @staticmethod
    def _init_schema(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "file TEXT NOT NULL,"
            "page INTEGER NOT NULL DEFAULT 0,"
            "chunk INTEGER NOT NULL DEFAULT 0,"
            "text TEXT NOT NULL,"
            "vec BLOB)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS files ("
            "path TEXT PRIMARY KEY,"
            "mtime REAL,"
            "size INTEGER,"
            "chunks INTEGER,"
            "indexed REAL,"
            "error TEXT)"
        )
        conn.commit()

    # ---- настройки (JSON) ----
    def load_settings(self):
        """Читает настройки RAG из JSON. Гарантирует все ключи."""
        defaults = {
            "enabled": bool(config.RAG_ENABLED),
            "docs_dir": config.RAG_DOCS_DIR,
            "top_k": int(config.RAG_TOP_K),
            "embed_model": config.RAG_EMBED_MODEL,
            "chunk_chars": int(config.RAG_CHUNK_CHARS),
            "updated": None,
        }
        data = {}
        try:
            with open(self.settings_file, "r", encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except Exception:                        # noqa: BLE001
            data = {}
        if not isinstance(data, dict):
            data = {}
        out = dict(defaults)
        if "enabled" in data:
            out["enabled"] = bool(data.get("enabled"))
        if isinstance(data.get("docs_dir"), str) and data["docs_dir"].strip():
            out["docs_dir"] = data["docs_dir"].strip()
        try:
            k = int(data.get("top_k"))
            out["top_k"] = max(1, min(20, k))
        except (TypeError, ValueError):
            pass
        if data.get("updated"):
            out["updated"] = data["updated"]
        return out

    def save_settings(self, settings):
        """Сохраняет настройки RAG в JSON (atomic-запись)."""
        try:
            with open(self.settings_file, "w", encoding="utf-8") as fh:
                json.dump(settings, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def update_settings(self, enabled=None, docs_dir=None, top_k=None):
        """Точечно меняет настройки (None — не трогать). Возвращает снимок."""
        s = self.load_settings()
        if enabled is not None:
            s["enabled"] = bool(enabled)
        if docs_dir is not None and str(docs_dir).strip():
            s["docs_dir"] = str(docs_dir).strip()
        if top_k is not None:
            try:
                s["top_k"] = max(1, min(20, int(top_k)))
            except (TypeError, ValueError):
                pass
        s["embed_model"] = config.RAG_EMBED_MODEL
        s["chunk_chars"] = int(config.RAG_CHUNK_CHARS)
        self.save_settings(s)
        return s

    # ---- (пере)индексация ----
    def _pdf_files(self, docs_dir):
        """Список PDF-файлов в папке (рекурсивно), отсортированный."""
        out = []
        if not docs_dir or not os.path.isdir(docs_dir):
            return out
        for root, _dirs, names in os.walk(docs_dir):
            for name in names:
                if name.lower().endswith(".pdf"):
                    out.append(os.path.join(root, name))
        out.sort()
        return out

    def index_docs(self, docs_dir=None, verbose=True):
        """Полная (пере)индексация папки с PDF.

        Перебирает PDF, для неизменённых (mtime+size) — пропускает, для
        остальных: извлекает текст -> режет на чанки -> считает эмбеддинги ->
        пишет в SQLite. Возвращает отчёт {ok, files, chunks, skipped, errors}.
        """
        if not _index_lock.acquire(blocking=False):
            return {"ok": False, "error": "индексация уже выполняется"}
        try:
            settings = self.load_settings()
            docs_dir = docs_dir or settings.get("docs_dir") or config.RAG_DOCS_DIR
            if not os.path.isdir(docs_dir):
                return {"ok": False,
                        "error": "папка не найдена: %s" % docs_dir}
            conn = self._conn()
            files = self._pdf_files(docs_dir)
            report = {"ok": True, "docs_dir": docs_dir, "files": 0,
                      "chunks": 0, "skipped": 0, "unchanged": 0,
                      "errors": [], "embed_model": config.RAG_EMBED_MODEL}
            if not files:
                return report
            for path in files:
                try:
                    mtime = os.path.getmtime(path)
                    size = os.path.getsize(path)
                except OSError:
                    report["skipped"] += 1
                    continue
                row = conn.execute(
                    "SELECT mtime, size, chunks FROM files WHERE path=?",
                    (path,)).fetchone()
                if row and abs(row[0] - mtime) < 1e-6 and row[1] == size \
                        and not row[2] is None:
                    report["unchanged"] += 1
                    report["chunks"] += int(row[2] or 0)
                    report["files"] += 1
                    continue
                pages, err = extract_pdf_text(path)
                if err:
                    # Помечаем файл с ошибкой, старые чанки удаляем.
                    report["errors"].append("%s: %s"
                                            % (os.path.basename(path), err))
                    self._replace_file(conn, path, mtime, size, [], 0, err)
                    report["skipped"] += 1
                    continue
                chunks = self._file_chunks(pages)
                if not chunks:
                    report["errors"].append(
                        "%s: нет текста" % os.path.basename(path))
                    self._replace_file(conn, path, mtime, size, [], 0,
                                       "нет текста")
                    report["skipped"] += 1
                    continue
                vectors = []
                embed_failed = None
                for ch in chunks:
                    try:
                        vectors.append(embed_text(ch["text"]))
                    except Exception as exc:     # noqa: BLE001
                        embed_failed = str(exc)
                        break
                if embed_failed:
                    report["ok"] = False
                    report["errors"].append(
                        "%s: эмбеддинги недоступны (%s)"
                        % (os.path.basename(path), embed_failed))
                    break
                self._replace_file(conn, path, mtime, size, chunks, vectors,
                                   None)
                report["files"] += 1
                report["chunks"] += len(chunks)
                if verbose:
                    print("[RAG] проиндексирован %s: %d чанков"
                          % (os.path.basename(path), len(chunks)), flush=True)
            settings["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self.save_settings(settings)
            return report
        finally:
            _index_lock.release()

    @staticmethod
    def _file_chunks(pages):
        """Превращает страницы PDF в список чанков {page, chunk, text}."""
        out = []
        for pi, page_text in enumerate(pages):
            for ci, piece in enumerate(chunk_text(page_text)):
                out.append({"page": pi + 1, "chunk": ci + 1, "text": piece})
        return out

    @staticmethod
    def _replace_file(conn, path, mtime, size, chunks, vectors, error):
        """Заменяет все чанки файла и его запись в files (в транзакции)."""
        conn.execute("DELETE FROM chunks WHERE file=?", (path,))
        if chunks and vectors and len(chunks) == len(vectors):
            for ch, vec in zip(chunks, vectors):
                conn.execute(
                    "INSERT INTO chunks(file, page, chunk, text, vec) "
                    "VALUES (?,?,?,?,?)",
                    (path, ch["page"], ch["chunk"], ch["text"],
                     _pack_vector(vec)))
        conn.execute(
            "INSERT OR REPLACE INTO files(path, mtime, size, chunks, indexed, "
            "error) VALUES (?,?,?,?,?,?)",
            (path, mtime, size, len(chunks) if not error else 0,
             time.time(), error))
        conn.commit()

    # ---- очистка ----
    def clear(self):
        """Удаляет все чанки, файлы и метаданные индекса."""
        conn = self._conn()
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM files")
        conn.commit()
        s = self.load_settings()
        s["updated"] = None
        self.save_settings(s)
        return {"ok": True}

    # ---- поиск ----
    def search(self, query, top_k=None, docs_dir=None):
        """Ищет top-k ближайших чанков к запросу (косинусная близость).

        Возвращает список {file, page, chunk, text, score}. Пустой запрос
        или пустой индекс → пустой список. Ошибки эмбеддингов пробрасываются
        в виде исключения (обрабатывает вызывающий).
        """
        query = str(query or "").strip()
        if not query:
            return []
        settings = self.load_settings()
        k = int(top_k or settings.get("top_k") or config.RAG_TOP_K)
        conn = self._conn()
        rows = conn.execute(
            "SELECT file, page, chunk, text, vec FROM chunks").fetchall()
        if not rows:
            return []
        qv = embed_text(query)
        scored = []
        for file, page, chunk, text, vec in rows:
            v = _unpack_vector(vec, config.RAG_EMBED_DIM)
            if not v:
                continue
            scored.append((_cosine(qv, v), file, page, chunk, text))
        scored.sort(key=lambda x: x[0], reverse=True)
        out = []
        for score, file, page, chunk, text in scored[:max(1, k)]:
            out.append({"file": file, "page": page, "chunk": chunk,
                        "text": text, "score": round(score, 4)})
        return out

    def build_context(self, query, top_k=None, docs_dir=None):
        """Формирует текстовый блок контекста RAG для подмешивания в промпт.

        Возвращает (context_text, hits): context_text — готовый блок с
        указанием источника (файл, страница) или "" если ничего не найдено;
        hits — список найденных фрагментов (для трассировки/UI).
        Ограничивается RAG_CTX_CAP символами.
        """
        hits = self.search(query, top_k=top_k, docs_dir=docs_dir)
        if not hits:
            return "", []
        parts = []
        used = 0
        cap = int(config.RAG_CTX_CAP)
        for h in hits:
            base = os.path.basename(h["file"])
            head = "[фрагмент: %s, стр. %s]" % (base, h["page"])
            body = h["text"].strip()
            piece = head + "\n" + body
            if used + len(piece) > cap and parts:
                break
            parts.append(piece)
            used += len(piece) + 2
        return "\n\n".join(parts), hits

    # ---- статус ----
    def status(self):
        """Снимок состояния RAG для интерфейса.

        {enabled, docs_dir, top_k, embed_model, files, chunks, updated,
         available(есть ли индекс), last_error}
        """
        settings = self.load_settings()
        conn = self._conn()
        try:
            chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        except Exception:                        # noqa: BLE001
            chunks = 0
        try:
            files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        except Exception:                        # noqa: BLE001
            files = 0
        try:
            errs = conn.execute(
                "SELECT path, error FROM files WHERE error IS NOT NULL "
                "AND error<>'' LIMIT 20").fetchall()
        except Exception:                        # noqa: BLE001
            errs = []
        return {
            "enabled": bool(settings.get("enabled")),
            "docs_dir": settings.get("docs_dir"),
            "top_k": settings.get("top_k"),
            "embed_model": config.RAG_EMBED_MODEL,
            "embed_dim": config.RAG_EMBED_DIM,
            "files": int(files),
            "chunks": int(chunks),
            "updated": settings.get("updated"),
            "available": int(chunks) > 0,
            "errors": [{"file": os.path.basename(p), "error": e}
                       for p, e in errs],
        }
