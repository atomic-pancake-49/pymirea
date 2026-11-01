"""СДО МИРЭА (Moodle): bootstrap сессии через SSO и чтение через AJAX. Сеть — respx."""

from __future__ import annotations

import base64
import secrets

import httpx
import pytest
import respx

from pymirea import Config, configure
from pymirea.sdo import SDO_URL, MireaSDO


def _setup():
    configure(Config(session_keys=base64.b64encode(secrets.token_bytes(32)).decode()))


def _page_with_sesskey(sesskey="SK123456"):
    return f'<html><script>M.cfg = {{"wwwroot":"{SDO_URL}","sesskey":"{sesskey}"}};</script></html>'


def _ajax_route(method_to_data: dict):
    def handler(request):
        info = request.url.params.get("info")
        if info in method_to_data:
            return httpx.Response(200, json=[{"error": False, "data": method_to_data[info]}])
        return httpx.Response(200, json=[{"error": True, "exception": {"message": "unknown"}}])

    return handler


@pytest.mark.asyncio
@respx.mock
async def test_courses_and_deadlines_via_ajax():
    _setup()
    respx.get(f"{SDO_URL}/my/").mock(return_value=httpx.Response(200, text=_page_with_sesskey()))
    respx.post(f"{SDO_URL}/lib/ajax/service.php").mock(
        side_effect=_ajax_route(
            {
                "core_course_get_enrolled_courses_by_timeline_classification": {
                    "courses": [{"id": 42, "fullname": "Физика", "viewurl": f"{SDO_URL}/course/view.php?id=42"}]
                },
                "core_calendar_get_action_events_by_timesort": {
                    "events": [
                        {
                            "name": "Сдать лабу 3",
                            "timesort": 1920000000,
                            "modulename": "assign",
                            "course": {"fullname": "Физика"},
                            "url": f"{SDO_URL}/mod/assign/view.php?id=7",
                        }
                    ]
                },
            }
        )
    )
    sdo = MireaSDO({"MoodleSession": "already-have-it"})
    try:
        courses = await sdo.get_courses()
        deadlines = await sdo.get_deadlines()
    finally:
        await sdo.close()

    assert courses.success and courses.courses[0].name == "Физика" and courses.courses[0].id == 42
    assert deadlines.success
    d = deadlines.deadlines[0]
    assert d.name == "Сдать лабу 3" and d.course == "Физика" and d.activity == "assign" and d.timestamp == 1920000000


@pytest.mark.asyncio
@respx.mock
async def test_bootstrap_session_via_sso():
    _setup()
    # /auth/oidc/ сразу отдаёт MoodleSession и приземляет на /my/ (живая KC-сессия).
    respx.get(f"{SDO_URL}/auth/oidc/").mock(
        return_value=httpx.Response(
            200, headers={"set-cookie": "MoodleSession=boot123; Path=/"}, text="<html>dashboard</html>",
            request=httpx.Request("GET", f"{SDO_URL}/my/"),
        )
    )
    sdo = MireaSDO({"KEYCLOAK_IDENTITY": "kc", "KEYCLOAK_SESSION": "kcs"})
    try:
        ok = await sdo.bootstrap_session()
    finally:
        await sdo.close()
    assert ok
    assert sdo.session_cookies["__sdo_moodle_session"] == "boot123"


@pytest.mark.asyncio
@respx.mock
async def test_bootstrap_fails_when_redirected_to_login():
    _setup()
    respx.get(f"{SDO_URL}/auth/oidc/").mock(
        return_value=httpx.Response(200, text="login", request=httpx.Request("GET", "https://sso.mirea.ru/realms/mirea/login-actions/authenticate"))
    )
    sdo = MireaSDO({})
    try:
        assert await sdo.bootstrap_session() is False
    finally:
        await sdo.close()


@pytest.mark.asyncio
@respx.mock
async def test_no_session_tries_bootstrap_then_fails_cleanly():
    _setup()
    respx.get(f"{SDO_URL}/auth/oidc/").mock(
        return_value=httpx.Response(200, text="x", request=httpx.Request("GET", f"{SDO_URL}/login/index.php"))
    )
    sdo = MireaSDO({})
    try:
        res = await sdo.get_courses()
    finally:
        await sdo.close()
    assert not res.success and "Сессия СДО" in res.message


@pytest.mark.asyncio
@respx.mock
async def test_grades_join_course_names():
    _setup()
    respx.get(f"{SDO_URL}/my/").mock(return_value=httpx.Response(200, text=_page_with_sesskey()))
    respx.post(f"{SDO_URL}/lib/ajax/service.php").mock(
        side_effect=_ajax_route(
            {
                "gradereport_overview_get_course_grades": {"grades": [{"courseid": 42, "grade": "85,00"}]},
                "core_course_get_enrolled_courses_by_timeline_classification": {
                    "courses": [{"id": 42, "fullname": "Физика"}]
                },
            }
        )
    )
    sdo = MireaSDO({"MoodleSession": "x"})
    try:
        res = await sdo.get_grades()
    finally:
        await sdo.close()
    assert res.success and res.grades[0].course == "Физика" and res.grades[0].grade == "85,00"
