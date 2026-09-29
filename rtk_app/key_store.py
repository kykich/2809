"""
Чтение API-ключа DeepSeek из текстового файла.
"""
from pathlib import Path

from . import config


def read_api_key(filename=None):
    """Читает и возвращает API-ключ из файла (по умолчанию KEY_FILE).

    Если файл не найден — выбрасывает FileNotFoundError.
    """
    path = Path(filename or config.KEY_FILE)
    if not path.exists():
        raise FileNotFoundError(
            f"Файл {path} не найден. Создайте его и вставьте туда ваш API-ключ."
        )
    return path.read_text(encoding="utf-8").strip()
