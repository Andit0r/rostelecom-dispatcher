"""UI-API: загрузка демо-дня, наряды со статусами, шаг сценария (на data/synth)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.demo_dates import dense_date
from app.main import create_app


def test_ui_demo_load_orders_step(monkeypatch):
    monkeypatch.setenv("UI_PLAN_BUDGET_SEC", "2")
    monkeypatch.setenv("PLANNER_DAY_BUDGET_SEC", "2")
    with TestClient(create_app()) as client:
        r = client.get("/ui/demo/load", params={"scenario": "dense"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "loaded" and body["date"] == dense_date()

        r = client.get(f"/ui/day/{dense_date()}/orders")
        assert r.status_code == 200, r.text
        orders = r.json()["orders"]
        assert len(orders) == body["orders"]
        assert {o["status"] for o in orders} <= {"ROUTED", "UNASSIGNED", "PGP", "CANCELLED"}
        assert any(o["status"] == "ROUTED" and o["staff_id"] for o in orders)

        r = client.post("/ui/demo/step", params={"count": 2})
        assert r.status_code == 200, r.text
        assert r.json()["plan_version"] >= 2

        r = client.get("/ui/reference")
        assert r.status_code == 200 and r.json()["staff"]
