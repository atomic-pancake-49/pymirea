"""Общий прикладной слой для CLI и MCP-сервера.

Каждая функция возвращает простые JSON-сериализуемые структуры (dict/list),
чтобы CLI мог печатать их таблицей или ``--json``, а MCP — отдавать модели
как есть. Сессия берётся из :class:`SessionStore` и после вызова
сохраняется обратно: сервисы pymirea мутируют dict cookies на месте
(обновлённые токены, ``.AspNetCore.Cookies``, кеш visiting log id).
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from .acs import MireaACS
from .grades import MireaGrades
from .schedule import MireaSchedule, Schedule
from .sdo import MireaSDO
from .session import MireaAPI
from .store import SessionStore
from .tokens import get_token_age_seconds, get_token_exp

MSK = timezone(timedelta(hours=3))

ENTITY_KINDS = ("group", "teacher", "classroom")

_ATTEND_TYPES = {0: "unknown", 1: "absent", 2: "excused", 3: "present"}


class MireaAppError(RuntimeError):
    """Понятная пользователю ошибка (МИРЭА ответила отказом, ничего не найдено и т.п.)."""


def _iso(epoch: float | None) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, MSK).isoformat(timespec="minutes")


def _snapshot(cookies: dict) -> str:
    return json.dumps(cookies, sort_keys=True, default=str)


@asynccontextmanager
async def session(store: SessionStore) -> AsyncIterator[dict]:
    """Загрузить сессию, отдать dict cookies и сохранить его, если сервисы его изменили."""
    store.configure()
    stored = store.load()
    cookies = stored.cookies
    before = _snapshot(cookies)
    try:
        yield cookies
    finally:
        if _snapshot(cookies) != before:
            store.save(cookies)


# --- аккаунт ---


async def status(store: SessionStore, *, check: bool = False) -> dict:
    store.configure()
    if not store.exists():
        return {"logged_in": False, "home": str(store.home)}
    stored = store.load()
    out: dict = {
        "logged_in": True,
        "login": stored.login,
        "home": str(store.home),
        "saved_at": datetime.fromtimestamp(stored.saved_at, MSK).isoformat(timespec="seconds")
        if stored.saved_at
        else None,
        "token_age_s": get_token_age_seconds(stored.cookies),
        "token_exp": _iso(get_token_exp(stored.cookies)),
        "has_aspnet_cookie": bool(stored.cookies.get(".AspNetCore.Cookies")),
    }
    if check:
        from .auth import MireaAuth

        auth = MireaAuth()
        try:
            out["session_active"] = await auth.verify_session(stored.cookies)
        finally:
            await auth.close()
    return out


# --- личный кабинет (нужна сессия) ---


async def my_schedule(store: SessionStore, *, days: int = 7) -> dict:
    days = max(1, min(int(days), 31))
    async with session(store) as cookies:
        api = MireaGrades(session_cookies=cookies)
        try:
            res = await api.get_schedule(days=days)
        finally:
            await api.close()
    if not res.success and res.message != "Нет пар в ближайшие дни":
        raise MireaAppError(res.message)
    return {
        "days": days,
        "lessons": [
            {
                "start": _iso(l.start_epoch),
                "end": _iso(l.end_epoch),
                "name": l.name,
                "type": l.lesson_type or None,
                "room": l.room or None,
                "teacher": l.teacher or None,
                "subgroup": l.subgroup or None,
            }
            for l in (res.lessons or [])
        ],
    }


async def grades(store: SessionStore) -> dict:
    async with session(store) as cookies:
        api = MireaGrades(session_cookies=cookies)
        try:
            res = await api.get_grades()
        finally:
            await api.close()
    if not res.success:
        raise MireaAppError(res.message)
    return {"subjects": [asdict(s) for s in (res.subjects or [])]}


async def attendance(store: SessionStore, discipline: str) -> dict:
    """Посещаемость по одной дисциплине: ``discipline`` — discipline_id или часть названия."""
    query = (discipline or "").strip()
    if not query:
        raise MireaAppError("Укажите дисциплину (id или часть названия).")
    async with session(store) as cookies:
        api = MireaGrades(session_cookies=cookies)
        try:
            gr = await api.get_grades()
            if not gr.success:
                raise MireaAppError(gr.message)
            subjects = gr.subjects or []
            match = [s for s in subjects if s.discipline_id == query]
            if not match:
                q = query.casefold()
                match = [s for s in subjects if q in (s.name or "").casefold()]
            if not match:
                raise MireaAppError(f"Дисциплина «{query}» не найдена среди {len(subjects)} предметов.")
            if len(match) > 1:
                names = "; ".join(s.name for s in match)
                raise MireaAppError(f"Под «{query}» подходит несколько дисциплин: {names}. Уточните.")
            subj = match[0]
            if not subj.discipline_id:
                raise MireaAppError(f"У дисциплины «{subj.name}» нет discipline_id.")
            res = await api.get_attendance_detail(subj.discipline_id, gr.semester)
        finally:
            await api.close()
    if not res.success:
        raise MireaAppError(res.message)
    return {
        "discipline": subj.name,
        "discipline_id": subj.discipline_id,
        "summary": asdict(res.summary) if res.summary else None,
        "entries": [
            {"lesson_start": _iso(e.lesson_start), "status": _ATTEND_TYPES.get(e.attend_type or 0, "unknown")}
            for e in (res.entries or [])
        ],
    }


async def sdo(store: SessionStore, *, limit: int = 50) -> dict:
    """Курсы, ближайшие дедлайны и оценки из СДО (Moodle)."""
    async with session(store) as cookies:
        api = MireaSDO(session_cookies=cookies)
        try:
            courses = await api.get_courses()
            deadlines = await api.get_deadlines(limit=limit)
            grades = await api.get_grades(course_names={c.id: c.name for c in (courses.courses or [])})
        finally:
            await api.close()
    if not courses.success and not deadlines.success:
        raise MireaAppError(courses.message or deadlines.message)
    return {
        "courses": [asdict(c) for c in (courses.courses or [])],
        "deadlines": [
            {"name": d.name, "course": d.course, "when": _iso(d.timestamp), "activity": d.activity or None, "url": d.url or None}
            for d in (deadlines.deadlines or [])
        ],
        "grades": [asdict(g) for g in (grades.grades or [])],
    }


async def acs_today(store: SessionStore) -> dict:
    async with session(store) as cookies:
        api = MireaACS(session_cookies=cookies)
        try:
            res = await api.get_today_events()
        finally:
            await api.close()
    if not res.success:
        raise MireaAppError(res.message)
    return {"date": res.date, "events": [asdict(e) for e in res.events]}


async def mark_attendance(store: SessionStore, qr_data: str) -> dict:
    """Отметиться на паре по QR (только себя). Не экспортируется в MCP."""
    async with session(store) as cookies:
        api = MireaAPI(session_cookies=cookies)
        try:
            res = await api.mark_attendance(qr_data)
        finally:
            await api.close()
    return {"success": res.success, "message": res.message}


# --- публичное расписание (сессия не нужна) ---


async def search(store: SessionStore, kind: str, query: str) -> list[dict]:
    if kind not in ENTITY_KINDS:
        raise MireaAppError(f"kind должен быть одним из {ENTITY_KINDS}")
    store.configure()
    sched = MireaSchedule()
    try:
        if kind == "group":
            items = await sched.search_groups(query)
        elif kind == "teacher":
            items = await sched.search_teachers(query)
        else:
            items = await sched.search_classrooms(query)
    finally:
        await sched.close()
    return [asdict(i) for i in items]


def _filter_events(schedule: Schedule, date_from: datetime | None, days: int | None) -> list[dict]:
    events = schedule.events
    if date_from is not None and days:
        start = date_from
        end = start + timedelta(days=days)
        events = [e for e in events if start <= _aware(e.start) < end]
    return [
        {
            "start": _aware(e.start).isoformat(timespec="minutes"),
            "end": _aware(e.end).isoformat(timespec="minutes") if e.end else None,
            "summary": e.summary,
            "location": e.location,
            "description": e.description or None,
        }
        for e in events
    ]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=MSK)


async def fetch_entity_schedule(store: SessionStore, kind: str, query: str) -> Schedule:
    """Найти группу/преподавателя/аудиторию по имени и вернуть полное расписание."""
    if kind not in ENTITY_KINDS:
        raise MireaAppError(f"kind должен быть одним из {ENTITY_KINDS}")
    store.configure()
    sched = MireaSchedule()
    try:
        if kind == "group":
            found = await sched.search_groups(query)
        elif kind == "teacher":
            found = await sched.search_teachers(query)
        else:
            found = await sched.search_classrooms(query)
        if not found:
            raise MireaAppError(f"По запросу «{query}» ничего не найдено ({kind}).")
        q = query.casefold().strip()
        exact = [f for f in found if (f.name or "").casefold().strip() == q]
        pick = exact[0] if exact else found[0]
        if kind == "group":
            result = await sched.get_group_schedule(uid=pick.uid or pick.name, name=pick.name)
        elif kind == "teacher":
            result = await sched.get_teacher_schedule(uid=pick.uid or pick.name, name=pick.name)
        else:
            result = await sched.get_classroom_schedule(uid=pick.uid or pick.name, name=pick.name)
    finally:
        await sched.close()
    return result


async def entity_schedule(
    store: SessionStore,
    kind: str,
    query: str,
    *,
    days: int | None = 7,
    date_from: datetime | None = None,
) -> dict:
    result = await fetch_entity_schedule(store, kind, query)
    if date_from is None:
        now = datetime.now(MSK)
        date_from = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "kind": kind,
        "name": result.entity_name,
        "uid": result.entity_uid,
        "from": date_from.date().isoformat() if days else None,
        "days": days,
        "events": _filter_events(result, date_from, days),
    }


__all__ = [
    "ENTITY_KINDS",
    "MSK",
    "MireaAppError",
    "acs_today",
    "attendance",
    "entity_schedule",
    "fetch_entity_schedule",
    "grades",
    "mark_attendance",
    "my_schedule",
    "sdo",
    "search",
    "session",
    "status",
]
