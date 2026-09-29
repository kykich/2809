# -*- coding: utf-8 -*-
"""Автономная проверка сборщика и хранителя погоды (MCP «Погода»).

Сеть и реальные API НЕ требуются: подменяем сетевые функции _find_city /
_get_json, а проверяем ЛОГИКУ накопления:
  * СБОРЩИК принимает пары «запрос погоды + ответ» и накапливает их;
  * при накоплении 5 ответов — АВТОМАТИЧЕСКИ передаёт их ХРАНИТЕЛЮ и
    очищает накопитель;
  * ХРАНИТЕЛЬ группирует запросы по КОДУ СТРАНЫ и пишет таблицу в weathe.txt.

Запуск:
    python tests/check_weather.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.harness import check, section, finish, ensure_utf8

import weather_server as ws


# Данные для подмены сети: город -> (координаты, код страны, погода).
_CITIES = {
    "Москва": ({"name": "Москва", "country": "RU", "state": "",
                "lat": 55.75, "lon": 37.62}, "RU"),
    "Берлин": ({"name": "Берлин", "country": "DE", "state": "",
                "lat": 52.52, "lon": 13.40}, "DE"),
    "Париж": ({"name": "Париж", "country": "FR", "state": "",
               "lat": 48.85, "lon": 2.35}, "FR"),
}


def main():
    ensure_utf8()

    # Изолируем файлы сборщика и хранителя во временных путях.
    fd, coll = tempfile.mkstemp(suffix=".json"); os.close(fd); os.remove(coll)
    fd, keep = tempfile.mkstemp(suffix=".txt"); os.close(fd); os.remove(keep)
    os.environ["WEATHER_COLLECTOR_FILE"] = coll
    os.environ["WEATHER_KEEPER_FILE"] = keep

    # Подменяем сетевые функции: сеть НЕ используется.
    def fake_find_city(query):
        return _CITIES.get(str(query).strip(), (None, ""))[0]

    def fake_get_json(url, params):
        # current_weather -> dict погоды; forecast -> dict с 'list'.
        if url == ws.API_CURRENT:
            return {"main": {"temp": 10.0, "feels_like": 8.0,
                             "humidity": 70, "pressure": 1010},
                    "weather": [{"description": "ясно"}],
                    "wind": {"speed": 3.0},
                    "clouds": {"all": 10},
                    "sys": {"country": "RU"}}
        if url == ws.API_FORECAST:
            return {"city": {"country": "RU"},
                    "list": [{"dt_txt": "2026-09-26 09:00",
                              "main": {"temp": 9.0},
                              "weather": [{"description": "облачно"}]}]}
        return {}

    real_find, real_get = ws._find_city, ws._get_json
    ws._find_city = fake_find_city
    ws._get_json = fake_get_json
    try:
        # --------------------------------------------------------------
        section("1. Сборщик: накопление пар «запрос + ответ»")
        # Начинаем с пустого накопителя.
        ws._save_collector([])
        check("накопитель пуст на старте", ws._load_collector() == [])

        r1 = ws.current_weather("Москва")
        check("weather вернул погоду", "Погода" in r1, r1[:40])
        items = ws._load_collector()
        check("после 1 запроса в накопителе 1 запись", len(items) == 1,
              "%r" % items)
        check("записана страна RU", items[0].get("country") == "RU", "%r" % items[0])
        check("записан вид current", items[0].get("kind") == "current")
        check("записан читаемый город", items[0].get("country_name"))
        check("до порога передачи хранителю НЕ было",
              not os.path.isfile(keep), "файл хранителя не должен быть создан")

        # Ещё 3 запроса — всего 4, порог (5) ещё не достигнут.
        ws.current_weather("Берлин")
        ws.current_weather("Париж")
        ws.forecast("Москва", days=1)
        items = ws._load_collector()
        check("после 4 запросов в накопителе 4 записи", len(items) == 4,
              "%r" % len(items))
        check("файл хранителя ещё НЕ создан (порог 5)",
              not os.path.isfile(keep))

        # --------------------------------------------------------------
        section("2. На 5-м ответе — передача хранителю и очистка сборщика")
        r5 = ws.current_weather("Москва")
        check("5-й запрос отработал", "Погода" in r5)
        items = ws._load_collector()
        check("сборщик ОЧИЩЕН после передачи", items == [], "%r" % items)
        check("файл хранителя СОЗДАН", os.path.isfile(keep))
        with open(keep, encoding="utf-8") as f:
            table = f.read()
        check("таблица содержит заголовок «ХРАНИТЕЛЬ ПОГОДЫ»",
              "ХРАНИТЕЛЬ ПОГОДЫ" in table)
        check("таблица сгруппирована по RU", "Страна: RU" in table)
        check("таблица сгруппирована по DE", "Страна: DE" in table)
        check("таблица сгруппирована по FR", "Страна: FR" in table)
        check("в таблице есть «Москва»", "Москва" in table)
        check("в таблице есть колонка «Погода»", "Погода" in table)
        # Все 5 записей попали в таблицу (3 RU, 1 DE, 1 FR).
        check("RU-блок содержит 3 записи", "(3 запис.)" in table, table)
        check("DE-блок содержит 1 запись", "(1 запис.)" in table)
        check("FR-блок содержит 1 запись", "(1 запис.)" in table)

        # --------------------------------------------------------------
        section("3. Явные инструменты сборщика/хранителя")
        st = ws.collector_status()
        check("collector_status сообщает об очистке",
              "накоплено 0 из 5" in st, st)
        # Принудительная передача: накопим 2 и передадим вручную.
        ws.current_weather("Берлин")
        ws.current_weather("Париж")
        check("после 2 запросов накоплено 2", "накоплено 2 из 5" in
              ws.collector_status())
        rep = ws.collector_flush()
        check("flush передал хранителю", "Хранитель: принято 2" in rep, rep)
        check("после flush сборщик пуст",
              "накоплено 0 из 5" in ws.collector_status())
        with open(keep, encoding="utf-8") as f:
            table2 = f.read()
        check("таблица перезаписана (2 записи)",
              "записей: 2" in table2, table2.splitlines()[2] if table2 else "")
        check("keeper_report читает файл",
              "ХРАНИТЕЛЬ ПОГОДЫ" in ws.keeper_report())

        # --------------------------------------------------------------
        section("4. Пустые случаи")
        ws._save_collector([])
        check("flush пустого сборщика — сообщение",
              "пуст" in ws.collector_flush().lower())
    finally:
        ws._find_city = real_find
        ws._get_json = real_get
        for p in (coll, keep):
            try:
                os.remove(p)
            except OSError:
                pass
        os.environ.pop("WEATHER_COLLECTOR_FILE", None)
        os.environ.pop("WEATHER_KEEPER_FILE", None)

    sys.exit(finish())


if __name__ == "__main__":
    main()
