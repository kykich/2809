"""MCP-сервер «Погода» — текущая погода и прогноз (OpenWeatherMap).

Отдельный MCP-сервер проекта: предоставляет инструменты для работы с
погодой через API https://openweathermap.org:
  * find_city        — найти город по названию (получить координаты/страну);
  * current_weather  — текущая погода в городе (температура, описание, ветер…);
  * forecast         — прогноз на несколько дней (3-часовые шаги, агрегировано
                       по дням: мин/макс, описание);
  * ping             — проверка живости сервера.

СБОРЩИК И ХРАНИТЕЛЬ (накопление запросов):
  * сборщик   (collector) — принимает пары «запрос погоды + ответ» от
                инструментов погоды и накапливает их. Как только накопилось
                COLLECT_THRESHOLD (= 5) ответов — АВТОМАТИЧЕСКИ передаёт их
                «хранителю» и очищает накопитель;
  * хранитель (keeper)    — принимает запросы от сборщика, ГРУППИРУЕТ их по
                КОДУ СТРАНЫ и сохраняет в табличном виде в файл weathe.txt.

Общая логика: запрос погоды -> ответ (хранит сборщик) -> когда ответов
становится 5 -> передача хранителю.

API-ключ берётся из файла w.txt (одна строка — ключ OpenWeatherMap).
Файл НЕ коммитится (см. .gitignore). Внешних зависимостей не требуется —
используется стандартная библиотека urllib (как у «Конвертера»).

ВАЖНО: OpenWeatherMap активирует новый ключ не сразу (обычно до ~10–120 минут
после регистрации). Пока ключ не активирован, API отвечает HTTP 401
«Invalid API key» — сервер вернёт понятное сообщение об этом.

Запуск (как stdio-подпроцесс MCP):
    python weather_server.py

Переключение проекта на этот сервер — в rtk_app/config.py (MCP_SERVERS).
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.mcpserver import MCPServer

# BASE_DIR — папка проекта (рядом с config.py); учитываем запуск из др. CWD.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
# now_str — общий форматировщик времени проекта (как у Завхоза).
try:
    from rtk_app.jobs_store import now_str
except Exception:                                  # сервер должен работать и без пакета
    def now_str():
        import time
        return time.strftime("%Y-%m-%d %H:%M:%S")

mcp = MCPServer("weather")

# Порог сборщика: сколько ответов накопить, прежде чем передать хранителю.
COLLECT_THRESHOLD = 5

# Файл-накопитель сборщика (в session/, папка не коммитится).
COLLECTOR_FILE = os.path.join(BASE_DIR, "session", "weather_collector.json")
# Файл-хранилище хранителя (таблица по странам). Имя задано в задаче.
KEEPER_FILE = os.path.join(BASE_DIR, "weathe.txt")

# Базовые адреса API OpenWeatherMap.
API_CURRENT = "https://api.openweathermap.org/data/2.5/weather"
API_FORECAST = "https://api.openweathermap.org/data/2.5/forecast"
API_GEOCODE = "https://api.openweathermap.org/geo/1.0/direct"

# Единицы измерения и язык описаний.
UNITS = "metric"      # градусы Цельсия, м/с
LANG = "ru"           # описания погоды на русском


# --------------------------------------------------------------------------
# Ключ и HTTP
# --------------------------------------------------------------------------
def _api_key():
    """Читает API-ключ OpenWeatherMap из w.txt (одна строка)."""
    path = os.environ.get("OWM_KEY_FILE") or os.path.join(BASE_DIR, "w.txt")
    if not os.path.isfile(path):
        raise RuntimeError("не найден файл с API-ключом: %s" % path)
    with open(path, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("w.txt пуст — положите в него API-ключ одной строкой")
    return lines[0]


def _get_json(url, params):
    """GET-запрос к API OpenWeatherMap и разбор JSON-ответа.

    url    — базовый адрес; params — dict параметров (добавим appid/units/lang).
    Возвращает разобранный dict. Сетевые/HTTP-ошибки и ошибки API приводятся
    к понятному текстовому сообщению через исключение.
    """
    params = dict(params or {})
    params.setdefault("appid", _api_key())
    params.setdefault("units", UNITS)
    params.setdefault("lang", LANG)
    full = "%s?%s" % (url, urllib.parse.urlencode(params))
    req = urllib.request.Request(full, headers={"User-Agent": "rtk-mcp-weather/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # OpenWeatherMap в теле ошибки возвращает {"cod":..., "message":...}.
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("message", "")
        except Exception:
            pass
        if exc.code == 401:
            raise RuntimeError(
                "недействительный API-ключ OpenWeatherMap (401). Проверьте w.txt "
                "и дождитесь активации ключа (до ~2 часов после регистрации). "
                "%s" % detail)
        if exc.code == 404:
            raise RuntimeError("город не найден (404). %s" % detail)
        raise RuntimeError("HTTP-%s от OpenWeatherMap. %s" % (exc.code, detail))
    except urllib.error.URLError as exc:
        raise RuntimeError("нет связи с OpenWeatherMap: %s" % exc.reason)
    if not isinstance(data, (dict, list)):
        raise RuntimeError("неожиданный ответ OpenWeatherMap")
    return data


# --------------------------------------------------------------------------
# Вспомогательные функции
# --------------------------------------------------------------------------
def _fmt_temp(value):
    """Форматирует температуру: одна цифра после запятой или «?»."""
    try:
        return "%.1f°C" % float(value)
    except (TypeError, ValueError):
        return "?"


def _find_city(query):
    """Ищет город через Geocoding API. Возвращает dict или None.

    query — название города (можно с кодом страны: «Москва,RU»).
    Результат — первый найденный: {"name", "country", "state", "lat", "lon"}.
    """
    data = _get_json(API_GEOCODE, {"q": query, "limit": 1})
    if not isinstance(data, list) or not data:
        return None
    item = data[0]
    return {
        "name": item.get("name") or query,
        "country": item.get("country") or "",
        "state": item.get("state") or "",
        "lat": item.get("lat"),
        "lon": item.get("lon"),
    }


def _fmt_city(city):
    """Читаемое имя города «Москва (RU)»."""
    name = city.get("name") or "?"
    extra = []
    if city.get("state"):
        extra.append(str(city["state"]))
    if city.get("country"):
        extra.append(str(city["country"]))
    return "%s (%s)" % (name, ", ".join(extra)) if extra else name


# --------------------------------------------------------------------------
# СБОРЩИК: накопление пар «запрос + ответ»; на пороге — передача хранителю
# --------------------------------------------------------------------------
def _collector_path():
    """Путь к файлу-накопителю сборщика (можно переопределить окружением)."""
    return os.environ.get("WEATHER_COLLECTOR_FILE") or COLLECTOR_FILE


def _keeper_path():
    """Путь к файлу-хранилищу хранителя (можно переопределить окружением)."""
    return os.environ.get("WEATHER_KEEPER_FILE") or KEEPER_FILE


def _load_collector():
    """Читает накопитель. Возвращает список записей (пустой при ошибке)."""
    path = _collector_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, dict):
        items = data.get("items")
        return items if isinstance(items, list) else []
    return data if isinstance(data, list) else []


def _save_collector(items):
    """Атомарно пишет накопитель сборщика (JSON)."""
    path = _collector_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"updated": now_str(), "count": len(items), "items": items},
                  f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _record_request(kind, query, country, country_name, summary):
    """СБОРЩИК: записывает пару «запрос погоды + ответ» и проверяет порог.

    kind         — вид запроса ("current"/"forecast");
    query        — исходный запрос пользователя (город);
    country      — КОД страны (например, "RU");
    country_name — читаемое имя города/страны;
    summary      — краткий ответ (текст погоды).

    Возвращает dict: {count, threshold, forwarded, keeper} — сколько накоплено,
    достигнут ли порог, была ли передача хранителю и её отчёт.
    """
    items = _load_collector()
    items.append({
        "ts": now_str(),
        "kind": str(kind),
        "query": str(query or ""),
        "country": (str(country or "").upper() or "??"),
        "country_name": str(country_name or ""),
        "summary": str(summary or ""),
    })
    forwarded = False
    keeper_report = None
    # ПРИ НАКОПЛЕНИИ 5 ОТВЕТОВ — передаём ВСЁ хранителю и очищаем накопитель.
    if len(items) >= COLLECT_THRESHOLD:
        batch = items[:]
        keeper_report = _keeper_accept(batch)
        forwarded = True
        items = []            # накопитель сброшен после передачи
    _save_collector(items)
    return {
        "count": len(items),
        "threshold": COLLECT_THRESHOLD,
        "forwarded": forwarded,
        "keeper": keeper_report,
    }


# --------------------------------------------------------------------------
# ХРАНИТЕЛЬ: приём запросов от сборщика, группировка по стране, таблица в файл
# --------------------------------------------------------------------------
def _keeper_accept(batch):
    """ХРАНИТЕЛЬ: принимает запросы от сборщика, группирует по КОДУ СТРАНЫ
    и сохраняет в табличном виде в файл weathe.txt.

    batch — список записей сборщика (см. _record_request). Возвращает
    текстовый отчёт: сколько записей принято, по скольким странам, файл.
    """
    try:
        table = _keeper_build_table(batch)
        path = _keeper_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(table)
        os.replace(tmp, path)
    except Exception as exc:
        return "Хранитель: не удалось сохранить weathe.txt: %s" % exc
    countries = sorted({(r.get("country") or "??")
                        for r in batch if isinstance(r, dict)})
    return ("Хранитель: принято %d запис. по %d странам(е): %s -> %s"
            % (len(batch), len(countries), ", ".join(countries) or "—",
               _keeper_path()))


def _keeper_build_table(batch):
    """Строит ТАБЛИЧНЫЙ текст (группировка по коду страны).

    Возвращает строку: заголовок, затем по каждой стране — блок с
    под-таблицей её запросов (время, вид, город, погода).
    """
    # Группируем записи по коду страны.
    by_country = {}
    for r in batch:
        if not isinstance(r, dict):
            continue
        code = (r.get("country") or "??").upper()
        by_country.setdefault(code, []).append(r)

    lines = []
    lines.append("=" * 72)
    lines.append("ХРАНИТЕЛЬ ПОГОДЫ — таблица запросов, сгруппированная по странам")
    lines.append("Обновлено: %s · записей: %d · стран: %d"
                 % (now_str(), len(batch), len(by_country)))
    lines.append("=" * 72)
    for code in sorted(by_country):
        rows = by_country[code]
        lines.append("")
        lines.append("Страна: %s (%d запис.)" % (code, len(rows)))
        lines.append("-" * 72)
        lines.append("%-19s %-9s %-16s %s"
                     % ("Время", "Вид", "Город", "Погода"))
        lines.append("-" * 72)
        for r in rows:
            kind = "текущая" if r.get("kind") == "current" else "прогноз"
            city = str(r.get("country_name") or r.get("query") or "")[:16]
            # Погоду делаем однострочной для таблицы.
            summ = " ".join(str(r.get("summary") or "").split())
            if len(summ) > 40:
                summ = summ[:39] + "…"
            lines.append("%-19s %-9s %-16s %s"
                         % (str(r.get("ts") or "")[:19], kind, city, summ))
        lines.append("-" * 72)
    lines.append("")
    return "\n".join(lines)



# --------------------------------------------------------------------------
# Инструменты MCP
# --------------------------------------------------------------------------
@mcp.tool()
def ping() -> str:
    """Проверка живости сервера «Погода»."""
    return "Погода на связи."


@mcp.tool()
def find_city(city: str) -> str:
    """Найти город по названию (страна, координаты).

    city — название города (можно с кодом страны: «Москва», «Moscow,RU»,
    «London,GB»). Возвращает страну и координаты найденного города.
    """
    city = str(city or "").strip()
    if not city:
        return "Укажите название города (city)."
    try:
        found = _find_city(city)
    except Exception as exc:
        return "Ошибка поиска города: %s" % exc
    if not found:
        return "Город «%s» не найден." % city
    return ("Найден: %s; координаты: %s, %s"
            % (_fmt_city(found), found.get("lat"), found.get("lon")))


@mcp.tool()
def current_weather(city: str = "") -> str:
    """Текущая погода в городе (OpenWeatherMap).

    city — название города (например, «Москва», «Moscow,RU», «London»).
    Можно указать и координаты через запятую: «55.75,37.62».
    Возвращает: температуру, ощущаемую, описание, ветер, влажность, давление.
    """
    city = str(city or "").strip()
    if not city:
        return "Укажите город (city)."

    # Координаты «lat,lon» — поддерживаем напрямую.
    params = {}
    where = ""
    country = ""
    try:
        lat_s, lon_s = city.split(",")
        lat, lon = float(lat_s), float(lon_s)
        params = {"lat": lat, "lon": lon}
        where = "%.4f, %.4f" % (lat, lon)
    except (ValueError, AttributeError):
        # Обычное название города — сначала уточняем координаты (для подписи).
        try:
            found = _find_city(city)
        except Exception as exc:
            return "Ошибка поиска города: %s" % exc
        if found is None:
            return "Город «%s» не найден." % city
        params = {"lat": found["lat"], "lon": found["lon"]}
        where = _fmt_city(found)
        country = found.get("country") or ""

    try:
        data = _get_json(API_CURRENT, params)
    except Exception as exc:
        return "Ошибка получения погоды: %s" % exc

    main = data.get("main") or {}
    weather = (data.get("weather") or [{}])[0]
    wind = data.get("wind") or {}
    # Код страны может прийти и в самом ответе погоды (sys.country).
    if not country:
        country = (data.get("sys") or {}).get("country") or ""
    result = (
        "Погода — %s:\n"
        "  %s, %s\n"
        "  температура: %s (ощущается %s)\n"
        "  ветер: %.1f м/с, влажность: %s%%, давление: %s гПа"
        % (where,
           weather.get("description") or "нет данных",
           "облачность %s%%" % (data.get("clouds") or {}).get("all", "?"),
           _fmt_temp(main.get("temp")), _fmt_temp(main.get("feels_like")),
           float(wind.get("speed", 0) or 0),
           main.get("humidity", "?"), main.get("pressure", "?"))
    )
    # СБОРЩИК: сохраняем пару «запрос + ответ»; на 5-м ответе — передача
    # хранителю (группировка по стране, таблица в weathe.txt).
    _record_request("current", city, country, where, result)
    return result


@mcp.tool()
def forecast(city: str = "", days: int = 3) -> str:
    """Прогноз погоды по дням (OpenWeatherMap, 3-часовые шаги).

    city — название города (например, «Москва»); можно «lat,lon».
    days — на сколько дней вперёд (1–5; по умолчанию 3).

    Возвращает по каждому дню: мин/макс температуру, преобладающее описание
    и число замеров. Five-day / 3-hour forecast покрывает до 5 суток.
    """
    city = str(city or "").strip()
    if not city:
        return "Укажите город (city)."
    try:
        days = int(days) if days is not None else 3
    except (TypeError, ValueError):
        days = 3
    days = min(max(days, 1), 5)

    params = {}
    where = ""
    country = ""
    try:
        lat_s, lon_s = city.split(",")
        lat, lon = float(lat_s), float(lon_s)
        params = {"lat": lat, "lon": lon}
        where = "%.4f, %.4f" % (lat, lon)
    except (ValueError, AttributeError):
        try:
            found = _find_city(city)
        except Exception as exc:
            return "Ошибка поиска города: %s" % exc
        if found is None:
            return "Город «%s» не найден." % city
        params = {"lat": found["lat"], "lon": found["lon"]}
        where = _fmt_city(found)
        country = found.get("country") or ""

    try:
        data = _get_json(API_FORECAST, params)
    except Exception as exc:
        return "Ошибка получения прогноза: %s" % exc

    if not country:
        country = (data.get("city") or {}).get("country") or ""

    items = data.get("list") or []
    if not items:
        return "Прогноз не получен."

    # Агрегируем 3-часовые записи по дате (день).
    by_day = {}
    order = []
    for it in items:
        dt = str(it.get("dt_txt") or "")          # "YYYY-MM-DD HH:MM:SS"
        day = dt.split(" ")[0]
        if not day:
            continue
        if day not in by_day:
            by_day[day] = {"temps": [], "desc": {}, "count": 0}
            order.append(day)
        main = it.get("main") or {}
        try:
            by_day[day]["temps"].append(float(main.get("temp")))
        except (TypeError, ValueError):
            pass
        desc = ((it.get("weather") or [{}])[0]).get("description") or ""
        if desc:
            by_day[day]["desc"][desc] = by_day[day]["desc"].get(desc, 0) + 1
        by_day[day]["count"] += 1

    lines = ["Прогноз — %s (на %d дн.):" % (where, days)]
    for day in order[:days]:
        d = by_day[day]
        if not d["temps"]:
            continue
        lo, hi = min(d["temps"]), max(d["temps"])
        desc = max(d["desc"].items(), key=lambda kv: kv[1])[0] if d["desc"] else "—"
        lines.append("  %s: %.1f…%.1f°C, %s (%d замер.)"
                     % (day, lo, hi, desc, d["count"]))
    result = "\n".join(lines)
    # СБОРЩИК: сохраняем пару «запрос + ответ»; на 5-м ответе — передача
    # хранителю (группировка по стране, таблица в weathe.txt).
    _record_request("forecast", city, country, where, result)
    return result


@mcp.tool()
def collector_status() -> str:
    """СБОРЩИК: сколько запросов накоплено (из порога в 5 ответов).

    Показывает текущее число пар «запрос + ответ» в накопителе. Как только
    число достигнет порога (5), сборщик автоматически передаст их хранителю.
    """
    items = _load_collector()
    countries = sorted({(r.get("country") or "??")
                        for r in items if isinstance(r, dict)})
    return ("Сборщик: накоплено %d из %d ответов; страны: %s; файл: %s"
            % (len(items), COLLECT_THRESHOLD, ", ".join(countries) or "—",
               _collector_path()))


@mcp.tool()
def collector_flush() -> str:
    """СБОРЩИК: принудительно передать накопленное ХРАНИТЕЛЮ и очистить.

    Полезно, чтобы не ждать порог в 5 ответов. Возвращает отчёт хранителя.
    """
    items = _load_collector()
    if not items:
        return "Сборщик пуст — передавать нечего."
    report = _keeper_accept(items)
    _save_collector([])
    return report


@mcp.tool()
def keeper_report() -> str:
    """ХРАНИТЕЛЬ: показать содержимое таблицы weathe.txt (сгруппировано по странам)."""
    path = _keeper_path()
    if not os.path.isfile(path):
        return "Хранилище пусто (weathe.txt ещё не создан)."
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception as exc:
        return "Не удалось прочитать weathe.txt: %s" % exc


if __name__ == "__main__":
    # Транспорт по умолчанию — stdio (как подпроцесс MCP-клиента проекта).
    mcp.run()
