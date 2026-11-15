"""Перепланирование по событиям (этап B) на маленькой фикстуре через HTTP.

Сценарии: заморозка H10 и её проверка validate_transition, VISIT_DELAYED → LATE_RISK
с передачей наряда, STAFF_UNAVAILABLE → передача нарядов и правила выбывшего,
ORDER_CANCELLED (начатый и неначатый визит), NEW_ORDER → ПГП с пополнением группы.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Plan, StopType
from app.planner.pyvrp_planner import PyVrpPlanner
from app.planner.validator import validate_plan, validate_transition
from app.store import Store

DAY = "2026-10-26"
D = dt.date.fromisoformat(DAY)
TZ = dt.timezone(dt.timedelta(hours=5))
SHIFT = {"start": "09:00", "end": "18:00", "break_duration_min": 60, "break_window": {"from": "12:00", "to": "14:00"}}
EQ = {"ONT": 3, "ROUTER": 3, "STB": 3}
CAP = {"ONT": 6, "ROUTER": 6, "STB": 4}
POINTS = ["SC-01", "SC-02", "B-00001", "B-00002", "B-00003", "B-00004", "B-00005"]


def at(hhmm: str) -> str:
    return f"{DAY}T{hhmm}:00+05:00"


def _staff(sid: str, sc: str) -> dict:
    return {"staff_id": sid, "service_center_id": sc, "profiles": ["LTP3"], "technologies": ["PON"],
            "height_permit": False, "shift": SHIFT, "equipment_start": EQ, "equipment_capacity": CAP}


def _reference() -> dict:
    sc = {"name": "СЦ", "address": "Екатеринбург", "lat": 56.85, "lon": 60.6, "restock_duration_min": 15,
          "opening_hours": {"open": "08:00", "close": "21:00"}}
    building = {"address": "Екатеринбург", "lat": 56.85, "lon": 60.6, "building_type": "MKD", "overhead_line": False}
    return {
        "service_centers": [{**sc, "service_center_id": "SC-01"}, {**sc, "service_center_id": "SC-02"}],
        "staff": [_staff("ST-001", "SC-01"), _staff("ST-002", "SC-02")],
        "access_nodes": [{"access_node_id": "AN-0001", "node_type": "OLT", "technology": "PON", "name": "OLT Тест-1",
                          "lat": 56.85, "lon": 60.6,
                          "buildings": [{**building, "building_id": b} for b in POINTS[2:]]}],
        "work_types": [{"code": "REPAIR", "name": "Ремонт", "profiles": ["LTP3"], "duration_min": 60,
                        "max_start_delay_min": None, "deadline_kind": "HOURS", "deadline_value": 24,
                        "repeat_deadline_value": 12, "pgp_relevant": True}],
    }


def _matrix() -> dict:
    # SC-01 рядом с домами (10 мин), SC-02 далеко (40 мин): оба наряда достаются ST-001
    n = len(POINTS)
    dur = [[0 if i == j else 600 for j in range(n)] for i in range(n)]
    for j in range(2, n):
        dur[1][j] = dur[j][1] = 2400
    return {"matrix_id": "test", "points": POINTS, "durations_sec": dur, "distances_m": dur}


def _order(n: int, building: str, slot_h: int, reg: str = "2026-10-25T10:00:00+05:00") -> dict:
    return {
        "order_id": f"ORD-20261026-{n:04d}", "work_type_code": "REPAIR", "technology": "PON",
        "building_id": building, "address": "Екатеринбург", "lat": 56.85, "lon": 60.6, "building_type": "MKD",
        "requires_height": False, "access_node_id": "AN-0001", "registered_at": reg,
        "slot": {"start": at(f"{slot_h:02d}:00"), "end": at(f"{slot_h + 2:02d}:00")},
        "latest_start": at(f"{slot_h + 2:02d}:00"), "deadline": at("23:00"),
        "required_equipment": {"ONT": 0, "ROUTER": 0, "STB": 0}, "is_repeat": False,
    }


@pytest.fixture(autouse=True)
def _fast_budget(monkeypatch):
    monkeypatch.setenv("PLANNER_DAY_BUDGET_SEC", "1")


class Day:
    """Обёртка над TestClient: загрузка, события, проверка H1–H10 на каждой версии."""

    def __init__(self, orders: list[dict]) -> None:
        self.app = create_app(planner=PyVrpPlanner(), store=Store())
        self.client = TestClient(self.app)
        c = self.client
        assert c.put("/api/v1/reference", json=_reference()).status_code == 200
        assert c.put("/api/v1/reference/travel-matrix", json=_matrix()).status_code == 200
        assert c.put(f"/api/v1/days/{DAY}", json={"date": DAY, "now": at("08:30"), "orders": orders}).status_code == 200
        r = c.post(f"/api/v1/days/{DAY}/plan", json={"time_limit_sec": 30})
        assert r.status_code == 200, r.text
        self.plan = Plan.model_validate(r.json())
        assert self.violations() == []
        self.n = 0

    @property
    def store(self) -> Store:
        return self.app.state.service.store

    def violations(self, prev: Plan | None = None, event: dict | None = None) -> list:
        day = self.store.days[D]
        out = validate_plan(self.plan, self.store.reference, self.store.matrix, day)
        if prev is not None:
            from pydantic import TypeAdapter

            from app.models import Event

            ev = TypeAdapter(Event).validate_python(event)
            out += validate_transition(prev, self.plan, self.store.reference, self.store.matrix, day, ev.timestamp, ev)
        return out

    def send(self, body: dict, status: int = 200) -> dict:
        self.n += 1
        body = {"event_id": f"EV-{self.n:04d}", **body}
        r = self.client.post(f"/api/v1/days/{DAY}/events", json=body)
        assert r.status_code == status, r.text
        if status != 200:
            return r.json()
        prev = self.plan
        self.plan = Plan.model_validate(self.client.get(f"/api/v1/days/{DAY}/plan").json())
        assert self.plan.plan_version == prev.plan_version + 1
        assert self.violations(prev, body) == []
        return r.json()

    def visits(self) -> dict[str, tuple[str, object]]:
        return {s.order_id: (r.staff_id, s) for r in self.plan.routes for s in r.stops if s.type == StopType.VISIT}

    def route(self, staff_id: str):
        return next(r for r in self.plan.routes if r.staff_id == staff_id)


def _two_visit_day() -> Day:
    day = Day([_order(1, "B-00001", 9), _order(2, "B-00002", 11)])
    v = day.visits()
    assert v["ORD-20261026-0001"][0] == v["ORD-20261026-0002"][0] == "ST-001"
    return day


def test_freeze_h10_and_transition_validator() -> None:
    day = _two_visit_day()
    v1 = day.visits()["ORD-20261026-0001"][1]
    day.send({"type": "TIME_TICK", "timestamp": at("09:30")})
    # начатый визит не изменился; новый наряд начинается не раньше своего timestamp
    assert day.visits()["ORD-20261026-0001"][1].start == v1.start
    new = _order(3, "B-00003", 9, reg=at("10:00"))
    new["registered_at"] = at("10:00")
    res = day.send({"type": "NEW_ORDER", "timestamp": at("10:00"), "order": new})
    sid, stop = day.visits()["ORD-20261026-0003"]
    assert stop.start >= dt.datetime.fromisoformat(at("10:00"))
    assert sid in res["changed_staff_ids"]

    # самопроверка ловит изменение начатой остановки и слишком раннее прибытие нового визита
    prev = day.plan
    tampered = prev.model_copy(deep=True)
    route = next(r for r in tampered.routes if r.staff_id == "ST-001")
    route.stops[1].start = route.stops[1].start + dt.timedelta(minutes=5)
    codes = [v.code for v in validate_transition(prev, tampered, day.store.reference, day.store.matrix,
                                                 day.store.days[D], dt.datetime.fromisoformat(at("10:30")))]
    assert "H10" in codes


def test_visit_delayed_reassigns_with_timely_late_risk() -> None:
    day = _two_visit_day()
    # визит по 0002 ещё не начат — VISIT_DELAYED противоречит плану
    day.send({"type": "VISIT_DELAYED", "timestamp": at("09:40"), "staff_id": "ST-001",
              "order_id": "ORD-20261026-0002", "expected_end": at("12:00")}, status=422)
    res = day.send({"type": "VISIT_DELAYED", "timestamp": at("09:40"), "staff_id": "ST-001",
                    "order_id": "ORD-20261026-0001", "expected_end": at("12:55")})
    v = day.visits()
    assert v["ORD-20261026-0001"][1].end == dt.datetime.fromisoformat(at("12:55"))
    assert v["ORD-20261026-0002"][0] == "ST-002"
    alert = next(a for a in res["new_alerts"] if a["order_id"] == "ORD-20261026-0002")
    assert alert["type"] == "LATE_RISK" and alert["suggested_action"] == "REASSIGN"
    assert alert["suggested_staff_id"] == "ST-002" and alert["applied"] is True
    assert alert["staff_id"] == "ST-001" and alert["lateness_min"] > 0
    # своевременно: создано не позднее чем за 30 минут до slot.start (11:00)
    assert dt.datetime.fromisoformat(alert["created_at"]) <= dt.datetime.fromisoformat(at("10:30"))
    assert {a.alert_id for a in day.plan.alerts} >= {a["alert_id"] for a in res["new_alerts"]}


def test_staff_unavailable_moves_orders_and_respects_period() -> None:
    day = _two_visit_day()
    res = day.send({"type": "STAFF_UNAVAILABLE", "timestamp": at("09:30"), "staff_id": "ST-001",
                    "until": None, "reason": "VEHICLE_BREAKDOWN"})
    route = day.route("ST-001")
    assert route.status == "UNAVAILABLE" and route.unavailable_until is None
    # начатый визит считается выполненным, остановок после выбытия нет (и DEPOT_END не нужен)
    assert [s.type for s in route.stops] == [StopType.DEPOT_START, StopType.VISIT]
    assert day.visits()["ORD-20261026-0002"][0] == "ST-002"
    assert any(a["type"] == "LATE_RISK" and a["order_id"] == "ORD-20261026-0002"
               and a["suggested_staff_id"] == "ST-002" for a in res["new_alerts"])
    assert set(res["changed_staff_ids"]) == {"ST-001", "ST-002"}


def test_staff_unavailable_with_until_continues_from_service_center() -> None:
    day = Day([_order(1, "B-00001", 9), _order(2, "B-00002", 15)])
    sid = day.visits()["ORD-20261026-0002"][0]
    day.send({"type": "STAFF_UNAVAILABLE", "timestamp": at("09:05"), "staff_id": sid,
              "until": at("13:00"), "reason": "ILLNESS"})
    route = day.route(sid)
    lo, hi = dt.datetime.fromisoformat(at("09:05")), dt.datetime.fromisoformat(at("13:00"))
    assert not any(lo <= s.start < hi for s in route.stops)
    # наряд со слотом после until может остаться у того же сотрудника
    assert "ORD-20261026-0002" in day.visits()


def test_order_cancelled_started_and_not_started() -> None:
    day = _two_visit_day()
    day.send({"type": "ORDER_CANCELLED", "timestamp": at("09:30"), "order_id": "ORD-20261026-0001",
              "reason": "CUSTOMER_REQUEST"})
    # визит начат: остаётся в маршруте, в cancelled не попадает (H9)
    assert "ORD-20261026-0001" in day.visits() and day.plan.cancelled == []
    day.send({"type": "ORDER_CANCELLED", "timestamp": at("09:45"), "order_id": "ORD-20261026-0002",
              "reason": "NOT_CONFIRMED"})
    assert "ORD-20261026-0002" not in day.visits()
    assert [c.order_id for c in day.plan.cancelled] == ["ORD-20261026-0002"]
    # повтор отмены — 422, состояние не меняется
    version = day.plan.plan_version
    day.send({"type": "ORDER_CANCELLED", "timestamp": at("09:50"), "order_id": "ORD-20261026-0002",
              "reason": "NOT_CONFIRMED"}, status=422)
    assert Plan.model_validate(day.client.get(f"/api/v1/days/{DAY}/plan").json()).plan_version == version


def test_new_order_forms_pgp_group_and_group_grows() -> None:
    day = Day([_order(1, "B-00001", 11, reg=at("08:00")), _order(2, "B-00002", 13, reg=at("08:20"))])
    assert day.plan.pgp_groups == [] and len(day.visits()) == 2
    third = _order(3, "B-00003", 15, reg=at("08:50"))
    res = day.send({"type": "NEW_ORDER", "timestamp": at("08:50"), "order": third})
    assert len(day.plan.pgp_groups) == 1
    group = day.plan.pgp_groups[0]
    assert sorted(group.order_ids) == ["ORD-20261026-0001", "ORD-20261026-0002", "ORD-20261026-0003"]
    assert group.detected_at == dt.datetime.fromisoformat(at("08:50"))
    assert not set(group.order_ids) & set(day.visits())  # сняты из маршрутов
    assert any(a["type"] == "PGP_DETECTED" and a["group_id"] == group.group_id for a in res["new_alerts"])
    # пополнение: следующий наряд узла через ≤ 60 мин после последнего — в ту же группу
    fourth = _order(4, "B-00004", 15, reg=at("09:40"))
    day.send({"type": "NEW_ORDER", "timestamp": at("09:40"), "order": fourth})
    g = day.plan.pgp_groups[0]
    assert g.group_id == group.group_id and g.detected_at == group.detected_at
    assert "ORD-20261026-0004" in g.order_ids and "ORD-20261026-0004" not in day.visits()
