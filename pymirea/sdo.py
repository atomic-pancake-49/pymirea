"""Клиент СДО МИРЭА (online-edu.mirea.ru — Moodle).

СДО логинится через тот же Keycloak SSO (`sso.mirea.ru`), что и Пульс, —
через OIDC-плагин Moodle (`/auth/oidc/`). Поэтому отдельный логин не нужен:
имея KEYCLOAK_IDENTITY/KEYCLOAK_SESSION из обычного `MireaAuth.login`, можно
«молча» обменять их на cookie `MoodleSession` (:meth:`bootstrap_session`).

Данные читаются через штатный Moodle AJAX (`/lib/ajax/service.php`): он
отдаёт JSON любому залогиненному пользователю и не требует токена
веб-сервисов (его студентам в МИРЭА не выдают). Методы Moodle-core:
курсы, ближайшие дедлайны, оценки.

ВАЖНО: это reverse-engineering, как и остальной pymirea. Имена AJAX-методов
и флоу OIDC могут отличаться на конкретной инсталляции Moodle — парсеры не
кидают исключений на неожиданных данных, а возвращают пустое/частичное.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from ._http import make_async_client
from ._settings import settings

logger = logging.getLogger(__name__)

SDO_URL = "https://online-edu.mirea.ru"
_SESSKEY_RE = re.compile(r'"sesskey":"([^"]{6,64})"')
_MOODLE_SESSION_COOKIE = "MoodleSession"
# Имя закешированного MoodleSession во внутреннем session-dict (не отправляется как HTTP-cookie).
_CACHE_KEY = "__sdo_moodle_session"


@dataclass
class SdoCourse:
    id: int
    name: str
    url: str = ""


@dataclass
class SdoDeadline:
    name: str
    course: str
    timestamp: int | None = None
    url: str = ""
    activity: str = ""  # assign | quiz | ...


@dataclass
class SdoGrade:
    course: str
    grade: str | None = None  # форматированная итоговая оценка курса, как её отдаёт Moodle


@dataclass
class SdoResult:
    success: bool
    message: str
    courses: list[SdoCourse] | None = None
    deadlines: list[SdoDeadline] | None = None
    grades: list[SdoGrade] | None = None


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class MireaSDO:
    """Чтение СДО МИРЭА по cookie-сессии.

    ``session_cookies`` — тот же dict, что у Пульса. Если в нём уже есть
    ``MoodleSession`` (или закешированный ключ), используется он; иначе
    :meth:`bootstrap_session` пытается получить его через SSO.
    """

    def __init__(self, session_cookies: dict):
        self.session_cookies = session_cookies or {}
        cookies = httpx.Cookies()
        for name, value in self.session_cookies.items():
            if not value or str(name).startswith("__"):
                continue
            if name in {"access_token", "token_type", "refresh_token", "expires_in"}:
                continue
            cookies.set(str(name), str(value), domain=".mirea.ru")
        cached = (self.session_cookies.get(_CACHE_KEY) or "").strip()
        if cached:
            cookies.set(_MOODLE_SESSION_COOKIE, cached, domain="online-edu.mirea.ru")

        self.client = make_async_client(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
            cookies=cookies,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                    "(KHTML, like Gecko) Version/16.4 Safari/605.1.15"
                ),
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            },
            transport=httpx.AsyncHTTPTransport(retries=2),
            proxy=settings.mirea_proxy if settings.mirea_proxy else None,
        )
        self._sesskey: str | None = None

    async def close(self) -> None:
        await self.client.aclose()

    # --- авторизация ---

    def _store_moodle_session(self) -> None:
        for cookie in self.client.cookies.jar:
            if cookie.name == _MOODLE_SESSION_COOKIE and cookie.value:
                self.session_cookies[_CACHE_KEY] = cookie.value
                return

    async def bootstrap_session(self) -> bool:
        """Получить ``MoodleSession`` молча через SSO (OIDC-плагин Moodle).

        Работает, если в сессии ещё жив KEYCLOAK_IDENTITY/KEYCLOAK_SESSION
        (т.е. пользователь недавно логинился). Иначе вернёт False — нужен
        повторный ``MireaAuth.login``.
        """
        try:
            # /auth/oidc/ → sso.mirea.ru authorize → (есть KC-сессия) → назад с code → MoodleSession.
            resp = await self.client.get(f"{SDO_URL}/auth/oidc/", follow_redirects=True)
        except (httpx.NetworkError, httpx.TimeoutException, httpx.HTTPError) as e:
            logger.debug("SDO bootstrap network error: %s", type(e).__name__)
            return False
        # Если нас оставили на форме входа Keycloak — тихий SSO не удался.
        final_host = urlparse(str(resp.url)).hostname or ""
        if final_host == "sso.mirea.ru" or "/login" in str(resp.url).lower():
            logger.debug("SDO bootstrap landed on login page (KC session expired)")
            return False
        self._store_moodle_session()
        ok = bool(self.session_cookies.get(_CACHE_KEY)) or self._has_moodle_cookie()
        return ok

    def _has_moodle_cookie(self) -> bool:
        return any(c.name == _MOODLE_SESSION_COOKIE and c.value for c in self.client.cookies.jar)

    async def _ensure_session(self) -> tuple[bool, str | None]:
        if self.session_cookies.get(_CACHE_KEY) or self._has_moodle_cookie():
            return True, None
        if await self.bootstrap_session():
            return True, None
        return False, "Сессия СДО не найдена. Перелогиньтесь в МИРЭА."

    async def _get_sesskey(self) -> str | None:
        """sesskey нужен для AJAX; берётся из M.cfg на странице /my/."""
        if self._sesskey:
            return self._sesskey
        try:
            resp = await self.client.get(f"{SDO_URL}/my/")
        except (httpx.NetworkError, httpx.TimeoutException, httpx.HTTPError):
            return None
        # Если редиректнуло на вход — сессия протухла.
        if "/login" in str(resp.url).lower() or "/auth/oidc" in str(resp.url).lower():
            return None
        self._store_moodle_session()
        m = _SESSKEY_RE.search(resp.text or "")
        self._sesskey = m.group(1) if m else None
        return self._sesskey

    # --- AJAX ---

    async def _ajax(self, methodname: str, args: dict) -> object | None:
        sesskey = await self._get_sesskey()
        if not sesskey:
            return None
        try:
            resp = await self.client.post(
                f"{SDO_URL}/lib/ajax/service.php",
                params={"sesskey": sesskey, "info": methodname},
                json=[{"index": 0, "methodname": methodname, "args": args}],
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
        except (httpx.NetworkError, httpx.TimeoutException, httpx.HTTPError):
            return None
        if resp.status_code != 200:
            return None
        try:
            body = resp.json()
        except ValueError:
            return None
        if not isinstance(body, list) or not body:
            return None
        entry = body[0]
        if not isinstance(entry, dict) or entry.get("error"):
            logger.debug("SDO ajax %s error: %s", methodname, str(entry)[:200] if isinstance(entry, dict) else entry)
            return None
        return entry.get("data")

    # --- данные ---

    async def get_courses(self) -> SdoResult:
        ok, err = await self._ensure_session()
        if not ok:
            return SdoResult(success=False, message=err or "Нет сессии СДО")
        data = await self._ajax(
            "core_course_get_enrolled_courses_by_timeline_classification",
            {"offset": 0, "limit": 0, "classification": "all", "sort": "fullname"},
        )
        courses: list[SdoCourse] = []
        items = data.get("courses") if isinstance(data, dict) else None
        for it in items or []:
            if not isinstance(it, dict):
                continue
            cid = _int(it.get("id"))
            name = (it.get("fullname") or it.get("shortname") or "").strip()
            if cid and name:
                courses.append(SdoCourse(id=cid, name=name, url=str(it.get("viewurl") or "")))
        if not courses and items is None:
            return SdoResult(success=False, message="Не удалось получить курсы (проверьте сессию СДО)")
        return SdoResult(success=True, message="OK", courses=courses)

    async def get_deadlines(self, *, limit: int = 50) -> SdoResult:
        ok, err = await self._ensure_session()
        if not ok:
            return SdoResult(success=False, message=err or "Нет сессии СДО")
        # Сигнатура Moodle: timesortfrom + limitnum (НЕ limit), как в блоке Timeline.
        data = await self._ajax(
            "core_calendar_get_action_events_by_timesort",
            {
                "timesortfrom": int(time.time()),
                "limitnum": max(1, min(int(limit), 200)),
                "limittononsuspendedevents": True,
            },
        )
        events = data.get("events") if isinstance(data, dict) else None
        deadlines: list[SdoDeadline] = []
        for ev in events or []:
            if not isinstance(ev, dict):
                continue
            name = (ev.get("name") or "").strip()
            if not name:
                continue
            course = ""
            c = ev.get("course")
            if isinstance(c, dict):
                course = (c.get("fullname") or c.get("shortname") or "").strip()
            modname = (ev.get("modulename") or "").strip()
            url = str(ev.get("url") or (ev.get("action") or {}).get("url") or "")
            deadlines.append(
                SdoDeadline(name=name, course=course, timestamp=_int(ev.get("timesort")), url=url, activity=modname)
            )
        if not deadlines and events is None:
            return SdoResult(success=False, message="Не удалось получить дедлайны (проверьте сессию СДО)")
        return SdoResult(success=True, message="OK", deadlines=deadlines)

    async def get_grades(self, *, course_names: dict[int, str] | None = None) -> SdoResult:
        """Оценки по курсам. ``course_names`` (id→имя) можно передать заранее,
        чтобы не запрашивать список курсов повторно (overview отдаёт только courseid)."""
        ok, err = await self._ensure_session()
        if not ok:
            return SdoResult(success=False, message=err or "Нет сессии СДО")
        data = await self._ajax("gradereport_overview_get_course_grades", {})
        rows = data.get("grades") if isinstance(data, dict) else None
        # Имена курсов: берём переданные, иначе (fallback) один раз тянем список курсов.
        names: dict[int, str] = dict(course_names or {})
        if rows and not names:
            courses = await self.get_courses()
            names = {c.id: c.name for c in (courses.courses or [])}
        grades: list[SdoGrade] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            cid = _int(row.get("courseid"))
            course = names.get(cid or -1, "") or (row.get("coursename") or "").strip() or (str(cid) if cid else "")
            grade = row.get("grade")
            grades.append(SdoGrade(course=course, grade=str(grade) if grade not in (None, "") else None))
        if not grades and rows is None:
            return SdoResult(success=False, message="Не удалось получить оценки (проверьте сессию СДО)")
        return SdoResult(success=True, message="OK", grades=grades)


__all__ = ["MireaSDO", "SdoCourse", "SdoDeadline", "SdoGrade", "SdoResult", "SDO_URL"]
