"""Справочник сотрудников /ui/directory/staff: список, правка, защита полей из staff.json, создание.

Справочник пишется во временный файл (STAFF_DIRECTORY_FILE на tmp_path), data/ не трогается.
Каждый тест получает свой файл и свой экземпляр справочника (модуль пересоздаёт его при смене пути).
Поля оборудования здесь не проверяются.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from conftest import load_catalog_reference

STAFF_ID = "ST-001"  # есть в справочнике каталога: профиль LTP3, СЦ SC-01


@pytest.fixture()
def client(tmp_path, monkeypatch):
    path = tmp_path / "staff_directory.json"
    monkeypatch.setenv("STAFF_DIRECTORY_FILE", str(path))
    with TestClient(create_app()) as c:
        load_catalog_reference(c)  # справочник сотрудников наполняется из справочника в Store
        c.tmp_file = path  # type: ignore[attr-defined]
        yield c


def test_list_and_get_write_to_tmp_file(client):
    r = client.get("/ui/directory/staff")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == len(body["staff"]) > 0
    assert body["persisted"] is True
    assert client.tmp_file.exists(), "справочник должен записаться во временный файл"

    item = client.get(f"/ui/directory/staff/{STAFF_ID}").json()
    assert item["staff_id"] == STAFF_ID
    assert item["in_planner"] is True
    assert item["profile"] == "LTP3" and item["service_center_id"] == "SC-01"
    # записи из справочника создаются без ФИО (заготовка ФИО — только при загрузке каталога)
    assert item["display_name"] == f"Сотрудник {STAFF_ID}" and item["name_empty"] is True
    assert item["personnel_number"] == STAFF_ID and item["in_reference"] is True


def test_put_renames_employee(client):
    r = client.put(f"/ui/directory/staff/{STAFF_ID}", json={"first_name": "  Пётр ", "last_name": "Петров"})
    assert r.status_code == 200, r.text
    assert r.json()["first_name"] == "Пётр"  # пробелы по краям обрезаются
    assert r.json()["display_name"].startswith("Петров П.")

    again = client.get(f"/ui/directory/staff/{STAFF_ID}").json()
    assert again["first_name"] == "Пётр" and again["last_name"] == "Петров"
    on_disk = json.loads(client.tmp_file.read_text(encoding="utf-8"))["staff"]
    assert any(p["staff_id"] == STAFF_ID and p["first_name"] == "Пётр" for p in on_disk)


def test_put_profile_or_service_center_from_staff_json_is_422(client):
    r = client.put(f"/ui/directory/staff/{STAFF_ID}", json={"profile": "INSTALL"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "READ_ONLY_FIELD"

    r = client.put(f"/ui/directory/staff/{STAFF_ID}", json={"service_center_id": "SC-02"})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "READ_ONLY_FIELD"

    # Профиль и СЦ не изменились.
    item = client.get(f"/ui/directory/staff/{STAFF_ID}").json()
    assert item["profile"] == "LTP3" and item["service_center_id"] == "SC-01"


def test_unknown_staff_is_404(client):
    r = client.put("/ui/directory/staff/ST-999", json={"first_name": "Иван"})
    assert r.status_code == 404, r.text
    assert r.json()["error"]["code"] == "STAFF_NOT_FOUND"

    r = client.get("/ui/directory/staff/ST-999")
    assert r.status_code == 404, r.text


def test_post_creates_employee_with_next_id(client):
    new = {"first_name": "Олег", "last_name": "Тестов", "profile": "INSTALL", "service_center_id": "SC-01"}

    r1 = client.post("/ui/directory/staff", json=new)
    assert r1.status_code == 201, r1.text
    first = r1.json()
    assert first["applies_from_next_replan"] is True
    assert first["in_planner"] is False  # в планировщик попадёт только при пересборке дня

    r2 = client.post("/ui/directory/staff", json=new)
    assert r2.status_code == 201, r2.text
    second = r2.json()
    assert int(second["staff_id"][3:]) == int(first["staff_id"][3:]) + 1
    assert second["staff_id"] != first["staff_id"]


def test_without_reference_list_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("STAFF_DIRECTORY_FILE", str(tmp_path / "staff_directory.json"))
    with TestClient(create_app()) as c:
        body = c.get("/ui/directory/staff").json()
        assert body["reference_loaded"] is False and body["staff"] == []


def test_other_reference_keeps_old_records(client):
    client.put(f"/ui/directory/staff/{STAFF_ID}", json={"first_name": "Пётр", "last_name": "Петров"})
    from app import catalog

    ref = json.loads(catalog.reference_bytes())
    ref["staff"][0]["staff_id"] = "ST-900"  # ST-001 -> ST-900
    assert client.put("/api/v1/reference", json=ref).status_code == 200
    staff = {s["staff_id"]: s for s in client.get("/ui/directory/staff").json()["staff"]}
    assert staff[STAFF_ID]["in_reference"] is False and staff[STAFF_ID]["last_name"] == "Петров"
    assert staff["ST-900"]["display_name"] == "Сотрудник ST-900"
