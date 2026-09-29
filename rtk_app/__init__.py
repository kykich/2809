"""
Пакет rtk_app — логика веб-приложения чата с DeepSeek.

Содержит модули:
  config    — настройки и константы;
  key_store — чтение API-ключа;
  deepseek  — клиент DeepSeek API;
  gigachat  — клиент GigaChat API;
  html_report — построение HTML-ответа;
  agent     — АГЕНТ (отдельная сущность), инкапсулирующий логику запросов к LLM.
"""

__all__ = [
    "config",
    "key_store",
    "deepseek",
    "gigachat",
    "html_report",
    "agent",
]

__version__ = "2.0.0"
