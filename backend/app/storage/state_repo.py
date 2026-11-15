"""Запись состояния сервиса для восстановления после рестарта (write-behind).

Горячий путь (POST /plan, события) только снимает дешёвую поверхностную копию DayState и ставит
операцию в очередь app/storage/db.py; сериализация и запросы — в фоновом потоке. Без БД все
функции ничего не делают. Чтение — только при старте (app/storage/restore.py).

Таблицы (migrations/0002_state.sql): inputs, service_state, day_state, marks, actions.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import zlib
from typing import Any

from .db import db


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _jsonb(value):
    from psycopg.types.json import Jsonb

    return Jsonb(value)


_plan_cache: tuple = (None, None)  # (объект плана, его JSON) — только поток записи


def plan_json(plan) -> dict:
    """model_dump плана с кэшем на один объект: журнал и снимок дня пишут один и тот же план подряд,
    сериализуем его один раз (меньше работы под GIL рядом с горячим путём)."""
    global _plan_cache
    if _plan_cache[0] is plan:
        return _plan_cache[1]
    data = plan.model_dump(mode="json")
    _plan_cache = (plan, data)
    return data


# ---------------------------------------------------------------- входные данные

def _write_input(conn, kind: str, raw: bytes, gen: str | None) -> None:
    sha = hashlib.sha256(raw).hexdigest()
    exists = conn.execute("SELECT 1 FROM inputs WHERE sha256 = %s", (sha,)).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO inputs (sha256, kind, size_bytes, payload) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (sha256) DO NOTHING",
            (sha, kind, len(raw), zlib.compress(raw, 6)),
        )
    value = {"sha256": sha}
    if gen is not None:
        value["gen"] = gen
    conn.execute(
        "INSERT INTO service_state (key, value, updated_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (kind, _jsonb(value)),
    )


def save_reference(raw: bytes, gen: str) -> None:
    """PUT /reference принят: тело и новое поколение справочников (дни прежнего не восстанавливаются)."""
    if not db.enabled:
        return
    db.submit(_write_input, "reference", bytes(raw), gen, coalesce_key=("input", "reference"))


def save_matrix(raw: bytes) -> None:
    if not db.enabled:
        return
    db.submit(_write_input, "matrix", bytes(raw), None, coalesce_key=("input", "matrix"))


# ---------------------------------------------------------------- снимок дня

def snapshot_day(day) -> dict[str, Any]:
    """Поверхностная копия под lock (микросекунды): объекты моделей после коммита не меняются,
    следующее событие работает на клоне дня."""
    return {
        "date": day.date,
        "load_id": day.load_id,
        "now": day.now,
        "orders": dict(day.orders),
        "absent_staff_ids": set(day.absent_staff_ids),
        "arrived_at": dict(day.arrived_at),
        "cancelled": dict(day.cancelled),
        "plan": day.plan,
        "plan_version": day.plan_version,
        "model_time": day.model_time,
        "events": dict(day.events),
        "visit_end_overrides": dict(day.visit_end_overrides),
    }


def dump_day(s: dict, planner_modes) -> dict:
    return {
        "format": 1,
        "date": s["date"].isoformat(),
        "load_id": s["load_id"],
        "now": _iso(s["now"]),
        "orders": [o.model_dump(mode="json", by_alias=True) for o in s["orders"].values()],
        "absent_staff_ids": sorted(s["absent_staff_ids"]),
        "arrived_at": {k: _iso(v) for k, v in s["arrived_at"].items()},
        "cancelled": {k: c.model_dump(mode="json", by_alias=True) for k, c in s["cancelled"].items()},
        "plan": plan_json(s["plan"]) if s["plan"] is not None else None,
        "plan_version": s["plan_version"],
        "model_time": _iso(s["model_time"]),
        "events": dict(s["events"]),
        "visit_end_overrides": {k: _iso(v) for k, v in s["visit_end_overrides"].items()},
        "planner_modes": list(planner_modes) if planner_modes is not None else None,
    }


def _write_day(conn, snap: dict, ref_gen: str | None, planner_modes, source: str) -> None:
    payload = dump_day(snap, planner_modes)
    plan = snap["plan"]
    conn.execute(
        'INSERT INTO day_state (date, load_id, ref_gen, scenario, "now", as_of, plan_version, payload, demo, '
        "updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NULL,now()) "
        "ON CONFLICT (date) DO UPDATE SET "
        "scenario = CASE WHEN day_state.load_id = EXCLUDED.load_id THEN day_state.scenario ELSE EXCLUDED.scenario END, "
        "demo = CASE WHEN day_state.load_id = EXCLUDED.load_id THEN day_state.demo ELSE NULL END, "
        'load_id = EXCLUDED.load_id, ref_gen = EXCLUDED.ref_gen, "now" = EXCLUDED."now", '
        "as_of = EXCLUDED.as_of, plan_version = EXCLUDED.plan_version, payload = EXCLUDED.payload, "
        "updated_at = now()",
        (snap["date"], snap["load_id"], ref_gen, source, snap["now"],
         plan.as_of if plan is not None else None, snap["plan_version"], _jsonb(payload)),
    )


def save_day(day, ref_gen: str | None, planner_modes=None, source: str = "api") -> None:
    """Последнее состояние дня (под lock вызывающего). Серия событий пишется одним снимком."""
    if not db.enabled:
        return
    db.submit(_write_day, snapshot_day(day), ref_gen, planner_modes, source, coalesce_key=("day", day.date))


# ---------------------------------------------------------------- демо-проигрыватель /ui

def dump_demo(demo: dict) -> dict:
    out = {k: v for k, v in demo.items() if k != "order_info"}  # order_info строится заново из файлов
    out["events"] = list(demo.get("events", []))
    out["triggers"] = list(demo.get("triggers", []))
    out["last_ts"] = _iso(demo.get("last_ts"))
    return out


def _write_demo(conn, date: dt.date, load_id: str, demo: dict) -> None:
    conn.execute(
        "UPDATE day_state SET demo = %s, scenario = %s, updated_at = now() WHERE date = %s AND load_id = %s",
        (_jsonb(demo), f"demo:{demo.get('scenario') or 'day'}", date, load_id),
    )


def save_demo(date: dt.date, load_id: str, demo: dict) -> None:
    if not db.enabled:
        return
    db.submit(_write_demo, date, load_id, dump_demo(demo), coalesce_key=("demo", date))


# ---------------------------------------------------------------- отметки сотрудников

def _write_mark(conn, load_id: str, date: dt.date, key: str, kind: str, mark: dict) -> None:
    ev = mark.get("event") or None
    conn.execute(
        'INSERT INTO marks (load_id, date, mark_key, kind, staff_id, order_id, "timestamp", recorded_at, author, '
        "event_sent, event_id, payload) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (load_id, mark_key, kind) DO UPDATE SET staff_id = EXCLUDED.staff_id, "
        '"timestamp" = EXCLUDED."timestamp", recorded_at = EXCLUDED.recorded_at, author = EXCLUDED.author, '
        "event_sent = EXCLUDED.event_sent, event_id = EXCLUDED.event_id, payload = EXCLUDED.payload",
        (load_id, date, key, kind, mark.get("staff_id"), None if key.startswith("@") else key,
         mark.get("at"), mark.get("recorded_at"), _jsonb(mark["author"]) if mark.get("author") else None,
         ev is not None, ev.get("event_id") if ev else None, _jsonb(mark)),
    )


def _delete_mark(conn, load_id: str, key: str, kind: str) -> None:
    conn.execute("DELETE FROM marks WHERE load_id = %s AND mark_key = %s AND kind = %s", (load_id, key, kind))


def save_mark(load_id: str, date: dt.date, key: str, kind: str, mark: dict) -> None:
    if db.enabled and load_id:
        db.submit(_write_mark, load_id, date, key, kind, dict(mark))


def delete_mark(load_id: str, key: str, kind: str) -> None:
    if db.enabled and load_id:
        db.submit(_delete_mark, load_id, key, kind)


# ---------------------------------------------------------------- сброс дня (/ui/day/reset)

def _write_reset(conn, days: list[tuple[dt.date, str]], with_reference: bool, info: dict,
                 only_date: dt.date | None = None) -> None:
    """Снимки дней и отметки текущих поколений удаляются — при рестарте день не восстановится.
    История (days/plan_versions/events/alerts) остаётся; факт сброса — в service_state["day_reset"].
    only_date — очищается только этот день (снимок day_state этой даты и отметки его load_id)."""
    if only_date is not None:
        conn.execute("DELETE FROM day_state WHERE date = %s", (only_date,))
    else:
        conn.execute("DELETE FROM day_state")  # в памяти дней не осталось — снимки всех дат неактуальны
    for _, load_id in days:
        if load_id:
            conn.execute("DELETE FROM marks WHERE load_id = %s", (load_id,))
    if with_reference:  # без указателей restore стартует пустым (тела в inputs не трогаем)
        conn.execute("DELETE FROM service_state WHERE key IN ('reference', 'matrix')")
    conn.execute(
        "INSERT INTO service_state (key, value, updated_at) VALUES ('day_reset', %s, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (_jsonb(info),),
    )


def save_reset(days: list[tuple[dt.date, str]], with_reference: bool, actor: str | None,
               only_date: dt.date | None = None) -> None:
    """Под lock вызывающего: встаёт в очередь после всех ранее поставленных снимков дней."""
    if not db.enabled:
        return
    info = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), "by": actor, "with_reference": with_reference,
            "days": [{"date": d.isoformat(), "load_id": lid} for d, lid in days],
            "only_date": only_date.isoformat() if only_date else None}
    db.submit(_write_reset, list(days), with_reference, info, only_date)


# ---------------------------------------------------------------- действия диспетчера

def _write_action(conn, ts: dt.datetime, actor, action: str, target, details) -> None:
    conn.execute(
        "INSERT INTO actions (ts, actor, action, target, details) VALUES (%s,%s,%s,%s,%s)",
        (ts, actor, action, target, _jsonb(details) if details is not None else None),
    )


def record_action(actor: str | None, action: str, target: str | None = None, details: dict | None = None) -> None:
    if db.enabled:
        db.submit(_write_action, dt.datetime.now(dt.timezone.utc), actor, action, target, details)
