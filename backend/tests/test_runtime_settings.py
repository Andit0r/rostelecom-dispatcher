"""Режимы на лету: GET/PUT /ui/settings и POST /ui/settings/reset, модуль runtime_settings.

Оверрайды пишутся в JSON на tmp_path (RUNTIME_SETTINGS_FILE), data/ не трогается.
Состояние модуля (_overrides, _loaded_from) подменяется на время теста через monkeypatch.
AUTH_ENABLED=off выставляет conftest; тест с авторизацией включает её сам.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import auth, runtime_settings as rs
from app.main import create_app
from conftest import load_catalog_reference
from app.planner.context import traffic_enabled

DISPATCHER_PASSWORD = "disp-pass-123"
EMPLOYEE_ID = "ST-001"  # есть в data/synth/staff.json


@pytest.fixture()
def settings_file(tmp_path, monkeypatch):
    """Чистое состояние: без env-переменных режимов, пустая память, файл оверрайдов на tmp_path."""
    path = tmp_path / "runtime_settings.json"
    for key in rs.REGISTRY:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RUNTIME_SETTINGS_FILE", str(path))
    monkeypatch.setenv("STAFF_DIRECTORY_FILE", str(tmp_path / "staff_directory.json"))
    monkeypatch.setattr(rs, "_overrides", {})
    monkeypatch.setattr(rs, "_loaded_from", None)
    return path


@pytest.fixture()
def client(settings_file):
    with TestClient(create_app()) as c:
        yield c


def _by_key(client) -> dict[str, dict]:
    return {s["key"]: s for s in client.get("/ui/settings").json()["settings"]}


def test_get_returns_all_settings_with_fields_and_joint_risk(client):
    r = client.get("/ui/settings")
    assert r.status_code == 200, r.text
    settings = r.json()["settings"]
    assert len(settings) == len(rs.REGISTRY) == 5
    assert {s["key"] for s in settings} == set(rs.REGISTRY)
    for s in settings:
        for field in ("key", "label", "value", "default", "source", "risk"):
            assert field in s, (s["key"], field)
    joint = _by_key(client)["JOINT_ORDERS"]
    assert joint["risk"] and joint["risk"].strip()


def test_put_traffic_on_is_changed_outdates_plan_and_applies(client):
    r = client.put("/ui/settings", json={"TRAFFIC_FACTORS": "on"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] == ["TRAFFIC_FACTORS"]
    assert body["plan_outdated"] is True
    assert rs.get("TRAFFIC_FACTORS") == "on"
    assert traffic_enabled() is True


def test_put_budget_only_does_not_outdate_plan(client):
    r = client.put("/ui/settings", json={"PLANNER_DAY_BUDGET_SEC": 30})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["changed"] == ["PLANNER_DAY_BUDGET_SEC"]
    assert body["plan_outdated"] is False
    assert rs.get("PLANNER_DAY_BUDGET_SEC") == 30.0


@pytest.mark.parametrize("bad", [
    {"TRAFFIC_FACTORS": "bad"},
    {"UI_PLAN_BUDGET_SEC": 500},
    {"NO_SUCH_SETTING": 1},
])
def test_invalid_put_is_422_and_nothing_applies(client, settings_file, bad):
    # валидное изменение в том же запросе не должно примениться: всё или ничего
    r = client.put("/ui/settings", json={"PLANNER_DAY_BUDGET_SEC": 30, **bad})
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert rs.get("PLANNER_DAY_BUDGET_SEC") == 60.0
    assert traffic_enabled() is False
    assert not settings_file.exists()


def test_reset_restores_defaults_and_overrides_file_survives_reload(client, settings_file, monkeypatch):
    client.put("/ui/settings", json={"TRAFFIC_FACTORS": "on", "PLANNER_DAY_BUDGET_SEC": 30})
    assert json.loads(settings_file.read_text(encoding="utf-8"))["overrides"]["TRAFFIC_FACTORS"] == "on"

    r = client.post("/ui/settings/reset")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["changed"]) == {"TRAFFIC_FACTORS", "PLANNER_DAY_BUDGET_SEC"}
    assert body["plan_outdated"] is True
    assert traffic_enabled() is False
    settings = _by_key(client)
    assert all(s["source"] in ("default", "env") for s in settings.values())
    assert settings["TRAFFIC_FACTORS"]["source"] == "default"
    assert settings["TRAFFIC_FACTORS"]["value"] == "off"

    # перезагрузка модуля: память пуста, файл читается заново
    client.put("/ui/settings", json={"TRAFFIC_FACTORS": "on"})
    monkeypatch.setattr(rs, "_overrides", {})
    monkeypatch.setattr(rs, "_loaded_from", None)
    assert rs.get("TRAFFIC_FACTORS") == "on"
    assert _by_key(client)["TRAFFIC_FACTORS"]["source"] == "override"


def test_settings_routes_forbidden_for_employee_and_401_without_token(tmp_path, monkeypatch, settings_file):
    monkeypatch.setenv("AUTH_ENABLED", "on")
    monkeypatch.setenv("AUTH_ACCOUNTS_FILE", str(tmp_path / "auth_accounts.json"))
    monkeypatch.setenv("DISPATCHER_PASSWORD", DISPATCHER_PASSWORD)
    monkeypatch.delenv("DISPATCHER_LOGIN", raising=False)
    monkeypatch.delenv("DEMO_ACCOUNTS", raising=False)
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    auth.limiter.reset()
    with TestClient(create_app()) as c:
        load_catalog_reference(c)  # аккаунты сотрудников создаются из справочника в Store
        c.cookies.clear()
        assert c.get("/ui/settings").status_code == 401

        token = c.post("/ui/auth/login",
                       json={"login": "dispatcher", "password": DISPATCHER_PASSWORD}).json()["token"]
        r = c.post(f"/ui/directory/accounts/{EMPLOYEE_ID}/reset-password",
                   headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200, r.text
        login, temp = r.json()["login"], r.json()["temporary_password"]
        emp_token = c.post("/ui/auth/login", json={"login": login, "password": temp}).json()["token"]

        r = c.get("/ui/settings", headers={"Authorization": f"Bearer {emp_token}"})
        assert r.status_code == 403, r.text
        assert r.json()["error"]["code"] == "FORBIDDEN"
