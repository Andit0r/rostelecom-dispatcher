"""Рюкзак АО в справочнике сотрудников: валидация, правка по одному, массовая правка и сброс,
подстановка эффективных значений в справочники демо-дня.

Справочник пишется во временный файл (STAFF_DIRECTORY_FILE на tmp_path), data/ не трогается.
Справочники загружаются через PUT /api/v1/reference из служебного каталога (как «Создать день»);
исходные данные рюкзака — из справочника в Store.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import catalog
from app.main import create_app
from app.ui_api import _effective_reference_bytes
from conftest import load_catalog_reference

LTP3_ID = "ST-001"  # LTP3; в staff.json: старт ONT 3 / ROUTER 5 / STB 2, максимум 6 / 6 / 4
INSTALL_ID = "ST-029"  # INSTALL из staff.json


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("STAFF_DIRECTORY_FILE", str(tmp_path / "staff_directory.json"))
    with TestClient(create_app()) as c:
        load_catalog_reference(c)
        yield c


def _item(client, staff_id: str) -> dict:
    return client.get(f"/ui/directory/staff/{staff_id}").json()


def _all(client) -> dict[str, dict]:
    return {s["staff_id"]: s for s in client.get("/ui/directory/staff").json()["staff"]}


def test_capacity_below_start_is_422_and_nothing_changes(client):
    r = client.put(f"/ui/directory/staff/{LTP3_ID}", json={"equipment_capacity": {"ONT": 2}})  # старт ONT 3
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "EQUIPMENT_CAPACITY_LESS_THAN_START"

    r = client.put(f"/ui/directory/staff/{LTP3_ID}", json={"equipment_start": {"STB": 5}})  # максимум STB 4
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "EQUIPMENT_CAPACITY_LESS_THAN_START"

    r = client.put(f"/ui/directory/staff/{LTP3_ID}", json={"equipment_start": {"ONT": 100}})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "INVALID_EQUIPMENT"

    item = _item(client, LTP3_ID)
    assert item["equipment_override"] is False
    assert item["equipment_start"] == item["equipment_start_data"]
    assert item["equipment_capacity"] == item["equipment_capacity_data"]


def test_put_override_then_delete_restores_data(client):
    r = client.put(f"/ui/directory/staff/{LTP3_ID}",
                   json={"equipment_start": {"ONT": 2}, "equipment_capacity": {"ONT": 5}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["equipment_override"] is True and body["applies_from_next_replan"] is True
    assert body["equipment_start"]["ONT"] == 2 and body["equipment_capacity"]["ONT"] == 5
    assert body["equipment_start"]["ROUTER"] == body["equipment_start_data"]["ROUTER"]  # не переданные виды не трогаем

    r = client.delete(f"/ui/directory/staff/{LTP3_ID}/equipment-override")
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["equipment_override"] is False
    assert item["equipment_start"] == item["equipment_start_data"]
    assert item["equipment_capacity"] == item["equipment_capacity_data"]


def test_bulk_install_changes_only_installers(client):
    r = client.post("/ui/directory/staff/equipment/bulk",
                    json={"start": {"ONT": 1}, "capacity": {"ONT": 7}, "profile": "INSTALL"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["profile"] == "INSTALL" and body["applies_from_next_replan"] is True

    staff = _all(client)
    installers = [s for s in staff.values() if s["profile"] == "INSTALL"]
    assert body["updated"] == len(installers) > 0
    assert all(s["equipment_override"] and s["equipment_start"]["ONT"] == 1 and s["equipment_capacity"]["ONT"] == 7
               for s in installers)
    others = [s for s in staff.values() if s["profile"] != "INSTALL"]
    assert others and not any(s["equipment_override"] for s in others)


def test_bulk_is_atomic_when_some_capacity_below_start(client):
    # у части LTP3 старт ONT выше 2 — операция целиком отклоняется, никто не меняется
    r = client.post("/ui/directory/staff/equipment/bulk", json={"capacity": {"ONT": 2}, "profile": "LTP3"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "EQUIPMENT_CAPACITY_LESS_THAN_START"
    assert not any(s["equipment_override"] for s in _all(client).values())


def test_bulk_reset_restores_installers_and_keeps_others(client):
    client.post("/ui/directory/staff/equipment/bulk",
                json={"start": {"ONT": 1}, "capacity": {"ONT": 7}, "profile": "INSTALL"})
    r = client.put(f"/ui/directory/staff/{LTP3_ID}", json={"equipment_start": {"ONT": 2}})
    assert r.status_code == 200, r.text

    r = client.post("/ui/directory/staff/equipment/bulk/reset", json={"profile": "INSTALL"})
    assert r.status_code == 200, r.text
    staff = _all(client)
    installers = [s for s in staff.values() if s["profile"] == "INSTALL"]
    assert r.json()["reset"] == len(installers)
    assert all(s["equipment_override"] is False and s["equipment_start"] == s["equipment_start_data"]
               for s in installers)
    assert staff[LTP3_ID]["equipment_override"] is True  # LTP3 не сбрасывался


def test_effective_values_reach_demo_reference(client):
    r = client.put(f"/ui/directory/staff/{LTP3_ID}",
                   json={"equipment_start": {"ONT": 2}, "equipment_capacity": {"ONT": 5}})
    assert r.status_code == 200, r.text

    eff = {s["staff_id"]: s for s in json.loads(_effective_reference_bytes())["staff"]}
    raw = {s["staff_id"]: s for s in json.loads(catalog.reference_bytes())["staff"]}
    assert eff[LTP3_ID]["equipment_start"]["ONT"] == 2 and eff[LTP3_ID]["equipment_capacity"]["ONT"] == 5
    assert raw[LTP3_ID]["equipment_start"]["ONT"] == 3  # исходные файлы не тронуты
    assert eff[INSTALL_ID]["equipment_start"] == raw[INSTALL_ID]["equipment_start"]  # без переопределения — как пришло

    client.delete(f"/ui/directory/staff/{LTP3_ID}/equipment-override")
    eff = {s["staff_id"]: s for s in json.loads(_effective_reference_bytes())["staff"]}
    assert eff[LTP3_ID] == raw[LTP3_ID]
