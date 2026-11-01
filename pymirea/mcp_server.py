"""MCP-сервер поверх pymirea.

Даёт LLM-клиентам (Claude Desktop/Code, Cursor и т.д.) read-only доступ к
Пульсу МИРЭА: своё расписание, баллы БРС, посещаемость, турникеты, а также
публичное расписание групп/преподавателей/аудиторий.

Принципы (как в PowerTronin/mirea-mcp для СДО):

* пароль и OTP через MCP не передаются — вход делается один раз в терминале
  командой ``pymirea login``, сервер читает зашифрованную сессию с диска;
* все инструменты только читают данные; отметка посещаемости по QR
  намеренно доступна только в CLI (``pymirea attend``).

Запуск::

    pip install "pymirea[mcp]"
    pymirea login
    pymirea mcp            # или pymirea-mcp
"""

from __future__ import annotations

import functools
from typing import Any, Awaitable, Callable

from . import app as A
from .store import NotLoggedIn, SessionStore

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - mcp 1.x
    try:
        from mcp.server.fastmcp import FastMCP as _Server
        from mcp.server.fastmcp.exceptions import ToolError
    except ImportError as e:  # pragma: no cover
        raise ImportError('MCP-сервер требует пакет mcp: pip install "pymirea[mcp]"') from e

from mcp.types import ToolAnnotations

INSTRUCTIONS = (
    "Доступ к Пульсу МИРЭА (pulse.mirea.ru) и публичному расписанию РТУ МИРЭА. "
    "Время — московское (UTC+3). Если инструмент сообщает, что сессии нет или она истекла, "
    "попроси пользователя выполнить в терминале `pymirea login` — пароль в чат не запрашивай. "
    "Для расписания чужой группы/преподавателя/аудитории используй group_timetable, "
    "для своего — my_schedule."
)

_READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True)


def _guard(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except NotLoggedIn as e:
            raise ToolError(f"{e} (вход выполняется пользователем в терминале)") from e
        except A.MireaAppError as e:
            raise ToolError(str(e)) from e

    return wrapper


def build_server(store: SessionStore | None = None) -> Any:
    store = store or SessionStore()
    mcp = _Server(name="pymirea", instructions=INSTRUCTIONS)

    def tool(fn):
        return mcp.tool(annotations=_READ_ONLY)(_guard(fn))

    @tool
    async def session_status(check: bool = False) -> dict:
        """Состояние локальной сессии Пульса: залогинен ли пользователь, когда истекает токен.
        check=true дополнительно проверяет сессию запросом к pulse.mirea.ru."""
        return await A.status(store, check=check)

    @tool
    async def my_schedule(days: int = 7) -> dict:
        """Личное расписание студента из Пульса на ближайшие `days` дней (1-31), начиная с сегодня."""
        return await A.my_schedule(store, days=days)

    @tool
    async def my_grades() -> dict:
        """Баллы БРС по всем предметам текущего семестра: текущий контроль (из 40),
        семестровый (из 30), посещения (из 30), достижения, дополнительные, итог (из 120).
        attendance_max_possible — достижимый максимум за посещения, если известен."""
        return await A.grades(store)

    @tool
    async def my_attendance(discipline: str) -> dict:
        """Посещаемость по одной дисциплине: сводка и статус каждого занятия.
        `discipline` — discipline_id из my_grades или часть названия предмета."""
        return await A.attendance(store, discipline)

    @tool
    async def my_turnstile_events() -> dict:
        """Проходы через турникеты (СКУД) за сегодня: время, зоны входа/выхода, длительность."""
        return await A.acs_today(store)

    @tool
    async def search_timetable(kind: str, query: str) -> list[dict]:
        """Поиск в публичном расписании. kind: group | teacher | classroom.
        Возвращает список совпадений с uid."""
        return await A.search(store, kind, query)

    @tool
    async def group_timetable(kind: str, query: str, days: int = 7, date_from: str | None = None) -> dict:
        """Публичное расписание группы, преподавателя или аудитории (вход не нужен).
        kind: group | teacher | classroom; query — название (например «ЭФБО-01-24», «Иванов И.И.», «А-101»).
        days — сколько дней показать начиная с date_from (YYYY-MM-DD, по умолчанию сегодня); 0 — всё расписание."""
        start = None
        if date_from:
            from datetime import datetime

            try:
                start = datetime.fromisoformat(date_from).replace(tzinfo=A.MSK)
            except ValueError as e:
                raise ToolError("date_from должен быть в формате YYYY-MM-DD") from e
        return await A.entity_schedule(store, kind, query, days=days or None, date_from=start)

    return mcp


def run(store: SessionStore | None = None, *, transport: str = "stdio", host: str = "127.0.0.1", port: int = 8000) -> None:
    server = build_server(store)
    if transport == "stdio":
        server.run("stdio")
        return
    try:
        server.run(transport, host=host, port=port)
    except TypeError:  # pragma: no cover - mcp 1.x: host/port в settings
        server.settings.host = host
        server.settings.port = port
        server.run(transport)


def main() -> None:
    import argparse

    p = argparse.ArgumentParser(prog="pymirea-mcp", description="MCP-сервер для Пульса МИРЭА")
    p.add_argument("--home", help="каталог хранилища сессии")
    p.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = p.parse_args()
    run(SessionStore(args.home), transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
