"""Смоук-тесты каркаса: загрузка, план-заглушка, события, идемпотентность и ошибки.

Данные — маленькая встроенная фикстура (2 сотрудника, 2 дома, матрица 3x3),
чтобы тесты шли за доли секунды и не зависели от data/synth.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import Plan
from app.planner import StubPlanner
from app.store import Store

DAY = "2026-10-26"
SHIFT_DAY = {
    "start": "09:00",
    "end": "18:00",
    "break_duration_min": 60,
    "break_window": {"from": "12:00", "to": "15:00"},
}
SHIFT_LATE = {
    "start": "12:00",
    "end": "21:00",
    "break_duration_min": 60,
    "break_window": {"from": "15:00", "to": "18:00"},
}
EQ_START = {"ONT": 3, "ROUTER": 4, "STB": 2}
EQ_CAP = {"ONT": 6, "ROUTER": 6, "STB": 4}
BUILDING_1 = {
    "building_id": "B-00412",
    "address": "г. Екатеринбург, ул. Учителей, 18",
    "lat": 56.8712,
    "lon": 60.6531,
    "building_type": "MKD",
    "overhead_line": False,
    "zone_service_center_id": "SC-01",
}
BUILDING_2 = {**BUILDING_1, "building_id": "B-00415", "address": "г. Екатеринбург, ул. Уральская, 61",
              "lat": 56.8655, "lon": 60.6493}


def _reference() -> dict:
    return {
        "service_centers": [
            {
                "service_center_id": "SC-01",
                "name": "СЦ «Север»",
                "address": "г. Екатеринбург, ул. Победы, 30",
                "lat": 56.8941,
                "lon": 60.6105,
                "opening_hours": {"open": "08:00", "close": "21:00"},
                "restock_duration_min": 15,
            }
        ],
        "staff": [
            {
                "staff_id": "ST-001",
                "service_center_id": "SC-01",
                "profiles": ["LTP3"],
                "technologies": ["PON", "FTTB"],
                "height_permit": False,
                "shift": SHIFT_DAY,
                "equipment_start": EQ_START,
                "equipment_capacity": EQ_CAP,
            },
            {
                "staff_id": "ST-029",
                "service_center_id": "SC-01",
                "profiles": ["INSTALL"],
                "technologies": ["PON"],
                "height_permit": True,
                "shift": SHIFT_LATE,
                "equipment_start": EQ_START,
                "equipment_capacity": EQ_CAP,
            },
        ],
        "access_nodes": [
            {
                "access_node_id": "AN-0031",
                "node_type": "OLT",
                "technology": "PON",
                "name": "OLT Пионерский-2",
                "lat": 56.8698,
                "lon": 60.651,
                "buildings": [BUILDING_1, BUILDING_2],
            }
        ],
        "work_types": [
            {
                "code": "REPAIR",
                "name": "Устранение неисправности (обращение абонента)",
                "profiles": ["LTP3"],
                "duration_min": 60,
                "max_start_delay_min": None,
                "deadline_kind": "HOURS",
                "deadline_value": 24,
                "repeat_deadline_value": 12,
                "pgp_relevant": True,
            },
            {
                "code": "INST_PON_MKD",
                "name": "Инсталляция ШПД/IPTV, МКД, PON",
                "profiles": ["INSTALL"],
                "duration_min": 90,
                "max_start_delay_min": 10,
                "deadline_kind": "WORKING_DAYS",
                "deadline_value": 3,
                "repeat_deadline_value": None,
                "pgp_relevant": False,
            },
        ],
    }


def _matrix() -> dict:
    return {
        "matrix_id": "ekb-2026-10-v1",
        "points": ["SC-01", "B-00412", "B-00415"],
        "durations_sec": [[0, 1080, 1200], [1110, 0, 240], [1230, 260, 0]],
        "distances_m": [[0, 4620, 5100], [4700, 0, 780], [5150, 820, 0]],
        "hourly_factors": [{"hour": 8, "factor": 1.35}],
    }


def _order(order_id: str, code: str, building: dict, slot_start: str, slot_end: str,
           latest: str, deadline: str, equipment: dict) -> dict:
    return {
        "order_id": order_id,
        "work_type_code": code,
        "technology": "PON",
        "building_id": building["building_id"],
        "address": building["address"],
        "lat": building["lat"],
        "lon": building["lon"],
        "building_type": building["building_type"],
        "requires_height": False,
        "access_node_id": "AN-0031",
        "registered_at": "2026-10-25T18:40:00+05:00",
        "slot": {"start": slot_start, "end": slot_end},
        "latest_start": latest,
        "deadline": deadline,
        "required_equipment": equipment,
        "is_repeat": False,
        "previous_order_id": None,
    }


def _day() -> dict:
    return {
        "date": DAY,
        "now": "2026-10-26T08:30:00+05:00",
        "absent_staff_ids": [],
        "orders": [
            _order("ORD-20261026-0001", "REPAIR", BUILDING_1,
                   "2026-10-26T09:00:00+05:00", "2026-10-26T11:00:00+05:00",
                   "2026-10-26T11:00:00+05:00", "2026-10-26T18:40:00+05:00",
                   {"ONT": 1, "ROUTER": 0, "STB": 0}),
            _order("ORD-20261026-0042", "INST_PON_MKD", BUILDING_2,
                   "2026-10-26T13:00:00+05:00", "2026-10-26T15:00:00+05:00",
                   "2026-10-26T13:10:00+05:00", "2026-10-27T14:12:00+05:00",
                   {"ONT": 1, "ROUTER": 1, "STB": 1}),
        ],
    }


@pytest.fixture
def client() -> TestClient:
    app = create_app(planner=StubPlanner(), store=Store())
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def loaded(client: TestClient) -> TestClient:
    assert client.put("/api/v1/reference", json=_reference()).status_code == 200
    assert client.put("/api/v1/reference/travel-matrix", json=_matrix()).status_code == 200
    assert client.put(f"/api/v1/days/{DAY}", json=_day()).status_code == 200
    assert client.post(f"/api/v1/days/{DAY}/plan").status_code == 200
    return client


def _tick(ts: str, event_id: str = "EV-20261026-0001") -> dict:
    return {"event_id": event_id, "type": "TIME_TICK", "timestamp": ts}


def test_health_is_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_stub_plan_is_valid_model(loaded: TestClient) -> None:
    response = loaded.get(f"/api/v1/days/{DAY}/plan")
    assert response.status_code == 200
    plan = Plan.model_validate(response.json())
    assert plan.plan_version == 1
    assert {r.staff_id for r in plan.routes} == {"ST-001", "ST-029"}
    assert {u.order_id for u in plan.unassigned} == {"ORD-20261026-0001", "ORD-20261026-0042"}
    for route in plan.routes:
        assert route.stops[0].type == "DEPOT_START"
        assert route.stops[-1].type == "DEPOT_END"


def test_event_bumps_version_and_repeat_is_duplicate(loaded: TestClient) -> None:
    first = loaded.post(f"/api/v1/days/{DAY}/events", json=_tick("2026-10-26T10:00:00+05:00"))
    assert first.status_code == 200
    assert first.json()["accepted"] is True
    assert first.json()["duplicate"] is False
    assert first.json()["plan_version"] == 2

    repeat = loaded.post(f"/api/v1/days/{DAY}/events", json=_tick("2026-10-26T10:00:00+05:00"))
    assert repeat.status_code == 200
    assert repeat.json()["duplicate"] is True
    assert repeat.json()["plan_version"] == 2


def test_unknown_order_is_422_and_state_unchanged(loaded: TestClient) -> None:
    assert loaded.post(f"/api/v1/days/{DAY}/events", json=_tick("2026-10-26T10:00:00+05:00")).status_code == 200

    cancel = {
        "event_id": "EV-20261026-0002",
        "type": "ORDER_CANCELLED",
        "timestamp": "2026-10-26T10:05:00+05:00",
        "order_id": "ORD-20261026-9999",
        "reason": "NOT_CONFIRMED",
    }
    rejected = loaded.post(f"/api/v1/days/{DAY}/events", json=cancel)
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "UNKNOWN_ORDER"
    assert loaded.get(f"/api/v1/days/{DAY}/plan").json()["plan_version"] == 2

    late = loaded.post(f"/api/v1/days/{DAY}/events", json=_tick("2026-10-26T09:00:00+05:00", "EV-20261026-0003"))
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "EVENT_OUT_OF_ORDER"


def test_staff_unavailable_marks_route(loaded: TestClient) -> None:
    event = {
        "event_id": "EV-20261026-0016",
        "type": "STAFF_UNAVAILABLE",
        "timestamp": "2026-10-26T11:10:00+05:00",
        "staff_id": "ST-001",
        "until": None,
        "reason": "VEHICLE_BREAKDOWN",
    }
    response = loaded.post(f"/api/v1/days/{DAY}/events", json=event)
    assert response.status_code == 200
    assert response.json()["changed_staff_ids"] == ["ST-001"]
    plan = Plan.model_validate(loaded.get(f"/api/v1/days/{DAY}/plan").json())
    route = next(r for r in plan.routes if r.staff_id == "ST-001")
    assert route.status == "UNAVAILABLE"


def test_invalid_json_is_400_error_response(client: TestClient) -> None:
    response = client.put("/api/v1/reference", content=b"{not json", headers={"content-type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "BAD_REQUEST"
