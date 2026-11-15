"""Сценарный анализ /ui/scenario/*: расчёт на копии, живой план дня не меняется."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.demo_dates import dense_date
from app.main import create_app
from app.models import Plan
from app.scenarios import metrics


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("UI_PLAN_BUDGET_SEC", "2")
    monkeypatch.setenv("PLANNER_DAY_BUDGET_SEC", "2")
    with TestClient(create_app()) as c:
        r = c.get("/ui/demo/load", params={"scenario": "dense"})
        assert r.status_code == 200, r.text
        yield c


def _live(client) -> tuple[dict, set]:
    plan = client.get(f"/api/v1/days/{dense_date()}/plan").json()
    svc = client.app.state.service
    day = next(iter(svc.store.days.values()))
    return plan, set(day.absent_staff_ids)


def test_staff_cut_keeps_live_plan_and_counts(client):
    before = _live(client)
    r = client.post("/ui/scenario/staff-cut", json={"remove_count": 3, "strategy": "by_profile:INSTALL", "seed": 7,
                                                     "budget_sec": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert _live(client) == before  # живой план и absent не тронуты

    assert len(body["removed"]) == 3 and all(x["profiles"] == ["INSTALL"] for x in body["removed"])
    assert body["base_source"] == "current_plan_v1"
    # база — это текущий план: метрики совпадают с пересчитанными по живому плану
    plan = Plan.model_validate(before[0])
    assert body["base"] == metrics(plan, body["base"]["orders_total"])
    for m in (body["base"], body["scenario"]):
        assert m["visits"] + m["unassigned"] + m["pgp_orders"] == m["orders_total"]
        assert sum(m["unassigned_by_reason"].values()) == m["unassigned"]
    removed = {x["staff_id"] for x in body["removed"]}
    # у убранных сотрудников в сценарии нет визитов, их бывшие наряды либо у других, либо в «пострадавших»
    assert all(a["order_id"] for a in body["affected_orders"])
    assert body["delta"]["visits"] == body["scenario"]["visits"] - body["base"]["visits"]
    assert body["time_sec"] > 0 and removed


def test_zero_removed_equals_base_and_errors(client):
    r = client.post("/ui/scenario/staff-cut", json={"remove_count": 0, "strategy": "random"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["base"] == body["scenario"] and body["affected_orders"] == [] and body["removed"] == []
    assert all(v == 0 for v in body["delta"].values())

    r = client.post("/ui/scenario/staff-cut", json={"remove_count": 15, "strategy": "by_service_center:SC-01"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "NOT_ENOUGH_STAFF"
    r = client.post("/ui/scenario/staff-cut", json={"remove_count": 99})
    assert r.status_code == 400 and r.json()["error"]["code"] == "BAD_REQUEST"


def test_equipment_no_restock(client):
    before = _live(client)
    r = client.post("/ui/scenario/equipment", json={"mode": "no_restock", "budget_sec": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    assert _live(client) == before
    assert body["no_restock"]["restocks"] == 0
    assert {x["profile"] for x in body["recommended"]} == {"INSTALL", "LTP3"}
    for x in body["recommended"]:
        for k in ("ONT", "ROUTER", "STB"):
            assert 0 <= x[k]["p50"] <= x[k]["p90"] <= x[k]["max"]
    assert body["visits_gained"] == body["no_restock"]["visits"] - body["current"]["visits"]
