"""Слой хранения на настоящем PostgreSQL: миграции, репозитории, импорт файлов, отметки и восстановление дня.

Нужна переменная TEST_DATABASE_URL (отдельная тестовая база, имя должно оканчиваться на `_test`,
например dispatch_test). Без неё все тесты модуля пропускаются. Рабочая база dispatch не используется.

Перед каждым тестом схема public пересоздаётся (DROP SCHEMA ... CASCADE; миграции накатываются заново),
зависшие сессии закрываются, чтобы фоновая очередь записи переподключилась к чистой схеме.
«Рестарт процесса» — сброс кэшей модулей (репозитории, аккаунты, справочник, настройки) и новый
экземпляр приложения: так же, как при старте, восстановление берётся из БД.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pytest
from fastapi.testclient import TestClient

from app import auth
from app import journal as journal_mod
from app import runtime_settings
from app import staff_directory
from conftest import load_catalog_reference
from app.main import create_app
from app.storage import repos
from app.storage.db import Database, apply_migrations
from app.storage.db import db as db_obj
from app.storage.repos import PgRepo

TEST_URL = (os.environ.get("TEST_DATABASE_URL") or "").strip() or None
DISPATCHER_PASSWORD = "disp-pass-pg-123"

pytestmark = pytest.mark.skipif(TEST_URL is None, reason="TEST_DATABASE_URL не задан: тесты PostgreSQL пропущены")

# Таблицы журнала до версионирования (как их создавал journal.py): миграция 0001 должна принять их без потерь.
LEGACY_JOURNAL_SQL = """
CREATE TABLE days (date date PRIMARY KEY, "now" timestamptz, loaded_at timestamptz NOT NULL DEFAULT now(),
                   orders_total integer);
CREATE TABLE plan_versions (date date NOT NULL, version integer NOT NULL, as_of timestamptz,
    based_on_event_id text, computation_ms integer, visits_planned integer, unassigned integer,
    pgp_orders integer, cancelled integer, late_risk_active integer, travel_sec_total integer,
    utilization_cv double precision, plan jsonb NOT NULL, PRIMARY KEY (date, version));
CREATE TABLE events (date date NOT NULL, event_id text NOT NULL, ts timestamptz, type text, payload jsonb,
    accepted boolean, duplicate boolean, processing_ms integer, plan_version integer,
    PRIMARY KEY (date, event_id));
CREATE TABLE alerts (date date NOT NULL, alert_id text NOT NULL, created_at timestamptz, type text, status text,
    order_id text, payload jsonb, PRIMARY KEY (date, alert_id));
"""


# ---------------------------------------------------------------- служебные функции

def _sql(query: str, params: tuple = ()):
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        cur = conn.execute(query, params)
        return cur.fetchall() if cur.description else None


def _reset_schema() -> None:
    """Пустая схема public с накатанными миграциями (зависшие сессии закрываются)."""
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        conn.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                     "WHERE datname = current_database() AND pid <> pg_backend_pid()")
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        apply_migrations(conn)


def _reset_caches(mp: pytest.MonkeyPatch) -> None:
    """Сброс процессных кэшей, как при новом старте процесса (откатывается monkeypatch)."""
    mp.setattr(repos, "_repos", {})
    mp.setattr(repos, "_waited", False)
    mp.setattr(auth, "_store", None)
    mp.setattr(staff_directory, "_directory", None)
    mp.setattr(runtime_settings, "_loaded_from", None)
    mp.setattr(runtime_settings, "_pg", None)
    mp.setattr(runtime_settings, "_overrides", {})


def _start(mp: pytest.MonkeyPatch) -> TestClient:
    """Новый «процесс»: дописать очередь записи, сбросить кэши, создать приложение (восстановление при старте)."""
    assert db_obj.flush(15), db_obj.stats()
    _reset_caches(mp)
    return TestClient(create_app())


def _demo_day_with_visit(client: TestClient) -> tuple[str, str, str]:
    r = client.get("/ui/demo/load", params={"scenario": "dense"})
    assert r.status_code == 200, r.text
    day = r.json()["date"]
    staff = client.get("/ui/employee/staff", params={"date": day}).json()["staff"]
    sid = next(s["staff_id"] for s in staff if s["visits"] > 0)
    route = client.get(f"/ui/employee/{sid}/route", params={"date": day}).json()
    oid = next(s["order_id"] for s in route["stops"] if s["type"] == "VISIT")
    return day, sid, oid


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- фикстура

@pytest.fixture()
def pg(tmp_path, monkeypatch):
    path = urlparse(TEST_URL).path.lstrip("/")
    if not path.endswith("_test"):
        pytest.fail(f"TEST_DATABASE_URL должен указывать на тестовую базу (имя *_test), сейчас: {path!r}")

    monkeypatch.setenv("UI_PLAN_BUDGET_SEC", "2")
    monkeypatch.setenv("PLANNER_DAY_BUDGET_SEC", "10")
    monkeypatch.setenv("STATE_RESTORE_WAIT_SEC", "10")
    monkeypatch.setenv("AUTH_ENABLED", "off")
    monkeypatch.setenv("AUTH_ACCOUNTS_FILE", str(tmp_path / "auth_accounts.json"))
    monkeypatch.setenv("STAFF_DIRECTORY_FILE", str(tmp_path / "staff_directory.json"))
    monkeypatch.setenv("RUNTIME_SETTINGS_FILE", str(tmp_path / "runtime_settings.json"))
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    monkeypatch.delenv("DEMO_ACCOUNTS", raising=False)
    monkeypatch.setattr(db_obj, "url", TEST_URL)
    monkeypatch.setattr(journal_mod.journal, "url", TEST_URL)

    assert db_obj.flush(15), db_obj.stats()
    _reset_schema()
    assert db_obj.wait_ready(15), "тестовая база недоступна"
    _reset_caches(monkeypatch)
    yield tmp_path
    assert db_obj.flush(15), db_obj.stats()
    _reset_caches(monkeypatch)


# ---------------------------------------------------------------- миграции

def test_migrations_from_zero_and_restart_is_idempotent(pg):
    with psycopg.connect(TEST_URL, autocommit=True) as conn:  # пустая схема, без миграций
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    assert _sql("SELECT to_regclass('public.schema_migrations')") == [(None,)]

    first = Database(TEST_URL)  # старт приложения с пустой схемой
    conn = first._connect()
    conn.close()
    assert first.schema_version == 2
    first._pool.close()

    rows = _sql("SELECT version, name FROM schema_migrations ORDER BY version")
    assert rows == [(1, "journal"), (2, "state")]
    tables = {r[0] for r in _sql("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")}
    assert {"days", "plan_versions", "events", "inputs", "service_state", "day_state",
            "marks", "actions", "accounts", "staff_profiles", "settings"} <= tables
    stamp = _sql("SELECT applied_at FROM schema_migrations WHERE version = 2")

    again = Database(TEST_URL)  # повторный старт: ничего не применяется, данные и отметки времени на месте
    conn = again._connect()
    conn.close()
    assert again.schema_version == 2
    again._pool.close()
    assert _sql("SELECT count(*) FROM schema_migrations") == [(2,)]
    assert _sql("SELECT applied_at FROM schema_migrations WHERE version = 2") == stamp
    with psycopg.connect(TEST_URL, autocommit=True) as c:
        assert apply_migrations(c) == []


def test_legacy_journal_tables_are_kept_by_migrations(pg):
    _reset_schema()
    with psycopg.connect(TEST_URL, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute(LEGACY_JOURNAL_SQL)
        conn.execute("INSERT INTO days (date, orders_total) VALUES ('2020-01-01', 7)")
        conn.execute("INSERT INTO plan_versions (date, version, plan) VALUES ('2020-01-01', 1, '{\"legacy\": true}')")

        assert apply_migrations(conn) == [1, 2]

        assert conn.execute("SELECT orders_total FROM days WHERE date = '2020-01-01'").fetchone() == (7,)
        assert conn.execute("SELECT count(*) FROM plan_versions WHERE date = '2020-01-01'").fetchone() == (1,)
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'plan_versions'")}
        assert "utilization_profiles" in cols  # колонка, добавленная позже, тоже появилась


# ---------------------------------------------------------------- репозитории

@pytest.mark.parametrize("table, key, value", [
    ("accounts", "660001", {"login": "660001", "role": "EMPLOYEE", "staff_id": "ST-001"}),
    ("staff_profiles", "ST-001", {"profile": {"staff_id": "ST-001", "first_name": "Иван"}, "equipment": None}),
    ("settings", "runtime_overrides", {"TRAFFIC_FACTORS": "on"}),
])
def test_pgrepo_crud_and_persistence(pg, table, key, value):
    repo = PgRepo(table)
    assert repo.all() == {}

    other = key + "-2"
    repo.put(key, value)
    repo.put(other, {"tmp": 1})
    assert repo.get(key) == value and repo.get("absent", "dflt") == "dflt"
    assert repos.flush(10)
    col = {"accounts": "login", "staff_profiles": "staff_id", "settings": "key"}[table]
    assert _sql(f"SELECT count(*) FROM {table}") == [(2,)]

    updated = {**value, "changed": True} if isinstance(value, dict) else value
    repo.put(key, updated)
    repo.delete(other)
    assert repos.flush(10)
    assert _sql(f"SELECT count(*) FROM {table} WHERE {col} = %s", (other,)) == [(0,)]

    fresh = PgRepo(table)  # новый экземпляр читает то, что записано в БД
    assert fresh.all() == {key: updated}


# ---------------------------------------------------------------- импорт файлов

def test_files_imported_once_and_not_modified_on_restart(pg, monkeypatch):
    accounts_file = Path(os.environ["AUTH_ACCOUNTS_FILE"])
    staff_file = Path(os.environ["STAFF_DIRECTORY_FILE"])
    settings_file = Path(os.environ["RUNTIME_SETTINGS_FILE"])

    acct = auth.AccountStore(accounts_file)._new("660001", "EMPLOYEE", "pw-from-file-1", staff_id="ST-001",
                                                 source="random")
    accounts_file.write_text(json.dumps({"version": 1, "accounts": [acct]}, ensure_ascii=False), encoding="utf-8")
    profile = {"staff_id": "ST-001", "first_name": "Импорт", "last_name": "Файлов", "middle_name": None,
               "phone": None, "position": "Инженер ЛТП", "personnel_number": "660001", "note": None}
    staff_file.write_text(json.dumps({"version": 1, "staff": [profile]}, ensure_ascii=False), encoding="utf-8")
    settings_file.write_text(json.dumps({"overrides": {"TRAFFIC_FACTORS": "off"}}), encoding="utf-8")
    before = {p: _sha(p) for p in (accounts_file, staff_file, settings_file)}

    # первый старт с БД: таблицы пусты, файлы есть -> импорт
    c = _start(monkeypatch)
    assert [a["login"] for a in auth.accounts().list()] == ["660001"]
    assert _sql("SELECT value->'profile'->>'first_name' FROM staff_profiles WHERE staff_id = 'ST-001'") == \
        [("Импорт",)]
    assert _sql("SELECT value FROM settings WHERE key = 'runtime_overrides'") == [({"TRAFFIC_FACTORS": "off"},)]
    assert c.get("/ui/directory/staff/ST-001").json()["first_name"] == "Импорт"
    assert {p: _sha(p) for p in before} == before, "файлы не должны меняться"

    # файлы «испортили» после импорта: повторный старт не импортирует их снова (берём из БД)
    staff_file.write_text(json.dumps({"version": 1, "staff": [{**profile, "first_name": "Другой"}]},
                                     ensure_ascii=False), encoding="utf-8")
    settings_file.write_text(json.dumps({"overrides": {"TRAFFIC_FACTORS": "on"}}), encoding="utf-8")
    accounts_file.write_text(json.dumps({"version": 1, "accounts": []}), encoding="utf-8")
    after_edit = {p: _sha(p) for p in (accounts_file, staff_file, settings_file)}

    _start(monkeypatch)
    assert [a["login"] for a in auth.accounts().list()] == ["660001"]
    assert _sql("SELECT value->'profile'->>'first_name' FROM staff_profiles WHERE staff_id = 'ST-001'") == \
        [("Импорт",)]
    assert _sql("SELECT value FROM settings WHERE key = 'runtime_overrides'") == [({"TRAFFIC_FACTORS": "off"},)]
    assert {p: _sha(p) for p in after_edit} == after_edit, "повторный старт файлы не меняет"


# ---------------------------------------------------------------- отметки и восстановление дня

def test_mark_is_written_and_restored_after_restart(pg, monkeypatch):
    c = _start(monkeypatch)
    day, sid, oid = _demo_day_with_visit(c)
    base = f"/ui/employee/{sid}/marks"

    r = c.post(base, params={"date": day}, json={"order_id": oid, "kind": "CALLED"})
    assert r.status_code == 200 and r.json()["duplicate"] is False, r.text
    assert db_obj.flush(15)
    assert _sql("SELECT kind, staff_id, order_id FROM marks WHERE mark_key = %s", (oid,)) == [("CALLED", sid, oid)]

    c2 = _start(monkeypatch)  # рестарт
    assert c2.app.state.restore_report["restored"] is True
    route = c2.get(f"/ui/employee/{sid}/route", params={"date": day}).json()
    stop = next(s for s in route["stops"] if s["order_id"] == oid)
    assert stop["marks"]["CALLED"] is not None, "отметка должна вернуться из marks"

    r = c2.post(base, params={"date": day}, json={"order_id": oid, "kind": "CALLED"})
    assert r.status_code == 200 and r.json()["duplicate"] is True  # идемпотентность переживает рестарт
    assert db_obj.flush(15)
    assert _sql("SELECT count(*) FROM marks WHERE mark_key = %s", (oid,)) == [(1,)]

    r = c2.post(f"/ui/employee/{sid}/marks/revert", params={"date": day}, json={"order_id": oid, "kind": "CALLED"})
    assert r.status_code == 200, r.text
    assert db_obj.flush(15)
    assert _sql("SELECT count(*) FROM marks WHERE mark_key = %s", (oid,)) == [(0,)]  # откат удаляет строку


def test_day_restored_after_restart_version_kept_and_event_applied(pg, monkeypatch):
    c = _start(monkeypatch)
    day, _, _ = _demo_day_with_visit(c)
    r = c.post("/ui/demo/step", params={"count": 3})
    assert r.status_code == 200 and all(s["accepted"] for s in r.json()["sent"]), r.text
    version = c.get(f"/api/v1/days/{day}/plan").json()["plan_version"]
    demo_before = c.get("/ui/demo/status").json()
    assert db_obj.flush(15)
    assert _sql("SELECT plan_version FROM day_state WHERE date = %s", (day,)) == [(version,)]

    c2 = _start(monkeypatch)
    report = c2.app.state.restore_report
    assert report["restored"] is True and report["skipped"] == [], report
    assert [d["date"] for d in report["days"]] == [day]

    plan = c2.get(f"/api/v1/days/{day}/plan").json()
    assert plan["plan_version"] == version, "версия плана не должна откатиться"
    assert c2.get("/ui/demo/status").json() == demo_before

    r = c2.post("/ui/demo/step", params={"count": 1})
    sent = r.json()["sent"]
    assert sent and sent[0]["accepted"] is True, r.text
    assert c2.get(f"/api/v1/days/{day}/plan").json()["plan_version"] == version + 1


def test_actions_are_logged_without_passwords(pg, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "on")
    monkeypatch.setenv("DISPATCHER_PASSWORD", DISPATCHER_PASSWORD)
    monkeypatch.delenv("DISPATCHER_LOGIN", raising=False)
    auth.limiter.reset()
    c = _start(monkeypatch)
    load_catalog_reference(c)  # аккаунты сотрудников создаются из справочника в Store

    r = c.post("/ui/auth/login", json={"login": "dispatcher", "password": DISPATCHER_PASSWORD})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    r = c.post("/ui/directory/accounts/ST-001/reset-password", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    temporary = r.json()["temporary_password"]
    assert db_obj.flush(15)

    rows = _sql("SELECT actor, action, target, details::text FROM actions ORDER BY id")
    actions = [row[1] for row in rows]
    assert any("reset-password" in a for a in actions), actions
    assert any("login" in a for a in actions), actions
    blob = json.dumps(rows, ensure_ascii=False)
    assert temporary not in blob and DISPATCHER_PASSWORD not in blob and token not in blob
