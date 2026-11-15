"""Авторизация /ui: вход, роли, сброс/отключение аккаунтов, защита от перебора, демо-подсказка.

Включаем AUTH_ENABLED=on через monkeypatch (conftest выставляет off). Аккаунты, секрет и справочник
сотрудников пишутся во временный каталог tmp_path. Состояние лимитера входа сбрасывается перед каждым тестом.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.demo_dates import dense_date
from app.main import create_app
from conftest import load_catalog_reference

DISPATCHER_PASSWORD = "disp-pass-123"
EMPLOYEE_ID = "ST-001"  # есть в справочнике каталога (аккаунты создаются из справочника в Store)
OTHER_EMPLOYEE_ID = "ST-002"
DAY_PATH = f"/ui/day/{dense_date()}/orders"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "on")
    monkeypatch.setenv("AUTH_ACCOUNTS_FILE", str(tmp_path / "auth_accounts.json"))
    monkeypatch.setenv("STAFF_DIRECTORY_FILE", str(tmp_path / "staff_directory.json"))
    monkeypatch.setenv("DISPATCHER_PASSWORD", DISPATCHER_PASSWORD)
    monkeypatch.delenv("DISPATCHER_LOGIN", raising=False)
    monkeypatch.delenv("DEMO_ACCOUNTS", raising=False)
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    auth.limiter.reset()
    with TestClient(create_app()) as c:
        load_catalog_reference(c)  # /api/v1 открыт: аккаунты сотрудников появятся из справочника
        c.cookies.clear()
        yield c


def _login(client, login: str, password: str):
    return client.post("/ui/auth/login", json={"login": login, "password": password})


def _dispatcher_token(client) -> str:
    r = _login(client, "dispatcher", DISPATCHER_PASSWORD)
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _employee_login(client, staff_id: str = EMPLOYEE_ID) -> tuple[str, str]:
    """Сбрасывает пароль сотрудника диспетчером и возвращает (логин, временный пароль)."""
    token = _dispatcher_token(client)
    r = client.post(f"/ui/directory/accounts/{staff_id}/reset-password",
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["login"], body["temporary_password"]


def test_dispatcher_login_sets_httponly_cookie(client):
    r = _login(client, "dispatcher", DISPATCHER_PASSWORD)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "DISPATCHER" and body["token"]
    assert "httponly" in r.headers["set-cookie"].lower()

    me = client.get("/ui/auth/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200 and me.json()["role"] == "DISPATCHER"


def test_wrong_password_and_unknown_login_give_same_401(client):
    wrong = _login(client, "dispatcher", "не-тот-пароль")
    unknown = _login(client, "nobody-такого-нет", "какой-то-пароль")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_reference_needs_token_but_health_is_open(client):
    r = client.get("/ui/reference")
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "UNAUTHORIZED"

    assert client.get("/api/v1/health").status_code == 200


def test_employee_is_forbidden_on_dispatcher_routes_and_only_own_route(client):
    login, temp = _employee_login(client)
    tok = _login(client, login, temp).json()["token"]
    headers = {"Authorization": f"Bearer {tok}"}

    assert client.get("/ui/reference", headers=headers).status_code == 403
    assert client.get(DAY_PATH, headers=headers).status_code == 403

    own = client.get(f"/ui/employee/{EMPLOYEE_ID}/route", headers=headers)
    assert own.status_code not in (401, 403), own.text  # 409/404 допустимы: день не загружен

    other = client.get(f"/ui/employee/{OTHER_EMPLOYEE_ID}/route", headers=headers)
    assert other.status_code == 403, other.text


def test_reset_password_replaces_old_and_revokes_old_token(client):
    login, first = _employee_login(client)
    old_token = _login(client, login, first).json()["token"]

    token = _dispatcher_token(client)
    r = client.post(f"/ui/directory/accounts/{EMPLOYEE_ID}/reset-password",
                    headers={"Authorization": f"Bearer {token}"})
    second = r.json()["temporary_password"]
    assert second != first

    assert _login(client, login, first).status_code == 401
    assert _login(client, login, second).status_code == 200
    stale = client.get("/ui/auth/me", headers={"Authorization": f"Bearer {old_token}"})
    assert stale.status_code == 401


def test_disable_makes_existing_token_401(client):
    login, temp = _employee_login(client)
    tok = _login(client, login, temp).json()["token"]
    headers = {"Authorization": f"Bearer {tok}"}
    assert client.get("/ui/auth/me", headers=headers).status_code == 200

    dispatcher = {"Authorization": f"Bearer {_dispatcher_token(client)}"}
    r = client.post(f"/ui/directory/accounts/{EMPLOYEE_ID}/disable", headers=dispatcher)
    assert r.status_code == 200, r.text
    assert client.get("/ui/auth/me", headers=headers).status_code == 401
    assert _login(client, login, temp).status_code == 401


def test_rate_limit_after_five_failures_returns_429_with_retry_after(client):
    for _ in range(5):
        assert _login(client, "dispatcher", "bad-password").status_code == 401
    r = _login(client, "dispatcher", "bad-password")
    assert r.status_code == 429, r.text
    assert int(r.headers["Retry-After"]) >= 1
    assert r.json()["error"]["code"] == "TOO_MANY_ATTEMPTS"


def test_create_staff_returns_login_and_initial_password(client):
    token = _dispatcher_token(client)
    new = {"first_name": "Олег", "last_name": "Тестов", "profile": "INSTALL", "service_center_id": "SC-01"}
    r = client.post("/ui/directory/staff", json=new, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["login"] and body["initial_password"]
    assert _login(client, body["login"], body["initial_password"]).status_code == 200


def test_demo_hint_disabled_by_default_and_enabled_without_password(client, monkeypatch):
    assert client.get("/ui/auth/demo-hint").json() == {"enabled": False}

    monkeypatch.setenv("DEMO_ACCOUNTS", "on")
    monkeypatch.setenv("DEMO_PASSWORD", "demo-secret-xyz-1")
    auth.limiter.reset()
    with TestClient(create_app()) as c:
        r = c.get("/ui/auth/demo-hint")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is True and body["dispatcher_login"] == auth.DEMO_DISPATCHER_LOGIN
        assert "demo-secret-xyz-1" not in r.text
