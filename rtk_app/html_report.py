"""
Построение HTML-фрагмента ответа из текста модели.
Включает: экранирование HTML, inline-Markdown, парсинг Markdown-таблиц,
а также рендеринг математических формул (LaTeX) в HTML/CSS.
"""
import re

__all__ = ["escape_html", "apply_inline_markdown", "text_to_html_paragraphs",
           "render_memory_marks", "mark_memory_fragments",
           "count_memory_fragments", "render_with_memory_counts",
           "render_math", "extract_and_render_math"]

# Маркеры, которыми помечаются фрагменты, взятые из памяти агента.
#   [[R]]…[[/R]] — из РАБОЧЕЙ памяти   (подсветка фисташковым);
#   [[L]]…[[/L]] — из ДОЛГОВРЕМЕННОЙ   (подсветка фуксией).
# Сначала эти маркеры ставит детерминированный подстановщик
# (mark_memory_fragments) — по фактическому содержимому памяти, независимо
# от того, послушалась ли модель. Если модель САМА расставила такие маркеры,
# они обрабатываются тем же путём.
_MEM_OPEN_R = "[[R]]"
_MEM_CLOSE_R = "[[/R]]"
_MEM_OPEN_L = "[[L]]"
_MEM_CLOSE_L = "[[/L]]"


def _memory_terms(memory):
    """Собирает список терминов памяти как (текст, метка-маркер).

    Возвращает список пар (термин, "R"|"L") по значениям И ключам рабочей
    ("R", фисташковый) и долговременной ("L", фуксия) памяти. Значения идут
    в приоритете, длинные термины — раньше (чтобы совпадали целиком).
    Короткие/пустые термины (короче 3 символов) пропускаются, чтобы не
    подсвечивать случайные односложные совпадения.
    """
    terms = []
    w = (memory or {}).get("working") or {}
    l = (memory or {}).get("longterm") or {}
    for value, mark in [(v, "R") for v in w.values()] + \
                       [(v, "L") for v in l.values()] + \
                       [(k, "R") for k in w.keys()] + \
                       [(k, "L") for k in l.keys()]:
        s = str(value or "").strip()
        # Не подсвечиваем слишком короткие термины и «чисто числовые»
        # значения (№, суммы) — иначе подсветка «расползается» по тексту.
        if len(s) < 4:
            continue
        if re.fullmatch(r"[\d\s.,₽¥$%+-]+", s):
            continue
        terms.append((s, mark))
    # Длинные — первыми, чтобы более специфичные совпадения не перекрывались
    # короткими; убираем дубли, сохраняя первый (наиболее приоритетный) маркер.
    terms.sort(key=lambda t: len(t[0]), reverse=True)
    seen, ordered = set(), []
    for term, mark in terms:
        low = term.lower()
        if low in seen:
            continue
        seen.add(low)
        ordered.append((term, mark))
    return ordered


def mark_memory_fragments(text, memory):
    """Помечает в тексте фрагменты, совпадающие с данными памяти.

    Детерминированно (без опоры на «послушность» модели) оборачивает
    вхождения значений/ключей рабочей памяти в [[R]]…[[/R]], а
    долговременной — в [[L]]…[[/L]]. Регистр совпадения сохраняется.

    Совпадение — ТОЛЬКО по границам слов/фраз: подсвечивается вхождение,
    являющееся отдельным словом (или целой фразой), а не частью другого
    слова. Так «день» НЕ подсветится внутри «ежедневно»/«деньги», а фраза
    «3 дня» — только как цельная последовательность. Это исключает ложные
    срабатывания на частичных совпадениях.
    """
    marked, _counts = mark_memory_fragments_ex(text, memory)
    return marked


def mark_memory_fragments_ex(text, memory):
    """Как mark_memory_fragments, но ещё возвращает СЧЁТЧИКИ использований.

    Возвращает кортеж (размеченный_текст, counts), где counts —
    {"working": сколько_вхождений_рабочей, "longterm": сколько_долговрем.}.
    """
    counts = {"working": 0, "longterm": 0}
    if not text or not memory:
        return text, counts
    for term, mark in _memory_terms(memory):
        # Границы слова по Unicode (\w понимает кириллицу): слева и справа
        # не должно быть буквенно-цифрового символа. Внутренние пробелы фраз
        # допускают ЛЮБОЕ количество пробелов, чтобы «3 дня»/«3  дня» совпали.
        inner = r"\s+".join(re.escape(part) for part in term.split())
        pattern = re.compile(r"(?<!\w)" + inner + r"(?!\w)", re.IGNORECASE)
        opener, closer = ("[[R]]", "[[/R]]") if mark == "R" else ("[[L]]", "[[/L]]")

        matched_here = [0]

        def _sub(m):
            matched_here[0] += 1
            return opener + m.group(0) + closer

        text = pattern.sub(_sub, text)
        key = "working" if mark == "R" else "longterm"
        counts[key] += matched_here[0]
    return text, counts


def count_memory_fragments(text, memory):
    """Считает, сколько раз в тексте встречаются данные РАБОЧЕЙ и ДОЛГОВРЕМ.

    Возвращает dict {"working": N, "longterm": M} — число подсвеченных
    (полных, по границам слов) вхождений. Совпадения не изменяют текст.
    """
    _marked, counts = mark_memory_fragments_ex(text, memory)
    return counts


def render_memory_marks(text):
    """Превращает маркеры памяти в цветные <span> (обе пары закрываются).

    Вызывается ПОСЛЕ экранирования и inline-markdown: маркеры [[R]]/[[L]]
    экранирование не меняет (это обычный текст), поэтому их можно безопасно
    заменить на HTML-теги уже на финальном шаге.
    """
    if not text:
        return text
    # Рабочая память — фисташковый.
    text = text.replace(_MEM_OPEN_R,
                        "<span class='mem-src mem-src-working'>")
    text = text.replace(_MEM_CLOSE_R, "</span>")
    # Долговременная память — фуксия.
    text = text.replace(_MEM_OPEN_L,
                        "<span class='mem-src mem-src-longterm'>")
    text = text.replace(_MEM_CLOSE_L, "</span>")
    return text


def escape_html(text):
    """Экранирует спецсимволы для безопасного отображения в HTML."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# ======================================================================
# Рендеринг математических формул (LaTeX) в HTML/CSS.
# ----------------------------------------------------------------------
# Проект работает локально/офлайн, без внешних CDN, поэтому формулы
# разбираются СВОИМ небольшим конвертером LaTeX -> HTML: дроби, корни,
# степени/индексы, суммы/интегралы/пределы, греческие буквы, операторы и
# т.п. Результат — обычные HTML-элементы со стилями .math-* (см. style.css),
# никакого внешнего JS не требуется.
#
# Поддерживаются разделители:
#   $$…$$   и  \[…\]  — «выключная» формула (по центру, отдельным блоком);
#   $…$     и  \(…\)  — «строчная» формула (внутри текста).
# ======================================================================

# Греческие буквы и часто используемые символы (имя команды -> символ).
_MATH_SYMBOLS = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ",
    "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "pi": "π", "varpi": "ϖ", "rho": "ρ", "varrho": "ϱ", "sigma": "σ",
    "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "φ", "varphi": "φ",
    "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ",
    "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ",
    "Omega": "Ω",
    "cdot": "·", "times": "×", "div": "÷", "pm": "±", "mp": "∓",
    "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥", "ne": "≠", "neq": "≠",
    "approx": "≈", "equiv": "≡", "sim": "∼", "propto": "∝",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "leftrightarrows": "⇄",
    "in": "∈", "notin": "∉", "subset": "⊂", "supset": "⊃",
    "subseteq": "⊆", "supseteq": "⊇", "cup": "∪", "cap": "∩",
    "emptyset": "∅", "varnothing": "∅", "forall": "∀", "exists": "∃",
    "neg": "¬", "land": "∧", "lor": "∨", "infty": "∞", "partial": "∂",
    "nabla": "∇", "ldots": "…", "dots": "…", "cdots": "⋯",
    "angle": "∠", "perp": "⊥", "parallel": "∥", "degree": "°",
    "prime": "′", "ast": "∗",
    # Функции (печатаются прямым шрифтом).
    "sin": "sin", "cos": "cos", "tan": "tan", "cot": "cot", "sec": "sec",
    "csc": "csc", "log": "log", "ln": "ln", "exp": "exp", "lim": "lim",
    "max": "max", "min": "min", "arg": "arg", "gcd": "gcd", "mod": "mod",
}

# Операторы с индексами «снизу/сверху» (сумма, интеграл, предел …).
_MATH_BIG_OPS = {
    "sum": "∑", "prod": "∏", "int": "∫", "iint": "∬", "oint": "∮",
    "lim": "lim", "limsup": "lim sup", "liminf": "lim inf",
    "sup": "sup", "inf": "inf",
}


def _math_escape(s):
    """Экранирует XML-спецсимволы внутри формулы (текст уже не экранирован)."""
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;"))


def _tokenize_math(src):
    """Лёгкая токенизация LaTeX: символы, команды, группы {} и спецсимволы.

    Возвращает список токенов-строк. Группы {…} НЕ раскрываются здесь —
    рекурсию по ним делает _render_group.
    """
    tokens = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch == "\\":
            j = i + 1
            if j < n and src[j].isalpha():
                while j < n and src[j].isalpha():
                    j += 1
                tokens.append(src[i:j])          # \команда
                i = j
            else:
                tokens.append(src[i:j + 1])      # \x (экранированный символ)
                i = j + 1
        elif ch in "{}":
            tokens.append(ch)
            i += 1
        elif ch == "^" or ch == "_":
            tokens.append(ch)
            i += 1
        elif ch == " ":
            tokens.append(" ")
            i += 1
        else:
            tokens.append(ch)
            i += 1
    return tokens


def _read_group(tokens, start):
    """Читает один «аргумент» после start: либо {...}, либо один токен.

    Возвращает (список_токенов_аргумента, индекс_после_аргумента). Если
    следующего токена нет — возвращает ([], start).
    """
    j = start
    while j < len(tokens) and tokens[j] == " ":
        j += 1
    if j >= len(tokens):
        return [], j
    if tokens[j] == "{":
        depth = 0
        k = j
        while k < len(tokens):
            if tokens[k] == "{":
                depth += 1
            elif tokens[k] == "}":
                depth -= 1
                if depth == 0:
                    return tokens[j + 1:k], k + 1
            k += 1
        return tokens[j + 1:], len(tokens)   # незакрытая { — берём до конца
    return [tokens[j]], j + 1                # одиночный токен


def _render_tokens(tokens):
    """Рекурсивно превращает список токенов LaTeX в HTML-строку.

    Обрабатывает команды, степени ^, индексы _, дроби/корни и т.п.
    Неизвестные команды выводятся как есть (без слэша) — текст не теряется.
    """
    out = []
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == " ":
            out.append(" ")
            i += 1
            continue
        if tok == "{":
            group, i = _read_group(tokens, i)
            out.append(_render_tokens(group))
            continue
        if tok == "}":
            i += 1
            continue
        if tok == "^" or tok == "_":
            arg, i = _read_group(tokens, i + 1)
            inner = _render_tokens(arg)
            cls = "math-sup" if tok == "^" else "math-sub"
            tag = "sup" if tok == "^" else "sub"
            out.append("<%s class='%s'>%s</%s>" % (tag, cls, inner, tag))
            continue
        if tok.startswith("\\"):
            cmd = tok[1:]
            # \frac{a}{b}
            if cmd == "frac" or cmd == "dfrac" or cmd == "tfrac":
                num, i = _read_group(tokens, i + 1)
                den, i = _read_group(tokens, i)
                out.append("<span class='math-frac'>"
                           "<span class='math-num'>%s</span>"
                           "<span class='math-den'>%s</span></span>"
                           % (_render_tokens(num), _render_tokens(den)))
                continue
            # \sqrt[n]{x}
            if cmd == "sqrt":
                # Необязательный показатель степени: \sqrt[3]{x}
                deg = None
                j = i + 1
                while j < len(tokens) and tokens[j] == " ":
                    j += 1
                if j < len(tokens) and tokens[j] == "[":
                    k = j + 1
                    buf = []
                    while k < len(tokens) and tokens[k] != "]":
                        buf.append(tokens[k])
                        k += 1
                    deg = buf
                    i = k  # съели до ']'
                body, i = _read_group(tokens, i + 1)
                root = "<span class='math-root'>%s</span>" % _render_tokens(body)
                if deg:
                    root = ("<span class='math-sqrtdeg'>%s</span>%s"
                            % (_render_tokens(deg), root))
                out.append("<span class='math-sqrt'>&radic;" + root + "</span>")
                continue
            # \text{…} / \mathrm{…} / \mathbf{…} — прямым/полужирным шрифтом.
            if cmd in ("text", "mathrm", "operatorname", "mathbf", "mathit"):
                body, i = _read_group(tokens, i + 1)
                cls = {"mathbf": "math-bf", "mathit": "math-it"}.get(cmd, "math-rm")
                out.append("<span class='%s'>%s</span>"
                           % (cls, _math_escape(_plain_group(body))))
                continue
            # \left / \right — просто игнорируем (размер скобок неважен).
            if cmd in ("left", "right"):
                i += 1
                continue
            # Операторы с индексами: \sum_{..}^{..}, \int, \lim и т.п.
            if cmd in _MATH_BIG_OPS:
                sym = _MATH_BIG_OPS[cmd]
                op = "<span class='math-op'>%s</span>" % sym
                sub, sup = None, None
                # читаем возможно идущие _{...} и ^{...}
                while i + 1 < n and tokens[i + 1] in ("_", "^"):
                    which = tokens[i + 1]
                    arg, i = _read_group(tokens, i + 2)
                    if which == "_":
                        sub = _render_tokens(arg)
                    else:
                        sup = _render_tokens(arg)
                if sub is not None:
                    op += "<sub class='math-sub'>%s</sub>" % sub
                if sup is not None:
                    op += "<sup class='math-sup'>%s</sup>" % sup
                out.append(op)
                i += 1
                continue
            # \vec{x}, \hat{x}, \bar{x}, \dot{x} — с акцентом.
            if cmd in ("vec", "hat", "bar", "dot", "ddot", "tilde", "overline",
                       "underline"):
                body, i = _read_group(tokens, i + 1)
                mark = {"vec": "→", "hat": "^", "bar": "‾", "overline": "‾",
                        "dot": "·", "ddot": "··", "tilde": "~",
                        "underline": "_"}.get(cmd, "")
                out.append("<span class='math-accent'>%s<span class='math-acc'>%s"
                           "</span></span>" % (_render_tokens(body), mark))
                continue
            # \mathbb{R} и т.п. — как обычный текст.
            if cmd in ("mathbb", "mathcal", "mathfrak", "mathsf", "mathtt"):
                body, i = _read_group(tokens, i + 1)
                out.append("<span class='math-rm'>%s</span>"
                           % _math_escape(_plain_group(body)))
                continue
            # Символы/функции по таблице.
            if cmd in _MATH_SYMBOLS:
                sym = _MATH_SYMBOLS[cmd]
                if cmd in ("sin", "cos", "tan", "cot", "sec", "csc", "log",
                           "ln", "exp", "max", "min", "arg", "gcd", "mod",
                           "lim", "sup", "inf"):
                    out.append("<span class='math-fn'>%s</span>" % sym)
                else:
                    out.append("<span class='math-sym'>%s</span>"
                               % _math_escape(sym))
                i += 1
                continue
            # Экранированные спецсимволы: \{ \} \% \_ \& \$ \# \,
            if cmd in ("{", "}", "%", "_", "&", "$", "#", ",", ";", "!", " "):
                out.append("&nbsp;" if cmd in (",", ";", "!", " ")
                           else _math_escape(cmd))
                i += 1
                continue
            # Неизвестная команда — выводим без слэша.
            out.append(_math_escape(cmd))
            i += 1
            continue
        # Обычный символ.
        out.append(_math_escape(tok))
        i += 1
    return "".join(out)


def _plain_group(tokens):
    """Собирает «плоский» текст группы (для \\text{…} и \\mathbb{…})."""
    parts = []
    for t in tokens:
        if t == "{":
            continue
        if t == "}":
            continue
        if t.startswith("\\"):
            parts.append(_MATH_SYMBOLS.get(t[1:], t[1:]))
        elif t in ("^", "_"):
            parts.append({"^": "^", "_": "_"}[t])
        else:
            parts.append(t)
    return "".join(parts)


def render_math(latex, display=False):
    """Рендерит одну формулу (без разделителей) в HTML.

    display=True — выключная (блочная) формула по центру.
    """
    src = str(latex or "").strip()
    inside = _render_tokens(_tokenize_math(src))
    cls = "math-display" if display else "math-inline"
    return "<span class='%s'>%s</span>" % (cls, inside)


# Регулярное выражение для поиска формул вместе с разделителями.
# Порядок альтернатив важен: сначала $$…$$, затем \[…\], потом $…$ и \(…\).
_MATH_RE = re.compile(
    r"(\$\$(?P<d1>.+?)\$\$)"                       # $$ … $$   (блок)
    r"|(\\\[(?P<d2>.+?)\\\])"                      # \[ … \]   (блок)
    r"|(?<!\\)(?<!\$)\$(?P<i1>.+?)(?<!\\)\$(?!\$)"  # $ … $     (строчная)
    r"|(\\\((?P<i2>.+?)\\\))"                      # \( … \)   (строчная)
    , re.DOTALL)


# Признаки того, что содержимое $…$ — действительно ФОРМУЛА, а не просто
# «доллары» в тексте (например «Цена $5 и $10»). Строчная $…$ принимается,
# если внутри есть TeX-команда (\…) ИЛИ математические символы/операторы.
_MATH_HINT_RE = re.compile(r"[\\^_{}=<>|*]|\d\s*[a-zA-Z]|[a-zA-Z]\s*[+\-/]\s*\d")


def _looks_like_math(inner):
    """Похоже ли содержимое $…$ на формулу (а не на цену/доллары в тексте)."""
    s = inner.strip()
    if not s:
        return False
    # Есть TeX-команда, степень/индекс, фигурные скобки, знак = и т.п.
    if _MATH_HINT_RE.search(s):
        return True
    # Короткая строка без пробелов из букв/цифр (одна переменная: $x$, $n$).
    if len(s) <= 3 and " " not in s and re.fullmatch(r"[a-zA-Z]+", s):
        return True
    return False


def extract_and_render_math(text):
    """Находит формулы в «сыром» тексте и заменяет их на HTML.

    Возвращает (преобразованный_текст, список_html_формул). Преобразованный
    текст содержит плейсхолдеры вида \\x00MATH0\\x00 вместо формул — их
    позже (после экранирования и markdown) заменяют обратно на HTML.

    Так формулы защищаются от экранирования и от разбора Markdown
    (звёздочки/подчёркивания внутри формул не ломают разметку).
    """
    formulas = []

    def _repl(m):
        if m.group("d1") is not None:
            latex, display = m.group("d1"), True
        elif m.group("d2") is not None:
            latex, display = m.group("d2"), True
        elif m.group("i1") is not None:
            latex, display = m.group("i1"), False
            # Строчная $…$ — принимаем ТОЛЬКО если содержимое похоже на
            # формулу (иначе «Цена $5 и $10» превратилось бы в формулу).
            if not _looks_like_math(latex):
                return m.group(0)          # оставляем как есть
        else:
            latex, display = m.group("i2"), False
        html = render_math(latex, display=display)
        idx = len(formulas)
        formulas.append(html)
        return "\x00MATH%d\x00" % idx

    new_text = _MATH_RE.sub(_repl, text)
    return new_text, formulas


def restore_math(text, formulas):
    """Возвращает HTML-формулы на место плейсхолдеров (после экранирования)."""
    if not formulas:
        return text
    for idx, html in enumerate(formulas):
        text = text.replace("\x00MATH%d\x00" % idx, html)
    return text


def apply_inline_markdown(text):
    """Преобразует inline-Markdown (жирный, курсив, код) в HTML-теги.

    Вызывается ПОСЛЕ escape_html — на безопасном тексте, у которого нет
    настоящих тегов, поэтому подстановка не ломает уже сгенерированный код.
    Порядок: сначала код (чтобы звёздочки/подчёркивания внутри него не
    размечались), затем жирный, затем курсив.
    """
    # inline-код
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    # жирный **text** / __text__
    text = re.sub(r"\*\*([^*]+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"__([^_]+?)__", r"<strong>\1</strong>", text)
        # курсив *text* / _text_
    text = re.sub(r"\*([^*\n]+?)\*", r"<em>\1</em>", text)
    text = re.sub(r"_([^_\n]+?)_", r"<em>\1</em>", text)
    return text


def parse_markdown_table_block(lines, start_idx):
    """Собирает строки Markdown-таблицы, начиная с start_idx, в HTML-таблицу.

    Возвращает кортеж (html_таблица, индекс_последней_обработанной_строки).
    """
    header_cells = [c.strip().replace("**", "") for c in lines[start_idx].strip("|").split("|")]
    i = start_idx
    rows = []

    # Пропускаем разделительную строку (например, |---|---|---|)
    j = start_idx + 1
    if j < len(lines) and re.match(r"^\s*\|?[\s:\-|]+\|?\s*$", lines[j]):
        j += 1

    for k in range(j, len(lines)):
        line = lines[k].strip()
        if not line.startswith("|"):
            break
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)

    table_html = ["<table>", "<thead><tr>"]
    for idx, h in enumerate(header_cells):
        if idx == 1:
            table_html.append(f"<th class='desc-col'>{h}</th>")
        else:
            table_html.append(f"<th>{h}</th>")
    table_html.append("</tr></thead><tbody>")
    for row in rows:
        table_html.append("<tr>")
        for idx, cell in enumerate(row):
            if idx == 0:
                table_html.append(f"<td class='date-col'>{cell}</td>")
            elif idx == 1:
                table_html.append(f"<td class='desc-col'>{cell}</td>")
            elif idx == 2:
                table_html.append(f"<td class='size-col'>{cell}</td>")
            else:
                table_html.append(f"<td>{cell}</td>")
        table_html.append("</tr>")
    table_html.append("</tbody></table>")

    return "\n".join(table_html), j - 1


def text_to_html_paragraphs(text, memory=None):
    """Разбивает текст на абзацы, списки и Markdown-таблицы.

    Дополнительно помечает фрагменты, совпадающие с данными ПАМЯТИ агента
    (значения/ключи рабочей — фисташковым, долговременной — фуксией) и
    подсвечивает их цветом. Разметка ставится детерминированно по memory
    (dict вида {"working": {...}, "longterm": {...}}), а также распознаются
    маркеры [[R]]/[[L]], если их расставила сама модель.
    """
    html, _counts = render_with_memory_counts(text, memory)
    return html


def render_with_memory_counts(text, memory=None):
    """Как text_to_html_paragraphs, но ещё возвращает СЧЁТЧИКИ использований.

    Возвращает кортеж (html, counts), где counts — {"working": N,
    "longterm": M}: сколько фрагментов ответа заимствовано из рабочей и
    долговременной памяти (по полным совпадениям с учётом границ слов и/или
    маркеров, расставленных моделью).
    """
    counts = {"working": 0, "longterm": 0}
    # 0) ВЫДЕЛЯЕМ формулы из «сырого» текста в плейсхолдеры — до разметки
    #    памяти и экранирования, чтобы символы формул (звёздочки, подчёрки-
    #    вания, слэши) не ломали ни подсветку памяти, ни Markdown, ни HTML.
    text, math_html = extract_and_render_math(text)
    # 1) Детерминированная разметка фрагментов памяти — до экранирования,
    #    пока текст ещё «сырой» (маркеры [[…]] экранирование не затрагивает).
    #    Считаем ТОЛЬКО реально поставленные метки (возвращает функция).
    if memory:
        text, det = mark_memory_fragments_ex(text, memory)
        counts["working"] += det["working"]
        counts["longterm"] += det["longterm"]
    text = escape_html(text)
    text = apply_inline_markdown(text)
    # Возвращаем формулы на место (их HTML не должен проходить ни через
    # экранирование, ни через inline-Markdown — они уже готовы).
    text = restore_math(text, math_html)
    # 2) Маркеры, расставленные САМОЙ моделью ([[R]]/[[L]]): учитываем те,
    #    что остались сверх уже подсчитанных детерминированных меток.
    counts["working"] = max(counts["working"], text.count(_MEM_OPEN_R))
    counts["longterm"] = max(counts["longterm"], text.count(_MEM_OPEN_L))
    # Подсветка фрагментов из памяти агента (после экранирования —
    # вставка своих <span> безопасна).
    text = render_memory_marks(text)
    lines = text.split("\n")
    html = []
    in_list = False
    i = 0

    while i < len(lines):
        line = lines[i].rstrip()

        # Если строка — начало Markdown-таблицы (содержит "|")
        if line.strip().startswith("|") and "|" in line[1:]:
            if in_list:
                html.append("</ul>")
                in_list = False
            table_html, i = parse_markdown_table_block(lines, i)
            html.append(table_html)
            i += 1
            continue

        if not line:
            if in_list:
                html.append("</ul>")
                in_list = False
            i += 1
            continue

        if line.startswith("### "):
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f"<h4>{line[4:]}</h4>")
        elif line.startswith("## "):
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f"<h3>{line[3:]}</h3>")
        elif line.startswith("# "):
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f"<h2>{line[2:]}</h2>")
        elif line.strip().startswith("- "):
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{line.strip()[2:]}</li>")
        elif re.match(r"^\d+\.\s", line.strip()):
            if not in_list:
                html.append("<ul>")
                in_list = True
            html.append(f"<li>{re.sub(r'^\d+\.\s', '', line.strip())}</li>")
        else:
            if in_list:
                html.append("</ul>")
                in_list = False
            html.append(f"<p>{line}</p>")

        i += 1

    if in_list:
        html.append("</ul>")

    return "\n".join(html), counts
