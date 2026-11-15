"""Слой хранения БЕЗ PostgreSQL: FileRepo, импорт файлов в заглушку, сервис без DATABASE_URL.

DATABASE_URL в тестах не задаётся: синглтоны `db` и `journal` читают его при импорте, поэтому
url подменяется через monkeypatch (как в test_history_api.py). Все файлы (аккаунты, справочник,
настройки) — в tmp_path; кэши процесса (репозитории, аккаунты, справочник, настройки) на время
теста подменяются monkeypatch и откатываются после него.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import auth
from app import journal as journal_mod
from app import runtime_settings
from app import staff_directory
from app.main import create_app
from app.storage import repos
from app.storage.db import db as db_obj
from app.storage.repos import FileRepo, import_missing, sync_repo


@pytest.fixture()
def no_db(tmp_path, monkeypatch):
    """Режим без БД: url выключен, все файлы — в tmp_path, кэши процесса пусты."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(db_obj, "url", None)
    monkeypatch.setattr(journal_mod.journal, "url", None)
    monkeypatch.setattr(repos, "_repos", {})
    monkeypatch.setattr(repos, "_waited", False)
    monkeypatch.setattr(auth, "_store", None)
    monkeypatch.setattr(staff_directory, "_directory", None)
    monkeypatch.setattr(runtime_settings, "_loaded_from", None)
    monkeypatch.setattr(runtime_settings, "_pg", None)
    monkeypatch.setattr(runtime_settings, "_overrides", {})
    monkeypatch.setenv("AUTH_ACCOUNTS_FILE", str(tmp_path / "auth_accounts.json"))
    monkeypatch.setenv("STAFF_DIRECTORY_FILE", str(tmp_path / "staff_directory.json"))
    monkeypatch.setenv("RUNTIME_SETTINGS_FILE", str(tmp_path / "runtime_settings.json"))
    monkeypatch.setenv("UI_PLAN_BUDGET_SEC", "2")
    monkeypatch.setenv("PLANNER_DAY_BUDGET_SEC", "2")
    assert db_obj.enabled is False
    return tmp_path


# ---------------------------------------------------------------- FileRepo

def test_file_repo_get_put_delete_all(tmp_path):
    repo = FileRepo(tmp_path / "repo.json")
    assert repo.backend == "file"
    assert repo.get("missing", "dflt") == "dflt"
    assert repo.all() == {}

    value = {"a": [1, 2], "b": {"c": "текст"}}
    repo.put("k1", value)
    repo.put("k2", 7)
    assert repo.get("k1") == value
    assert repo.all() == {"k1": value, "k2": 7}

    # get/all отдают копии: правка результата не меняет хранимое значение
    got = repo.get("k1")
    got["a"].append(99)
    repo.all()["k2"] = -1
    assert repo.get("k1") == value and repo.get("k2") == 7

    repo.delete("k1")
    repo.delete("nope")  # удаление отсутствующего ключа — не ошибка
    assert repo.get("k1") is None and repo.all() == {"k2": 7}


def test_file_repo_persists_and_tolerates_bad_file(tmp_path):
    path = tmp_path / "repo.json"
    FileRepo(path).put("settings", {"x": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"settings": {"x": 1}}
    assert FileRepo(path).get("settings") == {"x": 1}  # новый экземпляр читает то же

    path.write_text("{не json", encoding="utf-8")
    broken = FileRepo(path)
    assert broken.all() == {}  # битый файл — пустой репозиторий, без исключения
    broken.put("again", True)
    assert FileRepo(path).get("again") is True


def test_sync_repo_writes_changed_keys_only_and_import_is_once(tmp_path):
    repo = FileRepo(tmp_path / "repo.json")
    repo.put("same", 1)
    n = sync_repo(repo, {"same": 1, "new": 2})
    assert n == 1 and repo.all() == {"same": 1, "new": 2}

    n = sync_repo(repo, {"same": 1}, delete_missing=True)
    assert n == 1 and repo.all() == {"same": 1}

    # импорт из файлов: только отсутствующие ключи, существующие значения не перетираются
    assert import_missing(repo, {"same": 100, "added": "from-file"}, "тест") == 1
    assert repo.get("same") == 1 and repo.get("added") == "from-file"
    assert import_missing(repo, {"same": 100, "added": "other"}, "тест") == 0
    assert repo.get("added") == "from-file"


# ---------------------------------------------------------------- сервис без БД

def test_service_starts_without_database_and_status_is_clear(no_db):
    with TestClient(create_app()) as c:
        assert c.get("/api/v1/health").status_code == 200

        st = c.get("/ui/history/storage")
        assert st.status_code == 200, st.text
        body = st.json()
        assert body["storage"]["enabled"] is False and body["storage"]["ready"] is False
        assert body["restore"]["restored"] is False  # пустой старт, без ошибки

        for path in ("/ui/history/days", "/ui/history/actions"):
            r = c.get(path)
            assert r.status_code == 503, (path, r.text)
            assert r.json()["error"]["code"] == "JOURNAL_UNAVAILABLE", (path, r.text)


def test_marks_work_and_stay_in_memory_without_database(no_db):
    with TestClient(create_app()) as c:
        r = c.get("/ui/demo/load", params={"scenario": "dense"})
        assert r.status_code == 200, r.text
        day = r.json()["date"]

        staff = c.get("/ui/employee/staff", params={"date": day}).json()["staff"]
        sid = next(s["staff_id"] for s in staff if s["visits"] > 0)
        route = c.get(f"/ui/employee/{sid}/route", params={"date": day}).json()
        oid = next(s["order_id"] for s in route["stops"] if s["type"] == "VISIT")
        base = f"/ui/employee/{sid}/marks"

        r = c.post(base, params={"date": day}, json={"order_id": oid, "kind": "CALLED"})
        assert r.status_code == 200, r.text
        assert r.json()["duplicate"] is False

        # повтор — из кэша в памяти, без нового события и без ошибки
        r = c.post(base, params={"date": day}, json={"order_id": oid, "kind": "CALLED"})
        assert r.status_code == 200 and r.json()["duplicate"] is True

        route = c.get(f"/ui/employee/{sid}/route", params={"date": day}).json()
        stop = next(s for s in route["stops"] if s["order_id"] == oid)
        assert stop["marks"]["CALLED"] is not None
        assert c.get("/ui/employee/status").json()["loaded"] is True

    # «рестарт» без БД: отметки живут только в памяти процесса и не восстанавливаются
    with TestClient(create_app()) as c2:
        assert c2.get("/ui/employee/status").json()["loaded"] is False
        assert c2.get("/ui/history/storage").json()["storage"]["enabled"] is False
