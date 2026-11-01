"""Командная строка pymirea.

Примеры::

    pymirea login                      # логин + пароль (getpass) + OTP из почты
    pymirea schedule --days 3          # своё расписание из Пульса
    pymirea grades                     # баллы БРС
    pymirea attendance "Физика"        # посещаемость по дисциплине
    pymirea acs                        # турникеты за сегодня
    pymirea search group ЭФБО-01-24    # поиск в публичном расписании
    pymirea timetable group ЭФБО-01-24 --ics group.ics
    pymirea mcp                        # MCP-сервер (stdio)

Глобальный ``--json`` переключает вывод в JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import sys
from datetime import datetime
from typing import Any

from . import __version__
from . import app as A
from .store import NotLoggedIn, SessionStore

_DAY_NAMES = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def _print_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _day_header(iso: str | None) -> str:
    if not iso:
        return "?"
    dt = datetime.fromisoformat(iso)
    return f"{dt.strftime('%d.%m')} ({_DAY_NAMES[dt.weekday()]})"


def _hm(iso: str | None) -> str:
    return datetime.fromisoformat(iso).strftime("%H:%M") if iso else "??:??"


def _fmt_num(x: Any) -> str:
    if x is None:
        return "—"
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    return str(int(f)) if f.is_integer() else f"{f:.1f}"


# --- вывод ---


def _render_my_schedule(data: dict) -> None:
    lessons = data["lessons"]
    if not lessons:
        print(f"Пар на ближайшие {data['days']} дн. нет.")
        return
    current = None
    for l in lessons:
        day = _day_header(l["start"])
        if day != current:
            print(f"\n=== {day} ===")
            current = day
        kind = f"[{l['type']}] " if l.get("type") else ""
        extra = ", ".join(x for x in (l.get("room"), l.get("teacher"), l.get("subgroup")) if x)
        print(f"  {_hm(l['start'])}-{_hm(l['end'])}  {kind}{l['name']}" + (f"  — {extra}" if extra else ""))


def _render_grades(data: dict) -> None:
    subjects = data["subjects"]
    if not subjects:
        print("Предметов нет.")
        return
    width = min(max(len(s["name"]) for s in subjects), 50)
    print(f"{'Предмет':<{width}}  ТК/40  СК/30  Пос/30  Дост  Доп  Итого")
    for s in subjects:
        att = _fmt_num(s["attendance"])
        if s.get("attendance_max_possible") is not None:
            att += f"({_fmt_num(s['attendance_max_possible'])})"
        print(
            f"{s['name'][:width]:<{width}}  {_fmt_num(s['current_control']):>5}  {_fmt_num(s['semester_control']):>5}"
            f"  {att:>6}  {_fmt_num(s['achievements']):>4}  {_fmt_num(s['additional']):>3}  {_fmt_num(s['total']):>5}"
        )


def _render_attendance(data: dict) -> None:
    print(data["discipline"])
    s = data.get("summary")
    if s:
        print(f"Всего: {s['total_lessons']}  был: {s['present']}  уваж.: {s['excused']}  пропуск: {s['absent']}")
    marks = {"present": "✓", "excused": "~", "absent": "✗", "unknown": "?"}
    for e in data["entries"]:
        print(f"  {marks.get(e['status'], '?')} {e['lesson_start'] or '?'}")


def _render_sdo(data: dict) -> None:
    courses = data["courses"]
    print(f"Курсы ({len(courses)}):")
    for c in courses:
        print(f"  \u2022 {c['name']}")
    deadlines = data["deadlines"]
    print(f"\n\u0414\u0435\u0434\u043b\u0430\u0439\u043d\u044b ({len(deadlines)}):")
    for d in deadlines:
        print(f"  {d['when'] or '?'}  {d['name']}  \u2014 {d['course']}")
    grades = [g for g in data["grades"] if g.get("grade")]
    if grades:
        print(f"\n\u041e\u0446\u0435\u043d\u043a\u0438 ({len(grades)}):")
        for g in grades:
            print(f"  {g['grade']:>8}  {g['course']}")


def _render_acs(data: dict) -> None:
    events = data["events"]
    if not events:
        print("Сегодня проходов через турникеты нет.")
        return
    for e in events:
        dur = f"  ({e['duration_label']})" if e.get("duration_label") else ""
        print(f"  {e['time_label']}  {e['exit_zone'] or '?'} → {e['enter_zone'] or '?'}{dur}")


def _render_search(items: list[dict]) -> None:
    if not items:
        print("Ничего не найдено.")
        return
    for i in items:
        campus = ""
        if isinstance(i.get("campus"), dict):
            campus = f"  [{i['campus'].get('short_name') or i['campus'].get('name')}]"
        print(f"  {i['name']}  (uid={i.get('uid')}){campus}")


def _render_timetable(data: dict) -> None:
    print(f"{data['name'] or '?'} ({data['kind']})")
    events = data["events"]
    if not events:
        print("Занятий в выбранном диапазоне нет.")
        return
    current = None
    for e in events:
        day = _day_header(e["start"])
        if day != current:
            print(f"\n=== {day} ===")
            current = day
        loc = f"  — {e['location']}" if e.get("location") else ""
        print(f"  {_hm(e['start'])}-{_hm(e['end'])}  {e['summary']}{loc}")


def _render_status(data: dict) -> None:
    if not data["logged_in"]:
        print(f"Не залогинен. Хранилище: {data['home']}")
        return
    print(f"Логин: {data.get('login') or '?'}")
    print(f"Сессия сохранена: {data.get('saved_at')}")
    if data.get("token_exp"):
        print(f"access_token истекает: {data['token_exp']}")
    print(f".AspNetCore.Cookies: {'есть' if data.get('has_aspnet_cookie') else 'нет'}")
    if "session_active" in data:
        print(f"Сессия активна: {'да' if data['session_active'] else 'нет'}")
    print(f"Хранилище: {data['home']}")


# --- команды ---


async def cmd_login(args, store: SessionStore) -> int:
    from .auth import MireaAuth

    store.configure()
    login = args.login or os.environ.get("MIREA_LOGIN") or input("Логин (…@edu.mirea.ru): ").strip()
    password = os.environ.get("MIREA_PASSWORD") if args.password_env else None
    if not password:
        password = getpass.getpass("Пароль: ")
    auth = MireaAuth()
    try:
        result = await auth.login(login, password)
        if result.challenge:
            code = input("Код из письма (OTP): ").strip()
            result = await auth.complete_2fa(result.challenge, code)
    finally:
        await auth.close()
    if not result.success or not result.cookies:
        print(f"Не удалось войти: {result.message}", file=sys.stderr)
        return 1
    store.save(result.cookies, login=login)
    print(f"Вошли. Сессия сохранена в {store.session_path}")
    return 0


async def cmd_logout(args, store: SessionStore) -> int:
    print("Сессия удалена." if store.clear() else "Сессии и так не было.")
    return 0


async def cmd_status(args, store: SessionStore) -> int:
    data = await A.status(store, check=args.check)
    _print_json(data) if args.json else _render_status(data)
    return 0 if data["logged_in"] else 1


async def cmd_schedule(args, store: SessionStore) -> int:
    data = await A.my_schedule(store, days=args.days)
    _print_json(data) if args.json else _render_my_schedule(data)
    return 0


async def cmd_grades(args, store: SessionStore) -> int:
    data = await A.grades(store)
    _print_json(data) if args.json else _render_grades(data)
    return 0


async def cmd_attendance(args, store: SessionStore) -> int:
    data = await A.attendance(store, args.discipline)
    _print_json(data) if args.json else _render_attendance(data)
    return 0


async def cmd_sdo(args, store: SessionStore) -> int:
    data = await A.sdo(store, limit=args.limit)
    _print_json(data) if args.json else _render_sdo(data)
    return 0


async def cmd_acs(args, store: SessionStore) -> int:
    data = await A.acs_today(store)
    _print_json(data) if args.json else _render_acs(data)
    return 0


async def cmd_attend(args, store: SessionStore) -> int:
    qr = args.qr or input("Ссылка из QR: ").strip()
    data = await A.mark_attendance(store, qr)
    if args.json:
        _print_json(data)
    else:
        print(("✓ " if data["success"] else "✗ ") + data["message"])
    return 0 if data["success"] else 1


async def cmd_search(args, store: SessionStore) -> int:
    data = await A.search(store, args.kind, args.query)
    _print_json(data) if args.json else _render_search(data)
    return 0


async def cmd_timetable(args, store: SessionStore) -> int:
    if args.ics:
        sched = await A.fetch_entity_schedule(store, args.kind, args.query)
        text = sched.to_ics(calendar_name=sched.entity_name or args.query)
        if args.ics == "-":
            sys.stdout.write(text)
        else:
            with open(args.ics, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            print(f"{len(sched.events)} событий → {args.ics}")
        return 0
    date_from = None
    if args.date:
        date_from = datetime.fromisoformat(args.date).replace(tzinfo=A.MSK)
    data = await A.entity_schedule(store, args.kind, args.query, days=args.days or None, date_from=date_from)
    _print_json(data) if args.json else _render_timetable(data)
    return 0


def cmd_mcp(args, store: SessionStore) -> int:
    from .mcp_server import run

    run(store, transport=args.transport, host=args.host, port=args.port)
    return 0


# --- argparse ---


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="вывести результат в JSON")
    common.add_argument("--home", help="каталог хранилища сессии (по умолчанию ~/.config/pymirea)")

    p = argparse.ArgumentParser(prog="pymirea", description="CLI для Пульса МИРЭА")
    p.add_argument("--version", action="version", version=f"pymirea {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="КОМАНДА")

    s = sub.add_parser("login", parents=[common], help="войти (пароль не сохраняется)")
    s.add_argument("login", nargs="?", help="логин, например ivanov.i.i@edu.mirea.ru")
    s.add_argument("--password-env", action="store_true", help="взять пароль из MIREA_PASSWORD")
    s.set_defaults(func=cmd_login)

    s = sub.add_parser("logout", parents=[common], help="удалить сохранённую сессию")
    s.set_defaults(func=cmd_logout)

    s = sub.add_parser("status", parents=[common], help="состояние сохранённой сессии")
    s.add_argument("--check", action="store_true", help="проверить сессию запросом к Пульсу")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("schedule", parents=[common], help="своё расписание из Пульса")
    s.add_argument("-d", "--days", type=int, default=7, help="на сколько дней вперёд (1-31)")
    s.set_defaults(func=cmd_schedule)

    s = sub.add_parser("grades", parents=[common], help="баллы БРС")
    s.set_defaults(func=cmd_grades)

    s = sub.add_parser("attendance", parents=[common], help="посещаемость по дисциплине")
    s.add_argument("discipline", help="discipline_id или часть названия предмета")
    s.set_defaults(func=cmd_attendance)

    s = sub.add_parser("sdo", parents=[common], help="СДО: курсы, дедлайны, оценки (Moodle)")
    s.add_argument("--limit", type=int, default=50, help="сколько дедлайнов максимум")
    s.set_defaults(func=cmd_sdo)

    s = sub.add_parser("acs", parents=[common], help="проходы через турникеты за сегодня")
    s.set_defaults(func=cmd_acs)

    s = sub.add_parser("attend", parents=[common], help="отметиться на паре по ссылке из QR")
    s.add_argument("qr", nargs="?", help="ссылка из QR-кода")
    s.set_defaults(func=cmd_attend)

    s = sub.add_parser("search", parents=[common], help="поиск группы/преподавателя/аудитории")
    s.add_argument("kind", choices=A.ENTITY_KINDS)
    s.add_argument("query")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("timetable", parents=[common], help="публичное расписание группы/преподавателя/аудитории")
    s.add_argument("kind", choices=A.ENTITY_KINDS)
    s.add_argument("query", help="название, например ЭФБО-01-24")
    s.add_argument("-d", "--days", type=int, default=7, help="сколько дней показать (0 — всё)")
    s.add_argument("--date", help="начальная дата YYYY-MM-DD (по умолчанию сегодня)")
    s.add_argument("--ics", metavar="FILE", help="выгрузить всё расписание в .ics (- для stdout)")
    s.set_defaults(func=cmd_timetable)

    s = sub.add_parser("mcp", parents=[common], help="запустить MCP-сервер")
    s.add_argument("--transport", choices=["stdio", "streamable-http", "sse"], default="stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    store = SessionStore(args.home)
    try:
        if asyncio.iscoroutinefunction(args.func):
            return asyncio.run(args.func(args, store))
        return args.func(args, store)
    except NotLoggedIn as e:
        print(str(e), file=sys.stderr)
        return 2
    except A.MireaAppError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
