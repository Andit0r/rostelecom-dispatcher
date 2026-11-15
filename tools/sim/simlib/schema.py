"""Ручная проверка форм ответов по схемам data/api_spec.yaml (имена полей, типы, enum).

Независима от pydantic-моделей сервера. Дополнительные поля допускаются (спецификация, п. 7),
обязательные поля и типы - строго. Спецификация описана декларативно:
  "str" | "int" | "num" | "bool" | "dt" | "date" | "any"
  ("enum", [..])  ("arr", spec)  ("nullable", spec)  ("obj", {поле: spec}, [обязательные])
"""

from __future__ import annotations

import datetime as dt
import re

DT = "dt"


def enum(*vals):
    return ("enum", list(vals))


def arr(spec):
    return ("arr", spec)


def nullable(spec):
    return ("nullable", spec)


def obj(fields: dict, required: list[str]):
    return ("obj", fields, required)


EQUIPMENT = obj({"ONT": "int", "ROUTER": "int", "STB": "int"}, ["ONT", "ROUTER", "STB"])
WINDOW = obj({"start": DT, "end": DT}, ["start", "end"])

STOP = obj({
    "seq": "int", "type": enum("DEPOT_START", "VISIT", "BREAK", "RESTOCK", "DEPOT_END"),
    "location_id": "str", "order_id": nullable("str"), "arrival": DT, "start": DT, "end": DT,
    "travel_sec": "int", "distance_m": "int", "status": enum("PLANNED", "IN_PROGRESS", "DONE"),
    "equipment_after": EQUIPMENT, "explanation": "str",
}, ["seq", "type", "location_id", "arrival", "start", "end"])

ROUTE = obj({
    "staff_id": "str", "service_center_id": "str", "status": enum("ACTIVE", "UNAVAILABLE"),
    "unavailable_from": nullable(DT), "unavailable_until": nullable(DT), "stops": arr(STOP),
    "totals": obj({"visits": "int", "travel_sec": "int", "distance_m": "int", "work_min": "int",
                   "utilization": "num"}, []),
}, ["staff_id", "service_center_id", "status", "stops"])

UNASSIGNED = obj({
    "order_id": "str",
    "reason_code": enum("NO_QUALIFIED_STAFF", "NO_SHIFT_TIME", "SLOT_UNREACHABLE", "NO_EQUIPMENT",
                        "STAFF_UNAVAILABLE", "OTHER"),
    "reason_text": "str", "suggested_action": enum("RESCHEDULE", "REASSIGN", "ESCALATE"),
    "proposed_slot": nullable(WINDOW), "since": nullable(DT),
}, ["order_id", "reason_code", "reason_text", "suggested_action"])

PGP = obj({
    "group_id": "str", "access_node_id": "str", "order_ids": arr("str"), "detected_at": DT,
    "action": enum("TRANSFER_TO_STATION"), "comment": "str",
}, ["group_id", "access_node_id", "order_ids", "detected_at", "action"])

CANCELLED = obj({"order_id": "str", "cancelled_at": DT, "event_id": nullable("str")},
                ["order_id", "cancelled_at"])

ALERT = obj({
    "alert_id": "str", "created_at": DT, "type": enum("LATE_RISK", "PGP_DETECTED", "UNASSIGNED"),
    "status": enum("ACTIVE", "RESOLVED"), "resolved_at": nullable(DT), "order_id": nullable("str"),
    "group_id": nullable("str"), "staff_id": nullable("str"), "forecast_start": nullable(DT),
    "lateness_min": nullable("int"),
    "suggested_action": enum("REASSIGN", "RESCHEDULE", "TRANSFER_TO_STATION", "NONE"),
    "suggested_staff_id": nullable("str"), "proposed_slot": nullable(WINDOW), "applied": "bool",
    "message": "str",
}, ["alert_id", "created_at", "type", "status", "suggested_action", "message"])

PLAN = obj({
    "date": "date", "plan_version": "int", "based_on_event_id": nullable("str"), "as_of": DT,
    "computation_ms": "int", "routes": arr(ROUTE), "unassigned": arr(UNASSIGNED), "pgp_groups": arr(PGP),
    "cancelled": arr(CANCELLED), "alerts": arr(ALERT),
    "summary": obj({"orders_total": "int", "visits_planned": "int", "unassigned": "int", "pgp_orders": "int",
                    "cancelled": "int", "travel_sec_total": "int", "utilization_cv": "num"}, []),
}, ["date", "plan_version", "as_of", "routes", "unassigned", "pgp_groups", "cancelled", "alerts"])

EVENT_RESULT = obj({
    "event_id": "str", "accepted": "bool", "duplicate": "bool", "plan_version": "int", "as_of": DT,
    "processing_ms": "int", "changed_staff_ids": arr("str"), "new_alerts": arr(ALERT), "message": "str",
}, ["event_id", "accepted", "duplicate", "plan_version", "as_of"])

HEALTH = obj({"status": enum("ok"), "team": "str", "version": "str"}, ["status"])
LOAD_RESULT = obj({"status": enum("loaded"), "counts": ("map", "int")}, ["status"])
ERROR = obj({"error": obj({"code": "str", "message": "str",
                           "details": arr(obj({"field": "str", "message": "str"}, ["message"]))},
                          ["code", "message"])}, ["error"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_dt(v: str) -> bool:
    try:
        return dt.datetime.fromisoformat(v).tzinfo is not None
    except ValueError:
        return False


def validate(value, spec, path: str = "$", out: list[str] | None = None, limit: int = 25) -> list[str]:
    """Возвращает список ошибок (пустой - соответствует схеме)."""
    errs = out if out is not None else []
    if len(errs) >= limit:
        return errs
    if isinstance(spec, str):
        ok = True
        if spec == "str":
            ok = isinstance(value, str)
        elif spec == "int":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif spec == "num":
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif spec == "bool":
            ok = isinstance(value, bool)
        elif spec == "dt":
            ok = isinstance(value, str) and _parse_dt(value)
        elif spec == "date":
            ok = isinstance(value, str) and bool(_DATE_RE.match(value))
        if not ok:
            errs.append(f"{path}: ожидается {spec}, получено {type(value).__name__} {str(value)[:40]!r}")
        return errs
    kind = spec[0]
    if kind == "nullable":
        if value is not None:
            validate(value, spec[1], path, errs, limit)
    elif kind == "enum":
        if value not in spec[1]:
            errs.append(f"{path}: значение {value!r} не из enum {spec[1]}")
    elif kind == "arr":
        if not isinstance(value, list):
            errs.append(f"{path}: ожидается массив")
        else:
            for i, v in enumerate(value):
                validate(v, spec[1], f"{path}[{i}]", errs, limit)
    elif kind == "map":
        if not isinstance(value, dict):
            errs.append(f"{path}: ожидается объект")
        else:
            for k, v in value.items():
                validate(v, spec[1], f"{path}.{k}", errs, limit)
    elif kind == "obj":
        if not isinstance(value, dict):
            errs.append(f"{path}: ожидается объект")
        else:
            for r in spec[2]:
                if r not in value:
                    errs.append(f"{path}: нет обязательного поля {r}")
            for k, sub in spec[1].items():
                if k in value:
                    validate(value[k], sub, f"{path}.{k}", errs, limit)
    return errs
