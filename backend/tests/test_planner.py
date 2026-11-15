"""Тесты планировщика PyVRP и валидатора H1–H9 на маленькой фикстуре.

Сценарии: допуск к высоте (H1), окно инсталляции slot.start + 10 мин (H4),
перерыв (H3), АО и RESTOCK (H7/H8), ПГП, сквозной POST /plan.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Plan, StopType
from app.planner.pyvrp_planner import PyVrpPlanner
from app.planner.validator import validate_plan
from app.store import Store

DAY = "2026-10-26"
SHIFT = {"start": "09:00", "end": "18:00", "break_duration_min": 60, "break_window": {"from": "12:00", "to": "14:00"}}
FULL = {"ONT": 3, "ROUTER": 3, "STB": 3}
CAP = {"ONT": 6, "ROUTER": 6, "STB": 4}
POINTS = ["SC-01", "SC-02", "B-00001", "B-00002", "B-00003", "B-00004", "B-00005"]


def _staff(sid: str, sc: str = "SC-01", height: bool = False, profiles=("LTP3",), eq=FULL) -> dict:
    return {
        "staff_id": sid, "service_center_id": sc, "profiles": list(profiles), "technologies": ["PON"],
        "height_permit": height, "shift": SHIFT, "equipment_start": eq, "equipment_capacity": CAP,
    }


def _building(bid: str, private: bool = False) -> dict:
    return {"building_id": bid, "address": f"Екатеринбург, {bid}", "lat": 56.85, "lon": 60.6,
            "building_type": "PRIVATE" if private else "MKD", "overhead_line": private}


def _reference(staff: list[dict]) -> dict:
    sc = {"name": "СЦ", "address": "Екатеринбург", "lat": 56.85, "lon": 60.6, "restock_duration_min": 15}
    return {
        "service_centers": [
            {**sc, "service_center_id": "SC-01", "opening_hours": {"open": "08:00", "close": "21:00"}},
            {**sc, "service_center_id": "SC-02", "opening_hours": {"open": "10:00", "close": "20:00"}},
        ],
        "staff": staff,
        "access_nodes": [{
            "access_node_id": "AN-0001", "node_type": "OLT", "technology": "PON", "name": "OLT Тест-1",
            "lat": 56.85, "lon": 60.6,
            "buildings": [_building(b, private=(b == "B-00005")) for b in POINTS[2:]],
        }],
        "work_types": [
            {"code": "REPAIR", "name": "Ремонт", "profiles": ["LTP3"], "duration_min": 60, "max_start_delay_min": None,
             "deadline_kind": "HOURS", "deadline_value": 24, "repeat_deadline_value": 12, "pgp_relevant": True},
            {"code": "INST_PON_MKD", "name": "Инсталляция", "profiles": ["INSTALL"], "duration_min": 90,
             "max_start_delay_min": 10, "deadline_kind": "WORKING_DAYS", "deadline_value": 3,
             "repeat_deadline_value": None, "pgp_relevant": False},
        ],
    }


def _matrix(far: dict[tuple[str, str], int] | None = None) -> dict:
    n = len(POINTS)
    dur = [[0 if i == j else 600 for j in range(n)] for i in range(n)]
    for (a, b), v in (far or {}).items():
        dur[POINTS.index(a)][POINTS.index(b)] = v
    return {"matrix_id": "test", "points": POINTS, "durations_sec": dur, "distances_m": dur}


def _order(n: int, building: str, slot_h: int, code: str = "REPAIR", height: bool = False,
           eq: dict | None = None, reg: str = "2026-10-25T18:00:00+05:00") -> dict:
    start = f"{DAY}T{slot_h:02d}:00:00+05:00"
    end = f"{DAY}T{slot_h + 2:02d}:00:00+05:00"
    latest = f"{DAY}T{slot_h:02d}:10:00+05:00" if code.startswith("INST") else end
    return {
        "order_id": f"ORD-20261026-{n:04d}", "work_type_code": code, "technology": "PON",
        "building_id": building, "address": "Екатеринбург", "lat": 56.85, "lon": 60.6,
        "building_type": "PRIVATE" if height else "MKD", "requires_height": height, "access_node_id": "AN-0001",
        "registered_at": reg, "slot": {"start": start, "end": end}, "latest_start": latest,
        "deadline": f"{DAY}T23:00:00+05:00", "required_equipment": eq or {"ONT": 0, "ROUTER": 0, "STB": 0},
        "is_repeat": False,
    }


@pytest.fixture(autouse=True)
def _fast_budget(monkeypatch):
    monkeypatch.setenv("PLANNER_DAY_BUDGET_SEC", "1")


def _run(staff: list[dict], orders: list[dict], matrix: dict | None = None):
    """Загружает данные через API, строит план; возвращает (plan, service, day)."""
    app = create_app(planner=PyVrpPlanner(), store=Store())
    with TestClient(app) as client:
        assert client.put("/api/v1/reference", json=_reference(staff)).status_code == 200
        assert client.put("/api/v1/reference/travel-matrix", json=matrix or _matrix()).status_code == 200
        day = {"date": DAY, "now": f"{DAY}T08:30:00+05:00", "orders": orders}
        assert client.put(f"/api/v1/days/{DAY}", json=day).status_code == 200
        response = client.post(f"/api/v1/days/{DAY}/plan", json={"time_limit_sec": 30})
        assert response.status_code == 200, response.text
        plan = Plan.model_validate(response.json())
    store = app.state.service.store
    return plan, store


def _violations(plan: Plan, store: Store):
    import datetime as dt

    day = store.days[dt.date.fromisoformat(DAY)]
    return validate_plan(plan, store.reference, store.matrix, day)


def _visits(plan: Plan) -> dict[str, str]:
    return {s.order_id: r.staff_id for r in plan.routes for s in r.stops if s.type == StopType.VISIT}


def test_end_to_end_plan_is_valid_and_has_one_break_in_window() -> None:
    # регистрации через 2 ч — не ПГП
    orders = [_order(i, f"B-0000{1 + i % 4}", 9 + 2 * (i % 4), reg=f"2026-10-25T{7 + 2 * i:02d}:00:00+05:00")
              for i in range(1, 7)]
    plan, store = _run([_staff("ST-001"), _staff("ST-002", "SC-02")], orders)
    assert _violations(plan, store) == []
    assert len(_visits(plan)) + len(plan.unassigned) == 6
    for route in plan.routes:
        if not route.stops:
            continue
        kinds = [s.type for s in route.stops]
        assert kinds[0] == StopType.DEPOT_START and kinds[-1] == StopType.DEPOT_END
        assert kinds.count(StopType.BREAK) == 1
        brk = next(s for s in route.stops if s.type == StopType.BREAK)
        assert "12:00" <= brk.start.strftime("%H:%M") <= "14:00"
        assert (brk.end - brk.start).total_seconds() >= 3600
    # без перерыва валидатор ловит H3
    route = next(r for r in plan.routes if r.stops)
    route.stops = [s for s in route.stops if s.type != StopType.BREAK]
    for i, s in enumerate(route.stops, start=1):
        s.seq = i
    assert any(v.code == "H3" for v in _violations(plan, store))


def test_height_permit_is_respected() -> None:
    orders = [_order(1, "B-00005", 9, height=True), _order(2, "B-00001", 9)]
    plan, store = _run([_staff("ST-001"), _staff("ST-002", height=True)], orders)
    assert _visits(plan).get("ORD-20261026-0001") == "ST-002"
    assert _violations(plan, store) == []

    # без допуска на смене — NO_QUALIFIED_STAFF
    plan, _ = _run([_staff("ST-001")], orders[:1])
    assert [(u.order_id, u.reason_code) for u in plan.unassigned] == [("ORD-20261026-0001", "NO_QUALIFIED_STAFF")]
    assert any(a.type == "UNASSIGNED" and a.order_id == "ORD-20261026-0001" for a in plan.alerts)


def test_install_window_plus_10_minutes() -> None:
    # до B-00001 из СЦ ехать 75 мин: к 09:10 не успеть даже при выезде в 09:00
    far = {("SC-01", "B-00001"): 75 * 60}
    orders = [_order(1, "B-00001", 9, code="INST_PON_MKD"), _order(2, "B-00002", 11, code="INST_PON_MKD")]
    plan, store = _run([_staff("ST-031", profiles=("INSTALL",))], orders, _matrix(far))
    assert {u.order_id: u.reason_code for u in plan.unassigned} == {"ORD-20261026-0001": "SLOT_UNREACHABLE"}
    visit = next(s for r in plan.routes for s in r.stops if s.order_id == "ORD-20261026-0002")
    assert visit.start.strftime("%H:%M") <= "11:10"
    assert _violations(plan, store) == []

    # визит на 11 минут позже начала слота — H4
    import datetime as dt

    visit.start = visit.start.replace(hour=11, minute=11, second=0)
    visit.end = visit.start + dt.timedelta(minutes=90)
    assert any(v.code == "H4" and v.order_id == "ORD-20261026-0002" for v in _violations(plan, store))


def test_equipment_restock_in_open_service_center() -> None:
    empty = {"ONT": 0, "ROUTER": 0, "STB": 0}
    orders = [_order(1, "B-00001", 9, eq={"ONT": 1, "ROUTER": 0, "STB": 0})]
    plan, store = _run([_staff("ST-001", eq=empty)], orders)
    route = plan.routes[0]
    kinds = [s.type for s in route.stops]
    assert kinds.index(StopType.RESTOCK) < kinds.index(StopType.VISIT)
    restock = next(s for s in route.stops if s.type == StopType.RESTOCK)
    # SC-02 открывается в 10:00 — пополнение до слота 9–11 разумно только в SC-01
    assert restock.location_id == "SC-01"
    assert restock.equipment_after.ONT == 6
    assert _violations(plan, store) == []

    route.stops = [s for s in route.stops if s.type != StopType.RESTOCK]
    for i, s in enumerate(route.stops, start=1):
        s.seq = i
    assert any(v.code == "H7" for v in _violations(plan, store))


def test_pgp_group_is_taken_out_of_routing_and_h9_counts_once() -> None:
    regs = ["2026-10-26T07:00:00+05:00", "2026-10-26T07:30:00+05:00", "2026-10-26T07:55:00+05:00"]
    pgp = [_order(i + 1, f"B-0000{i + 1}", 11, reg=r) for i, r in enumerate(regs)]
    # четвёртый наряд с узла — через 2 часа: не в группе
    other = _order(4, "B-00004", 13, reg="2026-10-25T10:00:00+05:00")
    plan, store = _run([_staff("ST-001")], pgp + [other])
    assert len(plan.pgp_groups) == 1
    group = plan.pgp_groups[0]
    assert sorted(group.order_ids) == [o["order_id"] for o in pgp]
    assert not set(group.order_ids) & set(_visits(plan))
    assert "ORD-20261026-0004" in _visits(plan)
    assert any(a.type == "PGP_DETECTED" and a.group_id == group.group_id for a in plan.alerts)
    assert _violations(plan, store) == []

    # наряд одновременно в группе и в unassigned — H9
    from app.models import UnassignedOrder

    plan.unassigned.append(UnassignedOrder(order_id=group.order_ids[0], reason_code="OTHER",
                                           reason_text="x", suggested_action="ESCALATE"))
    assert any(v.code == "H9" and v.order_id == group.order_ids[0] for v in _violations(plan, store))

    # два наряда за час — не ПГП
    plan, _ = _run([_staff("ST-001")], pgp[:2])
    assert plan.pgp_groups == []
