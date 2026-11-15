"""Наряды из интерфейса /ui/orders/*: до загрузки дня, каталог, создание (автоназначение и пожелание), отмена.

Демо-день dense_date() (data/synth, сценарий dense). Справочник сотрудников — во временном файле,
data/ не трогается. Каждый тест получает свежий экземпляр приложения и загруженный день.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.demo_dates import date_compact, dense_date
from app.main import create_app

DAY = dense_date()
ORDER_ID = re.compile(rf"^ORD-{date_compact(DAY)}-\d{{4}}$")
WORK_TYPES = {"INST_PON_MKD", "INST_FTTB_MKD", "INST_PRIVATE_VLS", "ADD_SERVICE",
              "SURVEY_PRIVATE", "REPAIR", "DEGRADATION", "DISMANTLE"}


@pytest.fixture()
def bare_client(tmp_path, monkeypatch):
    monkeypatch.setenv("STAFF_DIRECTORY_FILE", str(tmp_path / "staff_directory.json"))
    monkeypatch.setenv("UI_PLAN_BUDGET_SEC", "2")
    monkeypatch.setenv("PLANNER_DAY_BUDGET_SEC", "2")
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture()
def client(bare_client):
    r = bare_client.get("/ui/demo/load", params={"scenario": "dense"})
    assert r.status_code == 200, r.text
    return bare_client


def _catalog(client, **params) -> dict:
    r = client.get("/ui/orders/catalog", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def _create(client, body: dict):
    return client.post("/ui/orders/create", json=body)


def test_before_load_catalog_create_cancel_are_409(bare_client):
    r = bare_client.get("/ui/orders/catalog")
    assert r.status_code == 409 and r.json()["error"]["code"] == "REFERENCE_NOT_LOADED"

    r = _create(bare_client, {"building_id": "B-00132", "work_type": "REPAIR"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "PLAN_NOT_BUILT"

    r = bare_client.post(f"/ui/orders/ORD-{DAY.replace('-', '')}-0001/cancel")
    assert r.status_code == 409 and r.json()["error"]["code"] == "PLAN_NOT_BUILT"


def test_catalog_lists_work_types_and_caps_search(client):
    cat = _catalog(client)
    assert {w["code"] for w in cat["work_types"]} == WORK_TYPES
    assert len(cat["buildings"]) == 20 and cat["buildings_total"] == cat["buildings_count"] > 20
    assert cat["staff"] and all("on_shift" in s for s in cat["staff"])

    found = _catalog(client, q="ул")
    assert found["buildings_total"] > 20 and len(found["buildings"]) == 20
    assert all("ул" in b["address"].lower() for b in found["buildings"])

    byid = _catalog(client, q="b-00132")  # поиск по идентификатору дома
    assert [b["building_id"] for b in byid["buildings"]] == ["B-00132"]

    assert client.get("/ui/orders/catalog", params={"limit": 50}).status_code == 422
    assert client.get("/ui/orders/catalog", params={"work_type": "NOPE"}).status_code == 422


def test_catalog_work_type_filter_follows_generator_rules(client):
    cat = _catalog(client, work_type="INST_PON_MKD")
    assert cat["buildings"]
    assert all(b["building_type"] == "MKD" and "PON" in b["technologies"] for b in cat["buildings"])
    assert all("INST_PON_MKD" in b["work_types"] for b in cat["buildings"])


def test_create_auto_assign_is_accepted_with_staff_or_reason(client):
    b = _catalog(client, work_type="INST_PON_MKD")["buildings"][0]
    r = _create(client, {"building_id": b["building_id"], "work_type": "INST_PON_MKD"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] is True and ORDER_ID.match(body["order_id"])
    assert body["preferred"] is None

    if body["outcome"] == "ASSIGNED":
        assert body["assignment"]["staff_id"] and body["assignment"]["planned_start"]
    elif body["outcome"] == "PGP":
        assert body["order_id"] in body["pgp"]["order_ids"]
    else:
        assert body["outcome"] == "UNASSIGNED" and body["unassigned"]["reason_text"]

    rows = {o["order_id"]: o for o in client.get(f"/ui/day/{DAY}/orders").json()["orders"]}
    expected = {"ASSIGNED": "ROUTED", "PGP": "PGP", "UNASSIGNED": "UNASSIGNED"}[body["outcome"]]
    assert rows[body["order_id"]]["status"] == expected


def test_create_with_unqualified_preferred_keeps_outcome_and_explains(client):
    cat = _catalog(client, work_type="INST_PON_MKD")
    b = cat["buildings"][0]
    # LTP3 не выполняет монтаж (INSTALL-профиль), пожелание заведомо неприменимо
    ltp3 = next(s for s in cat["staff"] if s["profiles"] == ["LTP3"])

    r = _create(client, {"building_id": b["building_id"], "work_type": "INST_PON_MKD",
                         "preferred_staff_id": ltp3["staff_id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] is True and ORDER_ID.match(body["order_id"])
    assert body["outcome"] in {"ASSIGNED", "UNASSIGNED", "PGP"}
    if body["outcome"] == "ASSIGNED":
        assert body["assignment"]["staff_id"] != ltp3["staff_id"]

    pref = body["preferred"]
    assert pref["staff_id"] == ltp3["staff_id"] and pref["honored"] is False
    assert pref["message"].startswith("Пожелание не")


def test_create_with_unknown_preferred_is_422_and_no_order(client):
    b = _catalog(client, work_type="INST_PON_MKD")["buildings"][0]
    before = len(client.get(f"/ui/day/{DAY}/orders").json()["orders"])

    r = _create(client, {"building_id": b["building_id"], "work_type": "INST_PON_MKD",
                         "preferred_staff_id": "ST-999"})
    assert r.status_code == 422, r.text
    err = r.json()["error"]
    assert err["code"] == "UNKNOWN_STAFF" and err["details"][0]["field"] == "preferred_staff_id"
    assert len(client.get(f"/ui/day/{DAY}/orders").json()["orders"]) == before


def test_cancel_created_order_then_409(client):
    b = _catalog(client, work_type="INST_PON_MKD")["buildings"][0]
    oid = _create(client, {"building_id": b["building_id"], "work_type": "INST_PON_MKD"}).json()["order_id"]

    r = client.post(f"/ui/orders/{oid}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] is True and r.json()["cancelled"] is True and r.json()["order_id"] == oid

    rows = {o["order_id"]: o for o in client.get(f"/ui/day/{DAY}/orders").json()["orders"]}
    assert rows[oid]["status"] == "CANCELLED"

    r = client.post(f"/ui/orders/{oid}/cancel")
    assert r.status_code == 409 and r.json()["error"]["code"] == "ORDER_ALREADY_CANCELLED"


def test_cancel_unknown_order_is_404(client):
    r = client.post(f"/ui/orders/ORD-{date_compact(DAY)}-9999/cancel")
    assert r.status_code == 404 and r.json()["error"]["code"] == "UNKNOWN_ORDER"
