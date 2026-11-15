"""Мобильный экран сотрудника /ui/employee: маршрут, порядок отметок, ошибки и сохранность отметок.

Данные и загрузка дня — как в test_ui_api.py (демо на data/synth, маленький бюджет планировщика).
День грузится один раз на модуль: отметки живут в памяти процесса, поэтому тесты идут
по порядку файла и каждый работает с собственными нарядами.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.demo_dates import dense_date
from app.main import create_app

DAY = dense_date()


@pytest.fixture(scope="module")
def client():
    mp = pytest.MonkeyPatch()
    mp.setenv("UI_PLAN_BUDGET_SEC", "2")
    mp.setenv("PLANNER_DAY_BUDGET_SEC", "2")
    try:
        with TestClient(create_app()) as c:
            r = c.get("/ui/demo/load", params={"scenario": "dense"})
            assert r.status_code == 200, r.text
            yield c
    finally:
        mp.undo()


def _staff_with_visits(client) -> list[dict]:
    items = client.get("/ui/employee/staff", params={"date": DAY}).json()["staff"]
    return [s for s in items if s["visits"] > 0]


def _visit_ids(client, staff_id: str) -> list[str]:
    route = client.get(f"/ui/employee/{staff_id}/route", params={"date": DAY}).json()
    return [s["order_id"] for s in route["stops"] if s["type"] == "VISIT"]


def test_staff_before_plan_is_409():
    mp = pytest.MonkeyPatch()
    mp.setenv("UI_PLAN_BUDGET_SEC", "2")
    try:
        with TestClient(create_app()) as c:
            r = c.get("/ui/employee/staff")
            assert r.status_code == 409, r.text
            assert r.json()["error"]["code"] == "PLAN_NOT_BUILT"
    finally:
        mp.undo()


def test_route_has_stops_and_next_call_hint(client):
    staff = _staff_with_visits(client)
    assert staff, "в демо-дне должен быть сотрудник с визитами"
    sid = staff[0]["staff_id"]
    route = client.get(f"/ui/employee/{sid}/route", params={"date": DAY}).json()

    assert route["on_shift"] is True
    visits = [s for s in route["stops"] if s["type"] == "VISIT"]
    assert len(visits) == route["progress"]["visits_total"] == staff[0]["visits"]
    assert route["next"] is not None
    assert route["next"]["order_id"] in {v["order_id"] for v in visits}
    cw = visits[0]["call_window"]
    assert cw["start"] < cw["end"]  # окно звонка: за 60..30 минут до слота


def test_marks_follow_called_arrived_done_order(client):
    sid = _staff_with_visits(client)[0]["staff_id"]
    oid = _visit_ids(client, sid)[0]
    base = f"/ui/employee/{sid}/marks"

    r = client.post(base, params={"date": DAY}, json={"order_id": oid, "kind": "ARRIVED"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "MARK_OUT_OF_ORDER", r.text

    r = client.post(base, params={"date": DAY}, json={"order_id": oid, "kind": "CALLED"})
    assert r.status_code == 200, r.text
    assert r.json()["duplicate"] is False and r.json()["next_mark"] == "ARRIVED"

    r = client.post(base, params={"date": DAY}, json={"order_id": oid, "kind": "CALLED"})
    assert r.status_code == 200 and r.json()["duplicate"] is True  # повтор — не ошибка

    for kind in ("ARRIVED", "DONE"):
        r = client.post(base, params={"date": DAY}, json={"order_id": oid, "kind": kind})
        assert r.status_code == 200, r.text
    assert r.json()["next_mark"] is None

    route = client.get(f"/ui/employee/{sid}/route", params={"date": DAY}).json()
    stop = next(s for s in route["stops"] if s["order_id"] == oid)
    assert stop["state"] == "DONE"
    assert stop["marks"] and all(stop["marks"][k] for k in ("CALLED", "ARRIVED", "DONE"))


def test_mark_for_order_outside_route_is_422(client):
    staff = _staff_with_visits(client)
    if len(staff) < 2:
        pytest.skip("нужны два сотрудника с визитами")
    foreign = _visit_ids(client, staff[1]["staff_id"])[0]
    sid = staff[0]["staff_id"]
    r = client.post(f"/ui/employee/{sid}/marks", params={"date": DAY},
                    json={"order_id": foreign, "kind": "CALLED"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "ORDER_NOT_IN_ROUTE", r.text


def test_unknown_staff_is_404(client):
    r = client.get("/ui/employee/ST-NOPE/route", params={"date": DAY})
    assert r.status_code == 404 and r.json()["error"]["code"] == "STAFF_NOT_FOUND"
    r = client.post("/ui/employee/ST-NOPE/marks", params={"date": DAY},
                    json={"order_id": "X", "kind": "CALLED"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "STAFF_NOT_FOUND"


def test_marks_survive_demo_step(client):
    sid = _staff_with_visits(client)[0]["staff_id"]
    oid = _visit_ids(client, sid)[0]
    r = client.post(f"/ui/employee/{sid}/marks", params={"date": DAY},
                    json={"order_id": oid, "kind": "CALLED"})
    assert r.status_code == 200, r.text

    r = client.post("/ui/demo/step", params={"count": 1})
    assert r.status_code == 200, r.text

    route = client.get(f"/ui/employee/{sid}/route", params={"date": DAY}).json()
    stop = next((s for s in route["stops"] if s["order_id"] == oid), None)
    if stop is not None:  # наряд остался у сотрудника: отметка не должна потеряться
        assert stop["marks"]["CALLED"] is not None
        assert any(m["order_id"] == oid and m["kind"] == "CALLED" for m in route["marks"])
