"""Подключение к PostgreSQL, миграции схемы и фоновая очередь записи (write-behind).

* Все записи идут через одну очередь в одном фоновом потоке, поэтому порядок сохраняется
  (журнал истории, снимки дней, отметки, действия). Вызов submit() не блокируется и не бросает.
* Операция — функция fn(conn, *args), выполняется в транзакции. Обрыв связи
  (OperationalError/InterfaceError) — переподключение с нарастающей паузой и повтор ТОЙ ЖЕ
  операции (ничего не теряется и не переставляется). Прочие ошибки — лог и пропуск операции.
* coalesce_key: если в очереди уже есть более свежая операция с тем же ключом, старая
  пропускается (снимок дня пишется один раз на серию событий; порядок с остальными не страдает,
  потому что снимок — идемпотентный upsert последнего состояния).
* Переполнение очереди (БД долго недоступна) — запись отбрасывается, счётчик dropped и
  предупреждение в логе не чаще раза в минуту. Без DATABASE_URL или без psycopg всё выключено.
* Миграции: нумерованные файлы migrations/NNNN_*.sql, применяются при подключении под
  advisory-lock, каждая в своей транзакции, факт применения — в schema_migrations.
"""

from __future__ import annotations

import atexit
import itertools
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("storage")

QUEUE_MAX = 10_000
CONNECT_TIMEOUT_SEC = 3
MAX_BACKOFF_SEC = 30
MAX_ITEM_RETRIES = 5  # повторов одной операции при живом соединении
MIGRATIONS_DIR = Path(__file__).with_name("migrations")
_LOCK_ID = 7_274_010  # pg_advisory_lock: миграции не применяются параллельно двумя процессами


class DbUnavailable(Exception):
    """БД не настроена или недоступна."""


def migrations() -> list[tuple[int, str, str]]:
    out = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = re.match(r"(\d+)_(.+)\.sql$", path.name)
        if m:
            out.append((int(m.group(1)), m.group(2), path.read_text(encoding="utf-8")))
    return out


def apply_migrations(conn) -> list[int]:
    """Идемпотентно применяет недостающие миграции; возвращает применённые сейчас версии."""
    done: list[int] = []
    conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version integer PRIMARY KEY, "
            "name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations").fetchall()}
        for version, name, sql in migrations():
            if version in applied:
                continue
            with conn.transaction():
                conn.execute(sql)
                conn.execute("INSERT INTO schema_migrations (version, name) VALUES (%s, %s)", (version, name))
            done.append(version)
            log.info("БД: применена миграция %04d_%s", version, name)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
    return done


class Database:
    def __init__(self, url: str | None) -> None:
        self.url = (url or "").strip() or None
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._ready = threading.Event()  # соединение есть, миграции применены
        self._pool = None
        self._seq = itertools.count(1)
        self._latest: dict[Any, int] = {}  # coalesce_key -> seq последней поставленной операции
        self._last_warn = 0.0
        self.counters = {"submitted": 0, "written": 0, "coalesced": 0, "dropped": 0, "failed": 0, "retries": 0}
        self.last_error: str | None = None
        self.last_ok_at: float | None = None
        self.schema_version: int | None = None
        self._no_driver = False
        self._inflight = 0  # поставлено, но ещё не обработано (записано, пропущено или отброшено ошибкой)

    # ------------------------------------------------ состояние

    @property
    def enabled(self) -> bool:
        return self.url is not None and not self._no_driver

    @property
    def ready(self) -> bool:
        return self.enabled and self._ready.is_set() and self._pool is not None

    def wait_ready(self, timeout: float) -> bool:
        if not self.enabled:
            return False
        self.start()
        return self._ready.wait(timeout) and self._pool is not None

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "ready": self.ready,
            "schema_version": self.schema_version,
            "queued": self.queue.qsize(),
            **self.counters,
            "last_error": self.last_error,
            "last_ok_sec_ago": round(time.monotonic() - self.last_ok_at, 1) if self.last_ok_at else None,
        }

    def _warn(self, msg: str, *args) -> None:
        """Предупреждения не чаще раза в 60 с, чтобы недоступная БД не засоряла лог."""
        now = time.monotonic()
        if now - self._last_warn >= 60 or self._last_warn == 0.0:
            self._last_warn = now
            log.warning(msg, *args)

    # ------------------------------------------------ жизненный цикл

    def start(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._thread is not None:
                return
            try:
                import psycopg  # noqa: F401
                import psycopg_pool  # noqa: F401
            except ImportError as exc:
                self._no_driver = True
                log.warning("БД отключена: нет пакета psycopg (%s)", exc)
                return
            self._thread = threading.Thread(target=self._run, name="db-writer", daemon=True)
            self._thread.start()
            atexit.register(self._atexit)

    def _atexit(self) -> None:
        """При останове процесса дописать очередь (не дольше DB_FLUSH_ON_EXIT_SEC, если БД доступна)."""
        if self._inflight and self._ready.is_set():
            try:
                timeout = float(os.environ.get("DB_FLUSH_ON_EXIT_SEC", "10"))
            except ValueError:
                timeout = 10.0
            if not self.flush(timeout):
                log.warning("БД: при останове не записано операций: %d", self._inflight)

    def _connect(self):
        import psycopg

        conn = psycopg.connect(self.url, connect_timeout=CONNECT_TIMEOUT_SEC, autocommit=True)
        apply_migrations(conn)
        row = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()
        self.schema_version = row[0] if row else None
        if self._pool is None:
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                self.url, min_size=1, max_size=3, open=False, timeout=CONNECT_TIMEOUT_SEC,
                check=ConnectionPool.check_connection,  # после рестарта БД не отдавать «мёртвые» соединения
                kwargs={"connect_timeout": CONNECT_TIMEOUT_SEC, "autocommit": True},
            )
            self._pool.open()
        self._ready.set()
        self.last_ok_at = time.monotonic()
        log.info("БД: подключено к PostgreSQL, схема v%s", self.schema_version)
        return conn

    def _run(self) -> None:
        import psycopg

        conn = None
        backoff = 1.0
        item = None
        attempts = 0  # повторы текущей операции
        connected_since_fail = False
        while True:
            try:
                if conn is None or conn.closed:
                    self._ready.clear()
                    conn = self._connect()
                    backoff = 1.0
                    connected_since_fail = True
                if item is None:
                    item = self.queue.get(timeout=10)
                self._handle(conn, item)
                item, attempts = None, 0
                self._done()
            except queue.Empty:
                try:  # заодно замечаем обрыв соединения в простое
                    conn.execute("SELECT 1")
                except Exception:
                    conn = None
            except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                self._ready.clear()
                self.last_error = f"{exc.__class__.__name__}: {str(exc).strip()[:200]}"
                if item is not None:
                    self.counters["retries"] += 1
                    attempts += 1
                    if attempts > MAX_ITEM_RETRIES and connected_since_fail:
                        # связь восстанавливалась, а операция снова падает — дело в ней, а не в сети
                        log.error("БД: операция пропущена после %d повторов (%s)", attempts, self.last_error)
                        self.counters["failed"] += 1
                        item, attempts = None, 0
                        self._done()
                connected_since_fail = False
                self._warn("БД недоступна (%s); повтор через %.0f с, в очереди %d",
                           self.last_error, backoff, self.queue.qsize())
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass
                conn = None
                time.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_SEC)
            except Exception as exc:
                self.counters["failed"] += 1
                self.last_error = f"{exc.__class__.__name__}: {str(exc).strip()[:200]}"
                if item is not None:
                    log.exception("БД: операция пропущена из-за ошибки")
                    item, attempts = None, 0
                    self._done()
                else:  # ошибка подключения/миграции: не крутимся в цикле
                    log.exception("БД: ошибка подключения или миграции; повтор через %.0f с", backoff)
                    self._ready.clear()
                    try:
                        if conn is not None:
                            conn.close()
                    except Exception:
                        pass
                    conn = None
                    time.sleep(backoff)
                    backoff = min(backoff * 2, MAX_BACKOFF_SEC)

    def _done(self) -> None:
        with self._lock:
            self._inflight -= 1

    def _handle(self, conn, item: tuple) -> None:
        seq, key, fn, args = item
        if key is not None:
            with self._lock:
                if self._latest.get(key, seq) != seq:
                    self.counters["coalesced"] += 1
                    return  # в очереди есть более свежая операция с тем же ключом
        with conn.transaction():
            fn(conn, *args)
        if key is not None:
            with self._lock:
                if self._latest.get(key) == seq:
                    del self._latest[key]
        self.counters["written"] += 1
        self.last_ok_at = time.monotonic()

    # ------------------------------------------------ постановка в очередь

    def note_dropped(self) -> None:
        self.counters["dropped"] += 1
        self._warn("БД: очередь записи переполнена, записи отбрасываются (всего %d)", self.counters["dropped"])

    def submit(self, fn: Callable, *args, coalesce_key: Any = None) -> bool:
        """Поставить fn(conn, *args) в очередь записи. Не блокируется; False — запись не принята."""
        if not self.enabled:
            return False
        if self._thread is None:
            self.start()
            if not self.enabled:
                return False
        with self._lock:
            seq = next(self._seq)
            try:
                self.queue.put_nowait((seq, coalesce_key, fn, args))
            except queue.Full:
                self.note_dropped()
                return False
            self._inflight += 1
            if coalesce_key is not None:
                self._latest[coalesce_key] = seq
        self.counters["submitted"] += 1
        return True

    def flush(self, timeout: float = 10.0) -> bool:
        """Дождаться, пока все поставленные операции обработаны (для проверок и останова)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._inflight == 0:
                return True
            time.sleep(0.05)
        return False

    # ------------------------------------------------ чтение (не горячий путь)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        if not self.ready:
            raise DbUnavailable("БД недоступна")
        from psycopg.rows import dict_row

        try:
            with self._pool.connection(timeout=CONNECT_TIMEOUT_SEC) as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute(sql, params)
                    return cur.fetchall() if cur.description else []
        except Exception as exc:
            raise DbUnavailable(f"Ошибка чтения БД: {exc.__class__.__name__}") from exc


db = Database(os.environ.get("DATABASE_URL"))
