"""Журнал истории: /ui/history без БД и запись журнала, которая не тормозит обработку событий.

DATABASE_URL журнал читает при импорте модуля (синглтон `journal`), поэтому вместо переменной
окружения состояние журнала подменяется через monkeypatch на время теста: url, очередь и
запуск фонового потока. Реальный PostgreSQL в тестах не нужен.
"""

from __future__ import annotations

import queue
import time

import pytest
from fastapi.testclient import TestClient

from app import journal as journal_mod
from app.demo_dates import dense_date
from app.main import create_app
from app.models import Plan

DAY = dense_date()
CLOSED_PORT_DSN = "postgresql://u:p@127.0.0.1:59999/x"


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


@pytest.fixture()
def no_db(monkeypatch):
    """Журнал выключен (как без DATABASE_URL)."""
    monkeypatch.setattr(journal_mod.journal, "url", None)


def test_history_endpoints_503_without_database(client, no_db):
    for path in ("/ui/history/days", f"/ui/history/{DAY}/versions", f"/ui/history/{DAY}/events",
                 f"/ui/history/{DAY}/plan/1"):
        r = client.get(path)
        assert r.status_code == 503, (path, r.text)
        assert r.json()["error"]["code"] == "JOURNAL_UNAVAILABLE", (path, r.text)


def test_event_not_blocked_by_unavailable_database(client, monkeypatch):
    # Журнал «включён», но БД на закрытом порту: очередь мала, чтобы проверить и путь переполнения.
    monkeypatch.setattr(journal_mod.journal, "url", CLOSED_PORT_DSN)
    monkeypatch.setattr(journal_mod.journal, "start", lambda: None)  # без фонового подключения
    monkeypatch.setattr(journal_mod.journal, "_q", queue.Queue(maxsize=1))

    started = time.monotonic()
    r = client.post("/ui/demo/step", params={"count": 2})
    elapsed = time.monotonic() - started
    assert r.status_code == 200, r.text
    assert r.json()["sent"], "событие должно быть отправлено сценарием"
    assert all(item.get("accepted") is not False for item in r.json()["sent"])
    assert elapsed < 5.0  # запись не ждёт БД: очередь неблокирующая

    r = client.get("/ui/history/days")  # БД не подключена — читающий путь честно отвечает 503
    assert r.status_code == 503 and r.json()["error"]["code"] == "JOURNAL_UNAVAILABLE"


def test_plan_metrics_from_plan(client):
    plan = Plan.model_validate(client.get(f"/api/v1/days/{DAY}/plan").json())
    m = journal_mod._metrics(plan)

    visits = sum(1 for r in plan.routes for st in r.stops if st.type == "VISIT")
    assert m["visits_planned"] == visits
    assert m["pgp_orders"] == sum(len(g.order_ids) for g in plan.pgp_groups)
    assert m["unassigned"] == (plan.summary.unassigned if plan.summary else len(plan.unassigned))
    assert m["late_risk_active"] == sum(
        1 for a in plan.alerts if a.type == "LATE_RISK" and a.status == "ACTIVE")
    assert set(m) == {"visits_planned", "unassigned", "pgp_orders", "cancelled", "late_risk_active",
                      "travel_sec_total", "utilization_cv"}
