"""Общий каркас сценариев: контекст, модель дня на стороне клиента, независимая проверка плана.

Единственное, что берётся из кода сервера, - модели pydantic и validator.py (H1-H10) для проверки
результата; состояние дня симулятор ведёт сам (по отправленным событиям), а не читает из сервера.
"""

from __future__ import annotations

import datetime as dt
import random
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import schema
from .client import ApiClient, Resp
from .data import SynthData, TZ, iso, parse_ts
from .report import ScenarioResult

_ROOT = __import__("pathlib").Path(__file__).resolve().parents[3]
if str(_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(_ROOT / "backend"))

from pydantic import TypeAdapter  # noqa: E402

from app.models import (  # noqa: E402
    Event, Order, Plan, ReferenceData, TravelMatrix,
)
from app.planner.validator import validate_plan, validate_transition  # noqa: E402
from app.state import DayState, MatrixState, ReferenceState  # noqa: E402

EVENT_ADAPTER = TypeAdapter(Event)
EVENT_LIMIT_SEC = 10.0


def at(date: str, hhmm: str, sec: int = 0) -> dt.datetime:
    h, m = hhmm.split(":")
    return dt.datetime.fromisoformat(date).replace(tzinfo=TZ) + dt.timedelta(hours=int(h), minutes=int(m), seconds=sec)


# --------------------------------------------------------------------------- контекст

class Ctx:
    def __init__(self, client: ApiClient, data: SynthData, seed: int = 1, scale: float = 1.0,
                 time_limit: int = 60, quick: bool = False, options: dict | None = None) -> None:
        self.client, self.data, self.seed, self.scale = client, data, seed, scale
        self.time_limit, self.quick = time_limit, quick
        self.options = options or {}
        self._checker: PlanChecker | None = None
        self.reference_loaded = False

    def rng(self, name: str) -> random.Random:
        return random.Random(f"{self.seed}:{name}")

    @property
    def checker(self) -> "PlanChecker":
        if self._checker is None:
            self._checker = PlanChecker(self.data)
        return self._checker

    def ensure_reference(self, res: ScenarioResult) -> bool:
        """PUT reference и travel-matrix (один раз за прогон; повторная загрузка стёрла бы дни)."""
        if self.reference_loaded:
            return True
        c = self.client
        r = c.put_reference(self.data.reference)
        ok = res.check(r.ok, "PUT /reference вернул не 200", f"{r.status} {r.text or r.data}", "contract", "reference")
        if ok:
            errs = schema.validate(r.data, schema.LOAD_RESULT)
            res.check(not errs, "PUT /reference: ответ не LoadResult", "; ".join(errs), "contract", "reference")
        r = c.put_matrix(self.data.matrix_raw)
        ok2 = res.check(r.ok, "PUT /reference/travel-matrix вернул не 200", f"{r.status} {r.text or r.data}",
                        "contract", "matrix")
        if ok2:
            errs = schema.validate(r.data, schema.LOAD_RESULT)
            res.check(not errs, "PUT travel-matrix: ответ не LoadResult", "; ".join(errs), "contract", "matrix")
        self.reference_loaded = ok and ok2
        return self.reference_loaded


# --------------------------------------------------------------------------- модель дня

class DayModel:
    """Состояние дня по версии клиента: наряды, отмены, концы визитов из событий, недоступность."""

    def __init__(self, date: str, now: dt.datetime, orders: list[dict], absent: list[str] | None = None) -> None:
        self.date = date
        self.day = dt.date.fromisoformat(date)
        self.now = now
        self.orders: dict[str, dict] = {o["order_id"]: o for o in orders}
        self.absent = set(absent or [])
        self.arrived_at = {oid: now for oid in self.orders}
        self.overrides: dict[str, dt.datetime] = {}
        self.cancelled: dict[str, tuple[dt.datetime, str]] = {}
        self.transferred: dict[str, dt.datetime] = {}
        self.unavail: dict[str, tuple[dt.datetime, dt.datetime | None]] = {}
        self.model_time = now
        self.version = 1
        self.event_ids: set[str] = set()
        self._order_cache: dict[str, Order] = {}

    def apply(self, ev: dict) -> None:
        ts = parse_ts(ev["timestamp"])
        t = ev["type"]
        if t == "NEW_ORDER":
            o = ev["order"]
            self.orders[o["order_id"]] = o
            self.arrived_at[o["order_id"]] = ts
        elif t == "ORDER_CANCELLED":
            self.cancelled[ev["order_id"]] = (ts, ev["event_id"])
        elif t == "VISIT_DELAYED":
            self.overrides[ev["order_id"]] = parse_ts(ev["expected_end"])
        elif t == "ORDER_TRANSFERRED":
            end = parse_ts(ev["actual_end"])
            self.overrides[ev["order_id"]] = end
            self.transferred[ev["order_id"]] = end
        elif t == "STAFF_UNAVAILABLE":
            until = parse_ts(ev["until"]) if ev.get("until") else None
            self.unavail[ev["staff_id"]] = (ts, until)
        self.model_time = max(self.model_time, ts)
        self.event_ids.add(ev["event_id"])
        self.version += 1

    def state(self) -> DayState:
        orders = {}
        for oid, o in self.orders.items():
            m = self._order_cache.get(oid)
            if m is None:
                m = self._order_cache[oid] = Order.model_validate(o)
            orders[oid] = m
        return DayState(date=self.day, now=self.now, orders=orders, absent_staff_ids=set(self.absent),
                        arrived_at=dict(self.arrived_at), visit_end_overrides=dict(self.overrides))


# --------------------------------------------------------------------------- проверка плана

class PlanChecker:
    def __init__(self, data: SynthData) -> None:
        rd = ReferenceData.model_validate(data.reference)
        buildings = {b.building_id: b for n in rd.access_nodes for b in n.buildings}
        self.ref = ReferenceState(
            service_centers={s.service_center_id: s for s in rd.service_centers},
            staff={s.staff_id: s for s in rd.staff},
            access_nodes={n.access_node_id: n for n in rd.access_nodes},
            buildings=buildings,
            work_types={w.code.value if hasattr(w.code, "value") else w.code: w for w in rd.work_types},
        )
        tm = TravelMatrix.model_validate_json(data.matrix_raw)
        self.matrix = MatrixState(matrix=tm, index={p: i for i, p in enumerate(tm.points)})
        self.on_shift = [s for s in self.ref.staff]

    def check(self, res: ScenarioResult, model: DayModel, plan_json: Any, prev: Plan | None,
              event: dict | None, step: Any, expected_version: int | None = None,
              forced: set[str] | None = None) -> Plan | None:
        """forced - сотрудники, по которым сервер сам сообщил «неустранимо» (H2/H3 вынуждены задержкой визита)."""
        errs = schema.validate(plan_json, schema.PLAN)
        res.schema_check("Plan", errs, step)
        try:
            plan = Plan.model_validate(plan_json)
        except Exception as exc:  # noqa: BLE001
            res.check(False, "Plan: не разбирается моделью", str(exc)[:300], "contract", step)
            return None
        state = model.state()
        vs = list(validate_plan(plan, self.ref, self.matrix, state, joint=False))
        if prev is not None:
            ev = EVENT_ADAPTER.validate_python(event) if event else None
            t = ev.timestamp if ev is not None else model.model_time
            vs += validate_transition(prev, plan, self.ref, self.matrix, state, t, ev)
        res.checks += 1
        hard = [v for v in vs if not (forced and v.code in ("H2", "H3") and v.staff_id in forced)]
        if hard:
            res.failed += 1
        for v in vs:
            if forced and v.code in ("H2", "H3") and v.staff_id in forced:
                res.add("note", f"{v.code} вынуждено событием (сервер сообщил «неустранимо: {v.staff_id}»)", str(v), step)
                continue
            res.violations_seen.add((v.code, v.staff_id, v.order_id))
            res.metrics["violations_total"] = res.metrics.get("violations_total", 0) + 1
            res.add("violation", v.code, str(v), step)
        self._extra(res, model, plan, prev, event, step, expected_version)
        return plan

    def _extra(self, res: ScenarioResult, model: DayModel, plan: Plan, prev: Plan | None,
               event: dict | None, step: Any, expected_version: int | None) -> None:
        chk = res.check
        if expected_version is not None:
            chk(plan.plan_version == expected_version, "plan_version не равен ожидаемому",
                f"{plan.plan_version} != {expected_version}", "contract", step)
        chk(plan.date == model.day, "Plan.date не совпадает с днём", f"{plan.date}", "contract", step)
        if event is not None:
            chk(plan.as_of == parse_ts(event["timestamp"]), "Plan.as_of не равен timestamp события",
                f"{plan.as_of} vs {event['timestamp']}", "contract", step)
            chk(plan.based_on_event_id == event["event_id"], "based_on_event_id не равен event_id",
                f"{plan.based_on_event_id!r} vs {event['event_id']!r}", "contract", step)
        ids = [a.alert_id for a in plan.alerts]
        chk(len(ids) == len(set(ids)), "alert_id не уникальны в пределах дня", "", "contract", step)
        if prev is not None:
            pids = [a.alert_id for a in prev.alerts]
            chk(ids[:len(pids)] == pids, "список alerts не только пополняется (удалён/переставлен)",
                f"было {len(pids)}, стало {len(ids)}", "contract", step)
        for a in plan.alerts:
            if a.type.value in ("LATE_RISK", "UNASSIGNED"):
                chk(bool(a.order_id), f"{a.type.value}: нет обязательного order_id", a.alert_id, "contract", step)
            if a.type.value == "PGP_DETECTED":
                chk(bool(a.group_id), "PGP_DETECTED: нет обязательного group_id", a.alert_id, "contract", step)
            if a.status.value == "RESOLVED":
                chk(a.resolved_at is not None, "RESOLVED без resolved_at", a.alert_id, "note", step)
            chk(a.created_at <= plan.as_of + dt.timedelta(seconds=1), "alert создан позже as_of плана",
                f"{a.alert_id} {a.created_at} > {plan.as_of}", "contract", step)
        for g in plan.pgp_groups:
            chk(len(g.order_ids) >= 2, "ПГП-группа короче 2 нарядов (minItems 2)", g.group_id, "contract", step)
        routes = {r.staff_id: r for r in plan.routes}
        in_route = {}
        for r in plan.routes:
            for s in r.stops:
                if s.type.value == "VISIT":
                    in_route[s.order_id] = (r.staff_id, s)
        # недоступные не получают остановок в период недоступности
        for sid, (frm, until) in model.unavail.items():
            r = routes.get(sid)
            if r is None:
                continue
            chk(r.status.value == "UNAVAILABLE", "сотрудник из STAFF_UNAVAILABLE не помечен UNAVAILABLE",
                sid, "contract", step)
            if r.status.value == "UNAVAILABLE":
                chk(r.unavailable_from == frm, "unavailable_from не равен timestamp события",
                    f"{sid}: {r.unavailable_from} vs {frm}", "contract", step)
                chk((r.unavailable_until == until) or (until is None and r.unavailable_until is None),
                    "unavailable_until не равен until события", f"{sid}: {r.unavailable_until} vs {until}",
                    "contract", step)
            bad = [s for s in r.stops if frm <= s.start and (until is None or s.start < until)]
            chk(not bad, "у недоступного сотрудника остановка начинается в период недоступности",
                f"{sid}: {[(s.type.value, s.order_id, s.start.isoformat()) for s in bad][:3]}", "invariant", step)
        unassigned = {u.order_id for u in plan.unassigned}
        pgp = {o for g in plan.pgp_groups for o in g.order_ids}
        cancelled_ids = {c.order_id for c in plan.cancelled}
        for oid, (ts, ev_id) in model.cancelled.items():
            started = oid in in_route and in_route[oid][1].start < ts
            chk(oid in cancelled_ids or started, "отменённый наряд не в cancelled", oid, "invariant", step)
            chk(not (oid in in_route and not started), "отменённый наряд остался в маршруте",
                oid, "invariant", step)
            chk(oid not in unassigned and oid not in pgp, "отменённый наряд в unassigned/ПГП", oid, "invariant", step)
        for c in plan.cancelled:
            if c.order_id in model.cancelled:
                ts, ev_id = model.cancelled[c.order_id]
                chk(c.cancelled_at == ts, "cancelled_at не равен timestamp отмены", c.order_id, "contract", step)
                chk(c.event_id in (None, ev_id), "cancelled.event_id не равен event_id", c.order_id, "contract", step)
        for oid, end in model.transferred.items():
            chk(oid in in_route, "переданный (ORDER_TRANSFERRED) наряд пропал из маршрута", oid, "invariant", step)
            if oid in in_route:
                chk(abs((in_route[oid][1].end - end).total_seconds()) <= 1,
                    "end визита не равен actual_end из ORDER_TRANSFERRED", oid, "invariant", step)
            chk(oid not in unassigned and oid not in pgp, "переданный наряд не закрыт (unassigned/ПГП)", oid,
                "invariant", step)
        s = plan.summary
        if s is not None:
            visits = len(in_route)
            chk((s.orders_total in (None, len(model.orders))) and (s.visits_planned in (None, visits))
                and (s.unassigned in (None, len(unassigned))) and (s.cancelled in (None, len(cancelled_ids))),
                "summary не совпадает с планом (справочно)",
                f"{s.model_dump()} vs orders={len(model.orders)} visits={visits} unassigned={len(unassigned)} "
                f"cancelled={len(cancelled_ids)}", "note", step)


def plan_stats(plan: Plan) -> dict:
    visits = sum(1 for r in plan.routes for s in r.stops if s.type.value == "VISIT")
    reasons: dict[str, int] = {}
    for u in plan.unassigned:
        reasons[u.reason_code.value] = reasons.get(u.reason_code.value, 0) + 1
    alerts: dict[str, int] = {}
    for a in plan.alerts:
        alerts[a.type.value] = alerts.get(a.type.value, 0) + 1
    return {"version": plan.plan_version, "visits": visits, "unassigned": len(plan.unassigned),
            "reasons": reasons, "pgp_orders": sum(len(g.order_ids) for g in plan.pgp_groups),
            "pgp_groups": len(plan.pgp_groups), "cancelled": len(plan.cancelled), "alerts": alerts}


def visits_of(plan: Plan) -> dict[str, tuple[str, Any]]:
    return {s.order_id: (r.staff_id, s) for r in plan.routes for s in r.stops if s.type.value == "VISIT"}


# --------------------------------------------------------------------------- сессия дня

class DaySession:
    """Один загруженный день: PUT day, POST plan, затем события с полной проверкой после каждого."""

    def __init__(self, ctx: Ctx, res: ScenarioResult, date: str, now: dt.datetime, orders: list[dict],
                 absent: list[str] | None = None, time_limit: int | None = None) -> None:
        self.ctx, self.res, self.date, self.now = ctx, res, date, now
        self.client = ctx.client
        self.model = DayModel(date, now, orders, absent)
        self.time_limit = time_limit or ctx.time_limit
        self.plan: Plan | None = None
        self.plan_json: Any = None
        self.step = 0
        self.last_result: dict | None = None
        self.event_counter = 0
        self.forced: set[str] = set()
        self.lock = threading.Lock()

    def next_event_id(self, prefix: str = "EV") -> str:
        self.event_counter += 1
        return f"{prefix}-{self.date.replace('-', '')}-S{self.ctx.seed}-{self.event_counter:04d}"

    # ---- загрузка и первоначальный план
    def setup(self) -> bool:
        res, c = self.res, self.client
        if not self.ctx.ensure_reference(res):
            return False
        body = {"date": self.date, "now": iso(self.now), "orders": list(self.model.orders.values())}
        if self.model.absent:
            body["absent_staff_ids"] = sorted(self.model.absent)
        r = c.put_day(self.date, body)
        if not res.check(r.ok, "PUT /days/{date} вернул не 200", f"{r.status} {r.text or r.data}", "contract", "put_day"):
            return False
        errs = schema.validate(r.data, schema.LOAD_RESULT)
        res.schema_check("LoadResult (PUT day)", errs, "put_day")
        res.check(not errs and r.data.get("counts", {}).get("orders") == len(self.model.orders),
                  "PUT /days: LoadResult.counts.orders не равен числу загруженных нарядов",
                  f"{r.data}", "contract", "put_day")
        r = c.post_plan(self.date, self.time_limit)
        if not res.check(r.ok, "POST /plan вернул не 200", f"{r.status} {r.text or r.data}", "contract", "post_plan"):
            return False
        res.check(r.elapsed <= self.time_limit + 1.0, "POST /plan дольше time_limit_sec",
                  f"{r.elapsed:.1f} с > {self.time_limit} с", "latency", "post_plan")
        self.plan_json = r.data
        self.plan = self.ctx.checker.check(res, self.model, r.data, None, None, "post_plan", expected_version=1)
        if self.plan is not None:
            res.check(self.plan.based_on_event_id is None, "первоначальный план: based_on_event_id не null",
                      str(self.plan.based_on_event_id), "contract", "post_plan")
            res.check(self.plan.as_of == self.now, "первоначальный план: as_of != now", str(self.plan.as_of),
                      "contract", "post_plan")
        return self.plan is not None

    def fetch_plan(self, step: Any = None) -> Resp:
        r = self.client.get_plan(self.date)
        self.res.check(r.ok, "GET /plan вернул не 200", f"{r.status} {r.text or r.data}", "contract", step)
        return r

    # ---- события
    def send(self, body: dict, *, label: str = "POST event", check: bool = True,
             expect: int = 200) -> Resp:
        """Отправляет событие; при 200 обновляет модель, забирает GET plan и проверяет H1-H10 и инварианты."""
        res = self.res
        self.step += 1
        res.events += 1
        step = f"#{self.step} {body.get('type')} {body.get('event_id')}"
        r = self.client.post_event(self.date, body, label)
        if r.status != expect:
            if expect == 200:
                res.check(False, f"событие {body.get('type')} отклонено",
                          f"HTTP {r.status}: {r.text or r.data}", "contract", step)
            else:
                res.check(False, f"ожидался HTTP {expect}", f"HTTP {r.status}: {r.text or r.data}", "contract", step)
            return r
        if expect != 200:
            return r
        res.check(r.elapsed <= EVENT_LIMIT_SEC, "событие обработано дольше 10 с",
                  f"{r.elapsed:.2f} с ({body['type']})", "latency", step)
        errs = schema.validate(r.data, schema.EVENT_RESULT)
        res.schema_check("EventResult", errs, step)
        d = r.data
        if not isinstance(d, dict) or not {"event_id", "accepted", "duplicate", "plan_version"} <= set(d):
            return r
        res.check(d["accepted"] is True and d["event_id"] == body["event_id"], "EventResult.accepted/event_id",
                  str(d)[:200], "contract", step)
        if d["duplicate"]:
            res.check(False, "новое событие помечено duplicate=true", str(d)[:200], "contract", step)
            return r
        prev = self.plan
        self.model.apply(body)
        res.check(d["plan_version"] == self.model.version, "EventResult.plan_version не равен предыдущей версии + 1",
                  f"{d['plan_version']} != {self.model.version}", "contract", step)
        self.model.version = d["plan_version"]  # продолжаем с версией сервера, чтобы не плодить каскад ошибок
        for a in d.get("new_alerts") or []:
            res.metrics.setdefault("new_alerts_by_type", {})
            m = res.metrics["new_alerts_by_type"]
            m[a["type"]] = m.get(a["type"], 0) + 1
        self.last_result = d
        self.forced |= set(re.findall(r"неустранимо: (ST-\d+)", d.get("message") or ""))
        gp = self.fetch_plan(step)
        if gp.ok and check:
            plan = self.ctx.checker.check(res, self.model, gp.data, prev, body, step, expected_version=d["plan_version"],
                                          forced=self.forced)
            if plan is not None:
                self.plan, self.plan_json = plan, gp.data
        elif gp.ok:
            self.plan = Plan.model_validate(gp.data)
            self.plan_json = gp.data
        return r

    # ---- шаблоны событий
    def tick(self, ts: dt.datetime) -> dict:
        return {"event_id": self.next_event_id("TK"), "type": "TIME_TICK", "timestamp": iso(ts)}

    def new_order(self, ts: dt.datetime, order: dict) -> dict:
        return {"event_id": self.next_event_id("NO"), "type": "NEW_ORDER", "timestamp": iso(ts), "order": order}

    def cancel(self, ts: dt.datetime, order_id: str, reason: str = "CUSTOMER_REQUEST") -> dict:
        return {"event_id": self.next_event_id("CX"), "type": "ORDER_CANCELLED", "timestamp": iso(ts),
                "order_id": order_id, "reason": reason}

    def delayed(self, ts: dt.datetime, staff_id: str, order_id: str, expected_end: dt.datetime) -> dict:
        return {"event_id": self.next_event_id("VD"), "type": "VISIT_DELAYED", "timestamp": iso(ts),
                "staff_id": staff_id, "order_id": order_id, "expected_end": iso(expected_end)}

    def transferred(self, ts: dt.datetime, staff_id: str, order_id: str, kind: str = "STATION_EQUIPMENT") -> dict:
        return {"event_id": self.next_event_id("OT"), "type": "ORDER_TRANSFERRED", "timestamp": iso(ts),
                "staff_id": staff_id, "order_id": order_id, "actual_end": iso(ts), "transferred_to": kind}

    def unavailable(self, ts: dt.datetime, staff_id: str, until: dt.datetime | None, reason: str = "ILLNESS") -> dict:
        body = {"event_id": self.next_event_id("SU"), "type": "STAFF_UNAVAILABLE", "timestamp": iso(ts),
                "staff_id": staff_id, "reason": reason}
        if until is not None:
            body["until"] = iso(until)
        return body

    def unstarted_orders(self, t: dt.datetime) -> list[str]:
        """Наряды из маршрутов, визит которых ещё не начат на момент t."""
        if self.plan is None:
            return []
        return [oid for oid, (_, s) in visits_of(self.plan).items() if s.start > t]
