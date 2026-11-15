"""Репозитории «ключ → JSON-значение» с файловой и PostgreSQL-реализацией (заготовка под часть 2).

Интерфейс один: get / put / delete / all. Выбор — make_repo(): БД готова (db.wait_ready) →
PgRepo, иначе FileRepo. PgRepo читает таблицу целиком один раз при создании (старт сервиса),
дальше отдаёт из памяти, а put/delete пишет фоном через очередь db (горячий путь не ждёт БД).
Таблицы: accounts(login), staff_profiles(staff_id), settings(key) — migrations/0002_state.sql.
Репозитории используют auth.py (accounts + settings[auth_secret]), staff_directory.py (staff_profiles) и
runtime_settings.py (settings[runtime_overrides]); при БД они ходят через pg_repo(), без БД остаются свои
JSON-файлы. Запись идёт через очередь db; изменения управления (аккаунты, справочник, настройки) после put
ждут запись через flush() (синхронно, с таймаутом) — редкие операции, горячий путь не затрагивается.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Protocol

from .db import DbUnavailable, db

log = logging.getLogger("storage.repos")

TABLES = {"accounts": "login", "staff_profiles": "staff_id", "settings": "key"}


class Repo(Protocol):
    backend: str

    def get(self, key: str, default: Any = None) -> Any: ...
    def put(self, key: str, value: Any) -> None: ...
    def delete(self, key: str) -> None: ...
    def all(self) -> dict[str, Any]: ...


class FileRepo:
    """JSON-файл {key: value}; запись атомарная (временный файл + os.replace). Нет файла — пусто."""

    backend = "file"

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self._data = raw if isinstance(raw, dict) else {}
            except FileNotFoundError:
                self._data = {}
            except (OSError, ValueError) as exc:
                log.warning("Файл %s не прочитан (%s): пустой репозиторий", self.path, exc)
                self._data = {}
        return self._data

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".repo-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("Файл %s не сохранён (%s): значения только в памяти", self.path, exc)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._load().get(key, default))

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._load()[key] = copy.deepcopy(value)
            self._save()

    def delete(self, key: str) -> None:
        with self._lock:
            if self._load().pop(key, None) is not None:
                self._save()

    def all(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._load())


def _pg_put(conn, table: str, col: str, key: str, value: Any) -> None:
    from psycopg.types.json import Jsonb

    conn.execute(
        f"INSERT INTO {table} ({col}, value, updated_at) VALUES (%s, %s, now()) "
        f"ON CONFLICT ({col}) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        (key, Jsonb(value)),
    )


def _pg_delete(conn, table: str, col: str, key: str) -> None:
    conn.execute(f"DELETE FROM {table} WHERE {col} = %s", (key,))


class PgRepo:
    """Таблица PostgreSQL: кэш в памяти + запись фоном. Создавать, когда db.ready (иначе DbUnavailable)."""

    backend = "postgres"

    def __init__(self, table: str) -> None:
        if table not in TABLES:
            raise ValueError(f"неизвестная таблица {table}")
        self.table, self.col = table, TABLES[table]
        self._lock = threading.RLock()
        rows = db.query(f"SELECT {self.col} AS k, value FROM {table}")  # бросает DbUnavailable
        self._data: dict[str, Any] = {r["k"]: r["value"] for r in rows}

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def put(self, key: str, value: Any) -> None:
        value = copy.deepcopy(value)
        with self._lock:
            self._data[key] = value
            db.submit(_pg_put, self.table, self.col, key, value)

    def delete(self, key: str) -> None:
        with self._lock:
            if self._data.pop(key, None) is not None:
                db.submit(_pg_delete, self.table, self.col, key)

    def all(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)


def make_repo(table: str, file_path: Path, wait_sec: float = 5.0) -> Repo:
    """PgRepo, если БД настроена и готова (ждём не дольше wait_sec), иначе FileRepo."""
    if db.enabled and db.wait_ready(wait_sec):
        try:
            return PgRepo(table)
        except DbUnavailable as exc:
            log.warning("Репозиторий %s: БД недоступна (%s), используется файл %s", table, exc, file_path)
    return FileRepo(file_path)


# ---------------------------------------------------------------- общий доступ для модулей

_repos: dict[str, PgRepo] = {}
_repos_lock = threading.Lock()
_waited = False
SYNC_TIMEOUT_SEC = 3.0


def pg_repo(table: str, wait_sec: float = 5.0) -> PgRepo | None:
    """Общий PgRepo таблицы (кэш процесса) или None, если БД не настроена/недоступна — тогда вызывающий
    остаётся на своём JSON-файле. Долго ждём БД только при первом обращении (дальше 0,3 с)."""
    global _waited
    if not db.enabled:
        return None
    with _repos_lock:
        repo = _repos.get(table)
        if repo is not None:
            return repo
        wait = 0.3 if _waited else wait_sec
        _waited = True
        try:
            if not db.wait_ready(wait):
                log.warning("БД недоступна: %s хранится в JSON-файле до перезапуска", table)
                return None
            repo = _repos[table] = PgRepo(table)
            return repo
        except DbUnavailable as exc:
            log.warning("Репозиторий %s: БД недоступна (%s), используется файл", table, exc)
            return None


def flush(timeout: float = SYNC_TIMEOUT_SEC) -> bool:
    """Дождаться записи поставленных в очередь операций (не дольше timeout). False — БД недоступна:
    запись остаётся в очереди и догонит при подключении, значения в памяти процесса актуальны."""
    if not db.enabled:
        return True
    if db.flush(timeout):
        return True
    log.warning("БД: запись не подтверждена за %.0f с (в очереди %d) — догонит при подключении",
                timeout, db.queue.qsize())
    return False


def sync_repo(repo: Repo, desired: dict[str, Any], delete_missing: bool = False) -> int:
    """Записать в репозиторий только изменившиеся ключи; число записанных."""
    current = repo.all()
    n = 0
    for k, v in desired.items():
        if current.get(k) != v:
            repo.put(k, v)
            n += 1
    if delete_missing:
        for k in current.keys() - desired.keys():
            repo.delete(k)
            n += 1
    return n


def import_missing(repo: Repo, items: dict[str, Any], label: str) -> int:
    """Однократный импорт из файлов: пишет только отсутствующие в репозитории ключи (идемпотентно)."""
    fresh = {k: v for k, v in items.items() if repo.get(k) is None}
    for k, v in fresh.items():
        repo.put(k, v)
    if fresh:
        flush(10.0)
    log.info("Импорт из файлов в БД (%s): импортировано %d", label, len(fresh))
    print(f"[storage] {label}: импортировано {len(fresh)} из файлов в БД", flush=True)
    return len(fresh)
