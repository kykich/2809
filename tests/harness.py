# -*- coding: utf-8 -*-
"""Общая обвязка автономных тестов проекта (единый формат вывода).

Единая точка для всех check_*.py / test_*.py: подсчёт проверок, печать
[OK]/[FAIL], секции и итог. Позволяет не дублировать один и тот же код
(PASS/FAIL-счётчики, функция check, завершение с exit code) в каждом тесте.

Использование:
    from tests.harness import check, section, finish

    section("1. Что-то")
    check("условие выполнено", 2 + 2 == 4)
    check("с пояснением", x == y, "ожидалось %r" % y)
    ...
    sys.exit(finish())

Формат вывода совместим с разбором на странице (web/server.py парсит
«== секция ==», «[OK] …»/«[FAIL] …» и строку «Итог: N OK, M FAIL»).
"""
import sys

__all__ = ["check", "section", "finish", "reset", "passed", "failed",
           "ensure_utf8"]

# Счётчики проверок за прогон (на уровне модуля — один тест = один процесс).
_PASS = 0
_FAIL = 0


def ensure_utf8():
    """Принудительно включает UTF-8 для stdout/stderr.

    Нужно на Windows: иначе русский текст печатается в cp1251 и при захвате
    вывода как UTF-8 (например, при запуске теста со страницы через
    subprocess) получаются «кракозябры».
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def reset():
    """Сбрасывает счётчики проверок (для повторных прогонов в одном процессе)."""
    global _PASS, _FAIL
    _PASS = 0
    _FAIL = 0


def section(name):
    """Печатает заголовок секции в формате, который парсит сервер."""
    print("\n== %s ==" % name)


def check(name, cond, detail=""):
    """Регистрирует одну проверку.

    name   — текст проверки;
    cond   — истинность (truthy) = успех;
    detail — пояснение, показывается при провале (необязательно).
    """
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [OK]   %s" % name)
    else:
        _FAIL += 1
        suffix = ("- " + str(detail)) if detail else ""
        print("  [FAIL] %s  %s" % (name, suffix))


def passed():
    """Сколько проверок пройдено за прогон."""
    return _PASS


def failed():
    """Сколько проверок провалено за прогон."""
    return _FAIL


def finish(title=None):
    """Печатает итог и возвращает код выхода (0 — всё ок, иначе 1)."""
    if title:
        print("\n=== %s ===" % title)
    print("\nИтог: %d OK, %d FAIL" % (_PASS, _FAIL))
    return 0 if _FAIL == 0 else 1
