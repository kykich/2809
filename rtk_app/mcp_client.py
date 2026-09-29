"""MCP-клиент проекта: подключение к MCP-серверу, список инструментов, статус.

Модуль инкапсулирует работу с MCP (Model Context Protocol) над stdio-транспортом:
запускает MCP-сервер как подпроцесс, устанавливает соединение, умеет:
  * получать список доступных инструментов (list_tools);
  * вызывать инструмент по имени (call_tool);
  * проверять СТАТУС сервера (успешное соединение + число инструментов).

Внешний код (HTTP-сервер, агент) работает через СИНХРОННУЮ обёртку: под капотом
используется asyncio, а наружу отдаётся обычный dict — так удобнее вызывать из
потокового HTTP-обработчика.

Конфигурация берётся из rtk_app.config (MCP_*).
"""

import asyncio
import os
import sys

from . import config

__all__ = ["mcp_list_tools", "mcp_call_tool", "mcp_status",
           "mcp_servers", "mcp_server_args"]


def mcp_servers():
    """Возвращает список доступных MCP-серверов для выбора в интерфейсе.

    Каждый элемент: {id, label, args}. Источник — config.MCP_SERVERS.
    """
    servers = getattr(config, "MCP_SERVERS", None) or []
    out = []
    for s in servers:
        if not isinstance(s, dict):
            continue
        out.append({
            "id": s.get("id", ""),
            "label": s.get("label", s.get("id", "")),
            "args": list(s.get("args", []) or []),
        })
    # Если списка нет — отдаём единственный сервер по умолчанию.
    if not out:
        out.append({
            "id": "default",
            "label": "по умолчанию",
            "args": list(getattr(config, "MCP_SERVER_ARGS", []) or
                         ["test_server.py"]),
        })
    return out


def mcp_server_args(server_id=None):
    """Аргументы запуска MCP-сервера по его id.

    server_id=None или неизвестный id -> аргументы по умолчанию
    (config.MCP_SERVER_ARGS).
    """
    if server_id:
        for s in mcp_servers():
            if s["id"] == server_id:
                return s["args"]
    return list(getattr(config, "MCP_SERVER_ARGS", []) or ["test_server.py"])


def _server_params(server_id=None):
    """Собирает параметры запуска MCP-сервера (stdio).

    Запускается выбранный сервер (server_id) текущим интерпретатором Python.
    По умолчанию — сервер из config.MCP_SERVER_ARGS. Команду можно
    переопределить через config.MCP_SERVER_CMD.
    """
    cmd = getattr(config, "MCP_SERVER_CMD", "") or sys.executable
    args = mcp_server_args(server_id)
    env = dict(os.environ)
    # Гарантируем UTF-8 в обмене (кириллица в описаниях инструментов).
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return cmd, args, env


async def _with_session(action, server_id=None):
    """Универсальный помощник: поднимает MCP-сессию и выполняет action(session).

    server_id — id выбранного MCP-сервера (см. mcp_servers());
    None — сервер по умолчанию (config.MCP_SERVER_ARGS).
    Возвращает то, что вернул action. Любые ошибки соединения всплывают
    наружу — вызывающий код превращает их в статус/сообщение об ошибке.
    """
    # Импорт внутри функции: mcp — опциональная зависимость, сервер проекта
    # должен запускаться даже без неё (тогда статус MCP = «не установлен»).
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    cmd, args, env = _server_params(server_id)
    params = StdioServerParameters(command=cmd, args=args, env=env)

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await action(session)


def _run(coro_factory, timeout=None):
    """Запускает корутину в отдельном событийном цикле (синхронная обёртка)."""
    timeout = timeout or getattr(config, "MCP_TIMEOUT", 30)

    async def _wrapped():
        return await asyncio.wait_for(coro_factory(), timeout=timeout)

    try:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_wrapped())
        finally:
            loop.close()
    except asyncio.TimeoutError:
        raise TimeoutError("MCP: превышено время ожидания (%s c)" % timeout)


def mcp_list_tools(timeout=None, server_id=None):
    """Возвращает список инструментов MCP-сервера.

    server_id — id выбранного MCP-сервера (None — по умолчанию).
    Результат — dict:
        ok      — удалось ли подключиться и получить список;
        tools   — [{name, description, params}] (при ok=True);
        error   — текст ошибки (при ok=False).
    """

    async def action(session):
        result = await session.list_tools()
        tools = []
        for tool in result.tools:
            schema = tool.input_schema or {}
            props = schema.get("properties", {}) or {}
            tools.append({
                "name": tool.name,
                "description": tool.description or "",
                "params": list(props.keys()),
            })
        return tools

    try:
        tools = _run(lambda: _with_session(action, server_id), timeout=timeout)
        return {"ok": True, "tools": tools, "error": None}
    except Exception as exc:
        return {"ok": False, "tools": [], "error": str(exc)}


def mcp_call_tool(name, arguments, timeout=None, server_id=None):
    """Вызывает инструмент MCP-сервера по имени.

    name — имя инструмента; arguments — dict с аргументами.
    server_id — id выбранного MCP-сервера (None — по умолчанию).
    Результат — dict:
        ok        — вызов успешен (и инструмент не сообщил об ошибке);
        text      — текстовый результат (склеенный из контента);
        is_error  — инструмент вернул признак ошибки;
        error     — текст ошибки (если не удалось вызвать);
    """

    async def action(session):
        res = await session.call_tool(name, arguments or {})
        parts = []
        for item in (res.content or []):
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(text)
        # В SDK поле называется is_error (snake_case); на всякий случай
        # поддерживаем и isError (старые версии).
        is_err = getattr(res, "is_error", None)
        if is_err is None:
            is_err = getattr(res, "isError", False)
        return {"is_error": bool(is_err), "text": "\n".join(parts)}

    try:
        res = _run(lambda: _with_session(action, server_id), timeout=timeout)
        return {"ok": not res["is_error"], "text": res["text"],
                "is_error": res["is_error"], "error": None}
    except Exception as exc:
        return {"ok": False, "text": "", "is_error": True, "error": str(exc)}


def mcp_status(timeout=None, server_id=None):
    """Проверяет СТАТУС MCP-сервера (для кнопки «Проверить статус»).

    server_id — id выбранного MCP-сервера (None — по умолчанию).
    Пытается установить соединение и получить список инструментов.
    Результат — dict:
        ok         — сервер доступен и отвечает;
        connected  — установлено ли соединение;
        tools_count— число доступных инструментов;
        tools      — список имён инструментов;
        server     — команда запуска сервера (для диагностики);
        error      — текст ошибки (при ok=False).
    """
    cmd, args, _env = _server_params(server_id)
    server_desc = " ".join([os.path.basename(cmd)] + list(args))
    res = mcp_list_tools(timeout=timeout, server_id=server_id)
    if not res.get("ok"):
        return {
            "ok": False,
            "connected": False,
            "tools_count": 0,
            "tools": [],
            "server": server_desc,
            "error": res.get("error") or "не удалось подключиться",
        }
    tools = res.get("tools", [])
    return {
        "ok": True,
        "connected": True,
        "tools_count": len(tools),
        "tools": [t.get("name") for t in tools],
        "server": server_desc,
        "error": None,
    }
