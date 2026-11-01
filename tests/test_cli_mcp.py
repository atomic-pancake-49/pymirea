"""Тесты CLI, локального хранилища сессии и MCP-сервера.

Сеть мокается через respx; реальная МИРЭА не дёргается."""

from __future__ import annotations

import json
import os
import stat

import httpx
import pytest
import respx

from pymirea import _settings
from pymirea import app as A
from pymirea.cli import main
from pymirea.schedule import SCHEDULE_SUPABASE_URL
from pymirea.store import NotLoggedIn, SessionStore

RPC = f"{SCHEDULE_SUPABASE_URL}/rest/v1/rpc"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMIREA_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("PYMIREA_SESSION_KEYS", raising=False)
    prev = _settings._cfg
    _settings._cfg = None
    yield
    _settings._cfg = prev


def _mock_group_api(router):
    router.post(f"{RPC}/search_schedule_targets").mock(
        return_value=httpx.Response(200, json=[{"external_id": "5012", "target_title": "ЭФБО-01-24", "full_title": "ЭФБО-01-24"}])
    )
    router.post(f"{RPC}/get_schedule_for_entity").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "type": "__lesson_schedule__",
                    "subject": "Физика",
                    "lesson_type": "lecture",
                    "dates": ["10-01-2030"],
                    "lesson_bells": {"start_time": "09:00", "end_time": "10:30"},
                    "classrooms": [{"name": "А-9"}],
                    "groups": ["ЭФБО-01-24"],
                }
            ],
        )
    )


# --- store ---


def test_store_roundtrip_and_permissions(tmp_path):
    store = SessionStore(tmp_path / "s")
    store.configure()
    with pytest.raises(NotLoggedIn):
        store.load()
    store.save({"a": "1"}, login="me@edu.mirea.ru")
    loaded = store.load()
    assert loaded.cookies == {"a": "1"}
    assert loaded.login == "me@edu.mirea.ru"
    # пароль/cookies не лежат в открытом виде
    assert "me@edu" in store.session_path.read_text()
    assert '"a"' not in json.loads(store.session_path.read_text())["blob"]
    if os.name == "posix":
        assert stat.S_IMODE(store.key_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(store.session_path.stat().st_mode) == 0o600
    # повторный save без login сохраняет прежний login
    store.save({"a": "2"})
    assert store.load().login == "me@edu.mirea.ru"
    assert store.clear() is True
    assert store.clear() is False


def test_store_env_key_overrides_file(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMIREA_SESSION_KEYS", "x" * 44)
    store = SessionStore(tmp_path / "s")
    assert store.session_keys() == "x" * 44
    assert not store.key_path.exists()


def test_store_wrong_key_is_not_logged_in(tmp_path):
    store = SessionStore(tmp_path / "s")
    store.save({"a": "1"})
    store.key_path.write_text("Y2hhbmdlZC1rZXktY2hhbmdlZC1rZXktY2hhbmdlZC0=\n")
    with pytest.raises(NotLoggedIn):
        store.load()


@pytest.mark.asyncio
async def test_session_ctx_persists_mutations(tmp_path):
    store = SessionStore(tmp_path / "s")
    store.save({"a": "1"}, login="l")
    async with A.session(store) as cookies:
        cookies["__brs_visiting_log_id"] = "x"
    assert store.load().cookies["__brs_visiting_log_id"] == "x"


# --- CLI ---


def test_cli_status_not_logged_in(capsys):
    assert main(["status", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["logged_in"] is False


def test_cli_grades_requires_login(capsys):
    assert main(["grades"]) == 2
    assert "pymirea login" in capsys.readouterr().err


@respx.mock
def test_cli_timetable_json(capsys):
    _mock_group_api(respx)
    rc = main(["timetable", "group", "ЭФБО-01-24", "--date", "2030-01-10", "-d", "1", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["name"] == "ЭФБО-01-24"
    assert [e["summary"] for e in data["events"]] == ["Физика (lecture)"]


@respx.mock
def test_cli_timetable_ics(tmp_path, capsys):
    _mock_group_api(respx)
    out = tmp_path / "g.ics"
    assert main(["timetable", "group", "ЭФБО-01-24", "--ics", str(out)]) == 0
    text = out.read_text()
    assert "BEGIN:VCALENDAR" in text and "SUMMARY:Физика (lecture)" in text


@respx.mock
def test_cli_search_not_found(capsys):
    respx.post(f"{RPC}/search_schedule_targets").mock(return_value=httpx.Response(200, json=[]))
    assert main(["timetable", "group", "НЕТ-00-00"]) == 1
    assert "ничего не найдено" in capsys.readouterr().err


# --- MCP ---

mcp_server = pytest.importorskip("pymirea.mcp_server")


def _text(result) -> str:
    content = getattr(result, "content", None)
    if content is None:  # mcp 1.x: (content, structured) или list
        content = result[0] if isinstance(result, tuple) else result
    return "".join(getattr(c, "text", "") for c in content)


@pytest.mark.asyncio
async def test_mcp_tools_are_read_only(tmp_path):
    server = mcp_server.build_server(SessionStore(tmp_path / "s"))
    tools = await server.list_tools()
    names = {t.name for t in tools}
    assert {"my_schedule", "my_grades", "my_attendance", "group_timetable", "session_status"} <= names
    # отметка посещаемости в MCP не экспортируется
    assert not any("mark" in n or "attend" == n for n in names)
    for t in tools:
        ann = t.annotations
        assert ann is not None
        assert getattr(ann, "read_only_hint", None) or getattr(ann, "readOnlyHint", None)


@pytest.mark.asyncio
async def test_mcp_not_logged_in_is_tool_error(tmp_path):
    server = mcp_server.build_server(SessionStore(tmp_path / "s"))
    with pytest.raises(mcp_server.ToolError, match="pymirea login"):
        await server.call_tool("my_grades", {})


@pytest.mark.asyncio
@respx.mock
async def test_mcp_group_timetable(tmp_path):
    _mock_group_api(respx)
    server = mcp_server.build_server(SessionStore(tmp_path / "s"))
    result = await server.call_tool(
        "group_timetable", {"kind": "group", "query": "ЭФБО-01-24", "days": 1, "date_from": "2030-01-10"}
    )
    assert "Физика (lecture)" in _text(result)
