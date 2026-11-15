"""Чтение истории из журнала PostgreSQL: /ui/history (вне контракта /api/v1).

Без БД эндпоинты отвечают 503 в формате ErrorResponse; остальной сервис не затрагивается.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Query, Request

from .errors import ApiError
from .journal import JournalUnavailable, journal

router = APIRouter(prefix="/ui/history", tags=["ui-history"])


def _query(sql: str, params: tuple = ()) -> list[dict]:
    try:
        return journal.query(sql, params)
    except JournalUnavailable as exc:
        raise ApiError(503, "JOURNAL_UNAVAILABLE", str(exc)) from None


def _date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ApiError(400, "BAD_REQUEST", f"Некорректная дата '{value}', ожидается YYYY-MM-DD") from None


@router.get("/days")
def history_days() -> dict:
    rows = _query(
        'SELECT d.date, d."now", d.loaded_at, d.orders_total, '
        "(SELECT max(version) FROM plan_versions p WHERE p.date = d.date) AS last_version "
        "FROM days d ORDER BY d.date DESC"
    )
    return {"days": rows}


@router.get("/{date}/versions")
def history_versions(date: str) -> dict:
    day = _date(date)
    rows = _query(
        "SELECT version, as_of, based_on_event_id, computation_ms, visits_planned, unassigned, "
        "pgp_orders, cancelled, late_risk_active, travel_sec_total, utilization_cv, utilization_profiles "
        "FROM plan_versions WHERE date = %s ORDER BY version",
        (day,),
    )
    return {"date": date, "versions": rows}


@router.get("/{date}/events")
def history_events(date: str, limit: int = Query(1000, ge=1, le=10000), offset: int = Query(0, ge=0)) -> dict:
    day = _date(date)
    rows = _query(
        "SELECT event_id, ts, type, payload, accepted, duplicate, processing_ms, plan_version "
        "FROM events WHERE date = %s ORDER BY plan_version, ts LIMIT %s OFFSET %s",
        (day, limit, offset),
    )
    return {"date": date, "events": rows}


@router.get("/{date}/plan/{version}")
def history_plan(date: str, version: int) -> dict:
    day = _date(date)
    rows = _query("SELECT plan FROM plan_versions WHERE date = %s AND version = %s", (day, version))
    if not rows:
        raise ApiError(404, "PLAN_NOT_FOUND", f"Версия {version} плана на {date} не найдена в журнале")
    return rows[0]["plan"]


@router.get("/storage")
def history_storage(request: Request) -> dict:
    """Состояние слоя хранения: подключение, версия схемы, очередь записи, итог восстановления при старте."""
    from .storage import db
    return {"storage": db.stats(), "restore": getattr(request.app.state, "restore_report", None)}


@router.get("/actions")
def history_actions(
    limit: int = Query(200, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    actor: str | None = Query(None, description="точный логин"),
    action: str | None = Query(None, description="подстрока «МЕТОД /шаблон/пути», без учёта регистра"),
    date_from: str | None = Query(None, description="YYYY-MM-DD, включительно (UTC)"),
    date_to: str | None = Query(None, description="YYYY-MM-DD, включительно (UTC)"),
) -> dict:
    """Журнал действий (изменяющие /ui/*; тела запросов и пароли не пишутся), новые сверху."""
    where, params = [], []
    if actor:
        where.append("actor = %s")
        params.append(actor)
    if action:
        where.append("action ILIKE %s")
        esc = action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f"%{esc}%")
    if date_from:
        where.append("ts >= %s")
        params.append(dt.datetime.combine(_date(date_from), dt.time.min, dt.timezone.utc))
    if date_to:
        where.append("ts < %s")
        params.append(dt.datetime.combine(_date(date_to) + dt.timedelta(days=1), dt.time.min, dt.timezone.utc))
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = _query(f"SELECT id, ts, actor, action, target, details FROM actions{clause} ORDER BY id DESC LIMIT %s OFFSET %s",
                  (*params, limit, offset))
    return {"actions": rows}
