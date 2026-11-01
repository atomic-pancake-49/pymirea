"""Schedule client for МИРЭА.

Sources:

1. **Supabase mirea.ninja** (``https://supabase.mirea.ninja``) — public
   PostgREST RPCs with a publishable key: ``search_schedule_targets``,
   ``get_schedule_for_entity``, ``get_free_rooms_window``. Primary source.
   (The old ``app-api.mirea.ninja`` REST API is deprecated — do not use it.)

2. **schedule-of.mirea.ru** — official МИРЭА iCal feeds
   (``/schedule/api/ical/{1|2|3}/{id}`` for group/teacher/classroom).
   Fallback when Supabase is unavailable.

3. **english.mirea.ru ICS endpoint** — :meth:`MireaSchedule.fetch_ical_for_group`.

Both surfaces feed the same :class:`Schedule` dataclass. Three different
JSON shapes from mirea.ninja (``data``-array of lessons, ``lessons``-by-
day, raw lesson lists) are normalized into a single :class:`ScheduleEvent`
list.

The result supports :meth:`Schedule.to_ics` for RFC 5545 export — useful
for "Add to Apple Calendar" / "Add to Google Calendar" buttons.

The library does not cache; consumers should add their own caching layer
(grades_cache pattern) if needed."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

from ._http import make_async_client
from ._settings import settings
from .exceptions import MireaParseFailed
from .upstreams import get_breaker

logger = logging.getLogger(__name__)


# ─── Constants ───────────────────────────────────────────────────────

SCHEDULE_SUPABASE_URL = "https://ejzybbyjwtzbibrrwrli.supabase.co"  # = supabase.mirea.ninja
SCHEDULE_SUPABASE_KEY = "sb_publishable_l4qxhUYyypOh4Zqqnn7dBw_zPS_CO2y"  # publishable, публичный
SCHEDULE_ORGANIZATION_ID = "mirea"
SCHEDULE_OF_BASE = "https://schedule-of.mirea.ru"
_ICAL_TARGET_TYPES = {"group": 1, "teacher": 2, "classroom": 3}
ENGLISH_MIREA_ICAL_BASE = "https://english.mirea.ru/schedule/api/ical"

_MSK_TZ = timezone(timedelta(hours=3))


# ─── Dataclasses ─────────────────────────────────────────────────────


@dataclass
class ScheduleEvent:
    """Single lesson / event in a schedule."""

    start: datetime
    end: Optional[datetime]
    summary: str
    location: Optional[str] = None
    description: str = ""


@dataclass
class GroupInfo:
    """Search result entry for groups / teachers."""

    name: str
    uid: Optional[str] = None


@dataclass
class ClassroomInfo:
    """Search result entry for classrooms — adds optional campus info."""

    name: str
    uid: Optional[str] = None
    campus: Optional[dict] = None  # {"name": str, "short_name": str}


@dataclass
class Schedule:
    """Result of a schedule fetch.

    ``raw_payload`` is included when normalization yielded no events —
    useful for diagnosing schema drift on MIREA's side.
    """

    events: list[ScheduleEvent] = field(default_factory=list)
    entity_type: Optional[str] = None  # "group" | "teacher" | "classroom"
    entity_name: Optional[str] = None
    entity_uid: Optional[str] = None
    raw_payload: Any = None

    def to_ics(self, *, calendar_name: str = "MIREA Schedule") -> str:
        """Export events to RFC 5545 ICS calendar text.

        Suitable for download as ``.ics`` file or ``Content-Type:
        text/calendar`` HTTP response. Imports cleanly into Apple Calendar,
        Google Calendar, Outlook.
        """
        lines: list[str] = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//pymirea//Schedule//EN",
            "CALSCALE:GREGORIAN",
            f"X-WR-CALNAME:{_ics_escape_text(calendar_name)}",
        ]
        dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for idx, ev in enumerate(self.events):
            uid = f"{idx}-{int(ev.start.timestamp())}@pymirea.local"
            lines.append("BEGIN:VEVENT")
            lines.append(f"UID:{uid}")
            lines.append(f"DTSTAMP:{dtstamp}")
            lines.append(f"DTSTART:{_ics_format_dt(ev.start)}")
            if ev.end:
                lines.append(f"DTEND:{_ics_format_dt(ev.end)}")
            lines.append(f"SUMMARY:{_ics_escape_text(ev.summary or '')}")
            if ev.location:
                lines.append(f"LOCATION:{_ics_escape_text(ev.location)}")
            if ev.description:
                lines.append(f"DESCRIPTION:{_ics_escape_text(ev.description)}")
            lines.append("END:VEVENT")
        lines.append("END:VCALENDAR")
        # RFC 5545 line-folding: long content-lines should wrap at 75 octets,
        # but most consumers tolerate longer; keep simple for now.
        return "\r\n".join(lines) + "\r\n"


# ─── ICS utilities ───────────────────────────────────────────────────


def _ics_escape_text(value: str) -> str:
    """Escape a TEXT-type value per RFC 5545 §3.3.11.

    Order matters: backslash MUST be escaped first, otherwise a freshly-
    inserted ``\\,`` would itself get its backslash doubled.
    """
    return (
        value.replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _ics_format_dt(dt: datetime) -> str:
    """Format datetime per RFC 5545 §3.3.5. Use UTC ``Z`` form for
    aware-UTC datetimes; floating local for naive."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y%m%dT%H%M%SZ")
    return dt.strftime("%Y%m%dT%H%M%S")


# ─── ICS parsing (consume incoming iCal) ─────────────────────────────


def _unfold_ical_lines(text: str) -> list[str]:
    """Reverse the line-folding from RFC 5545 §3.1 — continuations begin
    with whitespace and append to the previous line."""
    lines = text.splitlines()
    unfolded: list[str] = []
    for line in lines:
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def _parse_ical_datetime(value: str) -> Optional[datetime]:
    """Parse a DTSTART/DTEND value (basic ISO 8601 forms)."""
    if not value:
        return None
    val = value.strip()
    is_utc = val.endswith("Z")
    if is_utc:
        val = val[:-1]
    try:
        if "T" in val:
            dt = datetime.strptime(val, "%Y%m%dT%H%M%S")
        else:
            dt = datetime.strptime(val, "%Y%m%d")
        if is_utc:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _parse_ical_events(text: str) -> list[dict]:
    """Parse VEVENT blocks out of an iCal document. Returns raw key/value
    dicts; turn into :class:`ScheduleEvent` via :func:`_ical_to_event`."""
    lines = _unfold_ical_lines(text)
    events: list[dict] = []
    current: Optional[dict] = None

    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue

        key, value = line.split(":", 1)
        key = key.split(";", 1)[0].upper()
        value = value.replace("\\n", "\n").strip()
        current[key] = value

    return events


def _ical_to_event(raw: dict) -> Optional[ScheduleEvent]:
    dt_start = _parse_ical_datetime(raw.get("DTSTART", ""))
    if not dt_start:
        return None
    dt_end = _parse_ical_datetime(raw.get("DTEND", ""))
    return ScheduleEvent(
        start=dt_start,
        end=dt_end,
        summary=raw.get("SUMMARY", ""),
        location=raw.get("LOCATION") or None,
        description=raw.get("DESCRIPTION", ""),
    )


# ─── Datetime parsing helpers (MIREA's various flavours) ─────────────


def _parse_dt(value: Any) -> Optional[datetime]:
    """Try every reasonable representation MIREA endpoints have used."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 1e12 else value
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(raw, fmt).replace(tzinfo=_MSK_TZ)
            except Exception:
                continue
    return None


def _combine_date_time(date_str: Optional[str], time_str: Optional[str]) -> Optional[datetime]:
    """Combine a YYYY-MM-DD-ish date with a HH:MM time, anchoring to MSK."""
    if not date_str or not time_str:
        return None
    date_val = _parse_dt(date_str)
    if not date_val:
        for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
            try:
                date_val = datetime.strptime(date_str, fmt)
                break
            except Exception:
                continue
    if not date_val:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            time_val = datetime.strptime(time_str, fmt).time()
            return datetime.combine(date_val.date(), time_val, tzinfo=_MSK_TZ)
        except Exception:
            continue
    return None


# ─── Format normalisers ──────────────────────────────────────────────


def _extract_events(payload: Any) -> list[ScheduleEvent]:
    """Walk a JSON payload from any of MIREA's three known schedule
    schemas and produce a sorted list of :class:`ScheduleEvent`.

    Supported shapes:
    - ``{"days": [{"date": "...", "lessons": [...]}, ...]}`` — Pulse-ish
    - ``{"data": [{"dates": [...], "subject": "...", ...}]}`` — mirea.ninja
    - ``{"lessons": [...]}`` / ``{"pairs": [...]}`` — flat day list
    """
    events: list[ScheduleEvent] = []

    def normalize_lesson(lesson: dict, day_date: Optional[str] = None) -> None:
        if not isinstance(lesson, dict):
            return
        summary = (
            lesson.get("name")
            or lesson.get("title")
            or lesson.get("subject")
            or lesson.get("discipline")
            or lesson.get("summary")
            or "Занятие"
        )
        location = (
            lesson.get("location")
            or lesson.get("room")
            or lesson.get("auditory")
            or lesson.get("auditorium")
            or lesson.get("classroom")
        )
        teacher = lesson.get("teacher") or lesson.get("lecturer") or lesson.get("instructor")
        description = lesson.get("description") or ""
        if teacher and teacher not in description:
            description = f"{description}\n{teacher}".strip()

        start_raw = lesson.get("start") or lesson.get("start_time") or lesson.get("startTime")
        end_raw = lesson.get("end") or lesson.get("end_time") or lesson.get("endTime")
        date_raw = lesson.get("date") or lesson.get("day") or lesson.get("lesson_date") or day_date

        dt_start = _parse_dt(start_raw)
        dt_end = _parse_dt(end_raw)
        if not dt_start:
            dt_start = _combine_date_time(date_raw, start_raw)
        if not dt_end:
            dt_end = _combine_date_time(date_raw, end_raw)
        if not dt_start:
            return

        events.append(ScheduleEvent(
            start=dt_start,
            end=dt_end,
            summary=summary,
            location=location,
            description=description.strip() if description else "",
        ))

    def normalize_mirea_ninja_item(item: dict) -> None:
        if not isinstance(item, dict):
            return
        dates = item.get("dates") or []
        bells = item.get("lesson_bells") or {}
        start_time = bells.get("start_time")
        end_time = bells.get("end_time")
        subject = item.get("subject") or "Занятие"
        lesson_type = item.get("lesson_type")
        summary = f"{subject} ({lesson_type})" if lesson_type else subject

        teachers: list[str] = []
        for teacher in item.get("teachers") or []:
            name = teacher.get("name") if isinstance(teacher, dict) else str(teacher)
            if name:
                teachers.append(name)

        rooms: list[str] = []
        for room in item.get("classrooms") or []:
            if isinstance(room, dict):
                room_name = room.get("name")
                campus = room.get("campus", {})
                campus_name = None
                if isinstance(campus, dict):
                    campus_name = campus.get("short_name") or campus.get("name")
                if room_name:
                    rooms.append(f"{room_name} ({campus_name})" if campus_name else room_name)
            elif room:
                rooms.append(str(room))

        description = ""
        if teachers:
            description = "Преподаватели: " + ", ".join(teachers)

        for date_str in dates:
            dt_start = _combine_date_time(date_str, start_time)
            if not dt_start:
                continue
            dt_end = _combine_date_time(date_str, end_time)
            events.append(ScheduleEvent(
                start=dt_start,
                end=dt_end,
                summary=summary,
                location=", ".join(rooms) if rooms else None,
                description=description,
            ))

    def walk(obj: Any) -> None:
        if isinstance(obj, list):
            for item in obj:
                walk(item)
            return
        if not isinstance(obj, dict):
            return
        if "lessons" in obj and isinstance(obj["lessons"], list):
            day_date = obj.get("date") or obj.get("day")
            for lesson in obj["lessons"]:
                normalize_lesson(lesson, day_date)
            return
        if "pairs" in obj and isinstance(obj["pairs"], list):
            day_date = obj.get("date") or obj.get("day")
            for lesson in obj["pairs"]:
                normalize_lesson(lesson, day_date)
            return
        if "data" in obj and isinstance(obj["data"], list):
            for item in obj["data"]:
                normalize_mirea_ninja_item(item)
            return
        if obj.get("type") == "__lesson_schedule__":
            normalize_mirea_ninja_item(obj)
            return
        if any(k in obj for k in ("start", "start_time", "startTime")):
            normalize_lesson(obj)
            return
        for key in ("schedule", "lessons", "days", "data", "items"):
            if key in obj:
                walk(obj[key])

    walk(payload)
    events.sort(key=lambda e: e.start)
    return events


def _derive_entity_name(kind: str, payload: Any) -> Optional[str]:
    """Pull a human-readable entity name out of a schedule payload
    (``{"data": [...]}`` or a bare list of lesson items)."""
    if isinstance(payload, list):
        data = payload
    elif isinstance(payload, dict):
        data = payload.get("data")
    else:
        return None
    if not isinstance(data, list) or not data:
        return None
    for item in data:
        if not isinstance(item, dict):
            continue
        if kind == "teacher":
            for t in item.get("teachers") or []:
                if isinstance(t, dict) and t.get("name"):
                    return str(t.get("name"))
                if isinstance(t, str) and t.strip():
                    return t.strip()
        elif kind == "classroom":
            for r in item.get("classrooms") or []:
                if isinstance(r, dict) and r.get("name"):
                    return str(r.get("name"))
                if isinstance(r, str) and r.strip():
                    return r.strip()
        elif kind == "group":
            for g in item.get("groups") or []:
                if isinstance(g, str) and g.strip():
                    return g.strip()
    return None


def _extract_group_info(item: Any) -> Optional[GroupInfo]:
    if isinstance(item, dict):
        name = item.get("name") or item.get("group") or item.get("title") or item.get("value")
        uid = item.get("uid") or item.get("id") or item.get("group_id")
        if name:
            return GroupInfo(name=str(name), uid=str(uid) if uid is not None else None)
    if isinstance(item, str):
        return GroupInfo(name=item, uid=None)
    return None


def _extract_classroom_info(item: Any) -> Optional[ClassroomInfo]:
    if isinstance(item, dict):
        base = _extract_group_info(item)
        if not base:
            return None
        info = ClassroomInfo(name=base.name, uid=base.uid)
        campus = item.get("campus")
        if isinstance(campus, dict):
            campus_name = campus.get("name")
            campus_short = campus.get("short_name")
            if campus_name or campus_short:
                info.campus = {"name": campus_name, "short_name": campus_short}
        return info
    if isinstance(item, str):
        return ClassroomInfo(name=item, uid=None)
    return None


# ─── MireaSchedule client ────────────────────────────────────────────


class MireaSchedule:
    """Async client for MIREA's schedule sources.

    Construct without arguments; underlying HTTP client is created lazily
    on first use and shared across calls. Always ``await close()`` (or
    use as part of :class:`pymirea.Client`'s context manager).
    """

    def __init__(self) -> None:
        self._client: Optional[httpx.AsyncClient] = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            try:
                proxy = settings.mirea_proxy
            except (RuntimeError, AttributeError):
                proxy = None
            self._client = make_async_client(
                proxy=proxy,
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # ── Internal HTTP wrapper ──────────────────────────────────────

    async def _get_with_breaker(
        self,
        url: str,
        *,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
    ) -> tuple[Optional[httpx.Response], Optional[str]]:
        """Fetch a URL with circuit-breaker protection.

        Returns ``(response, error_message)``. On breaker-open or network
        failure, response is ``None`` and ``error_message`` is set.
        """
        breaker = get_breaker("schedule_api")
        decision = await breaker.allow()
        if not decision.allowed:
            retry_after = decision.retry_after_s or 5
            return None, f"Расписание временно недоступно. Попробуйте через {retry_after} сек."

        try:
            client = self._get_client()
            response = await client.get(url, params=params, headers=headers or {})
            if 500 <= int(response.status_code) <= 599:
                await breaker.record_failure()
            else:
                await breaker.record_success()
            return response, None
        except httpx.TimeoutException:
            await breaker.record_failure()
            return None, "Сервис расписания не отвечает"
        except Exception:
            await breaker.record_failure()
            return None, "Ошибка сервиса расписания"

    async def _rpc(self, name: str, body: dict) -> tuple[Any, Optional[str]]:
        """POST to a Supabase PostgREST RPC with circuit-breaker protection."""
        breaker = get_breaker("schedule_api")
        decision = await breaker.allow()
        if not decision.allowed:
            retry_after = decision.retry_after_s or 5
            return None, f"Расписание временно недоступно. Попробуйте через {retry_after} сек."
        try:
            response = await self._get_client().post(
                f"{SCHEDULE_SUPABASE_URL}/rest/v1/rpc/{name}",
                json=body,
                headers={
                    "apikey": SCHEDULE_SUPABASE_KEY,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
        except httpx.TimeoutException:
            await breaker.record_failure()
            return None, "Сервис расписания не отвечает"
        except Exception:
            await breaker.record_failure()
            return None, "Ошибка сервиса расписания"
        if 500 <= int(response.status_code) <= 599:
            await breaker.record_failure()
        else:
            await breaker.record_success()
        if response.status_code != 200:
            return None, f"Сервис расписания вернул HTTP {response.status_code}"
        try:
            return response.json(), None
        except Exception:
            return None, "Сервис расписания вернул не JSON"

    # ── Search ─────────────────────────────────────────────────────

    async def _search_targets(self, kind: str, query: str, limit: int = 20) -> list[dict]:
        data, _ = await self._rpc(
            "search_schedule_targets",
            {
                "p_target_type": kind,
                "p_query": query or "",
                "p_organization_id": SCHEDULE_ORGANIZATION_ID,
                "p_limit": limit,
            },
        )
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []

    @staticmethod
    def _target_name(item: dict) -> Optional[str]:
        name = item.get("full_title") or item.get("target_title") or item.get("name")
        return str(name) if name else None

    async def search_groups(self, query: str) -> list[GroupInfo]:
        """Search for groups by name. Empty list on any failure (search is
        best-effort — UI usually falls back to free-text query)."""
        results: list[GroupInfo] = []
        for item in await self._search_targets("group", query):
            name = self._target_name(item)
            if name:
                uid = item.get("external_id")
                results.append(GroupInfo(name=name, uid=str(uid) if uid is not None else None))
        return results

    async def search_teachers(self, query: str) -> list[GroupInfo]:
        """Search for teachers by name (``name`` is the full name)."""
        results: list[GroupInfo] = []
        for item in await self._search_targets("teacher", query):
            name = self._target_name(item)
            if name:
                uid = item.get("external_id")
                results.append(GroupInfo(name=name, uid=str(uid) if uid is not None else None))
        return results

    async def search_classrooms(self, query: str) -> list[ClassroomInfo]:
        """Search for classrooms by name (``campus.short_name`` parsed from «А-9 (В-78)»)."""
        results: list[ClassroomInfo] = []
        for item in await self._search_targets("classroom", query):
            name = self._target_name(item)
            if not name:
                continue
            uid = item.get("external_id")
            info = ClassroomInfo(name=name, uid=str(uid) if uid is not None else None)
            m = re.search(r"\(([^()]+)\)\s*$", name)
            if m:
                info.campus = {"name": None, "short_name": m.group(1).strip()}
            results.append(info)
        return results

    # ── Free rooms ─────────────────────────────────────────────────

    async def get_free_rooms(self, start: datetime, end: datetime) -> Optional[list[dict]]:
        """Аудитории, свободные во всём интервале ``[start, end)``.

        Returns ``[{"room": "А-9", "campus": "В-78"}, ...]`` or ``None`` if
        the service is unavailable."""
        data, _ = await self._rpc(
            "get_free_rooms_window",
            {
                "p_organization_id": SCHEDULE_ORGANIZATION_ID,
                "p_from": start.isoformat(),
                "p_to": end.isoformat(),
            },
        )
        if not isinstance(data, list):
            return None
        return [
            {"room": str(x.get("room")), "campus": x.get("campus")}
            for x in data
            if isinstance(x, dict) and x.get("room")
        ]

    # ── Fetch by entity ────────────────────────────────────────────

    async def get_group_schedule(
        self,
        *,
        uid: str,
        name: Optional[str] = None,
    ) -> Schedule:
        """Fetch schedule for a known group UID (or fallback ``name``)."""
        return await self._fetch_entity_schedule("group", uid=uid, name=name)

    async def get_teacher_schedule(
        self,
        *,
        uid: str,
        name: Optional[str] = None,
    ) -> Schedule:
        return await self._fetch_entity_schedule("teacher", uid=uid, name=name)

    async def get_classroom_schedule(
        self,
        *,
        uid: str,
        name: Optional[str] = None,
    ) -> Schedule:
        return await self._fetch_entity_schedule("classroom", uid=uid, name=name)

    async def _fetch_entity_schedule(
        self,
        kind: str,
        *,
        uid: str,
        name: Optional[str] = None,
    ) -> Schedule:
        entity = uid or name or ""
        payload, _ = await self._rpc(
            "get_schedule_for_entity",
            {
                "p_entity_type": kind,
                "p_entity": entity,
                "p_organization_id": SCHEDULE_ORGANIZATION_ID,
            },
        )
        events = _extract_events(payload) if payload is not None else []
        if events:
            return Schedule(
                events=events,
                entity_type=kind,
                entity_name=name or _derive_entity_name(kind, payload),
                entity_uid=uid,
            )

        # Fallback: официальный iCal schedule-of.mirea.ru (нужен числовой id).
        type_no = _ICAL_TARGET_TYPES.get(kind)
        if type_no and uid and str(uid).isdigit():
            try:
                ical = await self.fetch_ical(f"{SCHEDULE_OF_BASE}/schedule/api/ical/{type_no}/{uid}")
            except MireaParseFailed:
                ical = None
            if ical and ical.events:
                return Schedule(events=ical.events, entity_type=kind, entity_name=name, entity_uid=uid)

        return Schedule(
            events=[],
            entity_type=kind,
            entity_name=name,
            entity_uid=uid,
            raw_payload=payload,
        )

    # ── ICS endpoints ──────────────────────────────────────────────

    async def fetch_ical(self, url: str) -> Schedule:
        """Fetch and parse an iCal URL. Caller is responsible for
        validating the URL host (SSRF protection is consumer-side)."""
        client = self._get_client()
        try:
            response = await client.get(
                url,
                headers={"User-Agent": "pymirea/schedule"},
            )
        except Exception as e:
            raise MireaParseFailed(f"failed to fetch iCal from {url}: {e}") from e

        if response.status_code != 200:
            raise MireaParseFailed(
                f"iCal endpoint returned HTTP {response.status_code}",
                response_preview=(response.text or "")[:500],
            )

        raw_events = _parse_ical_events(response.text)
        events: list[ScheduleEvent] = []
        for raw in raw_events:
            ev = _ical_to_event(raw)
            if ev:
                events.append(ev)
        events.sort(key=lambda e: e.start)
        return Schedule(events=events)

    async def fetch_ical_for_group(
        self,
        group_id: str,
        institute_id: str = "1",
    ) -> Schedule:
        """Convenience wrapper for the
        ``english.mirea.ru/schedule/api/ical/{institute_id}/{group_id}``
        URL pattern."""
        url = f"{ENGLISH_MIREA_ICAL_BASE}/{institute_id}/{group_id}"
        return await self.fetch_ical(url)


__all__ = [
    "ClassroomInfo",
    "GroupInfo",
    "MireaSchedule",
    "Schedule",
    "ScheduleEvent",
]
