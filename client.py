"""Минимальный MCP-клиент (stdio).

Шаги:
  1. Запускаем MCP-сервер как подпроцесс по stdio.
  2. Устанавливаем MCP-соединение и инициализируем сессию.
  3. Запрашиваем список инструментов через list_tools().
  4. Выводим список на экран.

По умолчанию запускается сервер Яндекс.Календаря. Можно указать другой
MCP-сервер проекта первым аргументом командной строки:
    python client.py                       # yandex_calendar_server.py
    python client.py currency_server.py    # MCP-сервер «Конвертер»
"""

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Чтобы кириллица корректно печаталась в консоли Windows.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# Какой MCP-сервер проекта смотреть: из аргумента CLI или по умолчанию —
# Яндекс.Календарь.
SERVER_FILE = sys.argv[1] if len(sys.argv) > 1 else "yandex_calendar_server.py"


# Описание того, как запустить наш MCP-сервер.
SERVER_PARAMS = StdioServerParameters(
    command=sys.executable,          # текущий интерпретатор Python
    args=[SERVER_FILE],              # файл MCP-сервера проекта
)


async def main() -> None:
    # stdio_client сам запускает сервер и отдаёт пару потоков чтения/записи.
    async with stdio_client(SERVER_PARAMS) as (read, write):
        # Открываем MCP-сессию поверх потоков и инициализируем её.
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("[OK] MCP-соединение установлено\n")

            # Запрашиваем у сервера список доступных инструментов.
            result = await session.list_tools()

            tools = result.tools
            print(f"[OK] Сервер вернул инструментов: {len(tools)}\n")
            print("Доступные инструменты:")
            print("-" * 40)
            for tool in tools:
                print(f"  • {tool.name}")
                if tool.description:
                    print(f"      {tool.description}")
                schema = tool.input_schema or {}
                props = schema.get("properties", {})
                if props:
                    args = ", ".join(props.keys())
                    print(f"      параметры: {args}")
            print("-" * 40)

            # Проверка: список должен быть непустым.
            assert tools, "Сервер вернул пустой список инструментов!"
            print("\n[OK] Проверка пройдена: инструменты корректно получены")


if __name__ == "__main__":
    asyncio.run(main())

