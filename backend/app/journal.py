"""Журнал истории в PostgreSQL (вне горячего пути).

Сервис считает всё в памяти; сюда только дописываются факты для графиков и разбора.
Запись идёт через общую очередь слоя хранения (app/storage/db.py): один фоновый поток,
порядок сохраняется, вызовы record_* не блокируются и не бросают исключений. Нет DATABASE_URL,
БД недоступна или очередь переполнена — запись откладывается/пропускается (в лог пишется
предупреждение), API работает как обычно. Схема таблиц — миграция storage/migrations/0001_journal.sql.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import queue
from typing import Any

from .storage.db import DbUnavailable, db
from .storage.state_repo import plan_json

log = logging.getLogger("journal")

CONNECT_TIMEOUT_SEC = 3


class JournalUnavailable(Exception):
    """БД не настроена или недоступна (читающие эндпоинты отвечают 503)."""


def _metrics(plan) -> dict[str, Any]:
    s = plan.summary
    visits = sum(1 for r in plan.routes for st in r.stops if st.type == "VISIT")
    pgp = sum(len(g.order_ids) for g in plan.pgp_groups)
    late = sum(1 for a in plan.alerts if a.type == "LATE_RISK" and a.status == "ACTIVE")

    def pick(value, fallback):
        return value if value is not None else fallback

    return {
        "visits_planned": pick(s.visits_planned if s else None, visits),
        "unassigned": pick(s.unassigned if s else None, len(plan.unassigned)),
        "pgp_orders": pick(s.pgp_orders if s else None, pgp),
        "cancelled": pick(s.cancelled if s else None, len(plan.cancelled)),
        "late_risk_active": late,
        "travel_sec_total": pick(s.travel_sec_total if s else None,
                                 sum(r.totals.travel_sec for r in plan.routes if r.totals)),
        "utilization_cv": s.utilization_cv if s else None,
    }


class Journal:
    def __init__(self, url: str | None) -> None:
        self.url = (url or "").strip() or None
        self._q: queue.Queue = db.queue  # тесты подменяют, чтобы проверить переполнение
        self._dropped = 0

    @property
    def enabled(self) -> bool:
        return self.url is not None

    def start(self) -> None:
        if not self.enabled:
            log.warning("DATABASE_URL не задан: журнал истории и восстановление состояния отключены")
            return
        db.start()

    # ------------------------------------------------ постановка в очередь

    def _put(self, item: tuple) -> None:
        if not self.enabled:
            return
        if self._q is db.queue:
            db.submit(self._handle, item)
            return
        try:  # подменённая очередь (тесты): только постановка, без записи
            self._q.put_nowait(item)
        except queue.Full:
            self._dropped += 1
            log.warning("Журнал истории: очередь переполнена, записи отбрасываются (всего %d)", self._dropped)

    def record_day(self, date: dt.date, now: dt.datetime, orders_total: int) -> None:
        self._put(("day", date, now, orders_total, dt.datetime.now(dt.timezone.utc)))

    def record_plan(self, plan, util: dict | None = None) -> None:
        """Первая версия плана (после POST /plan). util — разбивка загрузки по профилям."""
        self._put(("plan", plan, util))

    def record_event(self, date: dt.date, event, plan, processing_ms: int, util: dict | None = None) -> None:
        """Принятое (не дубликат) событие вместе с порождённой версией плана."""
        self._put(("event", date, event, plan, processing_ms, util))

    # ------------------------------------------------ запись (в потоке worker)

    def _handle(self, conn, item: tuple) -> None:
        from psycopg.types.json import Jsonb

        kind = item[0]
        if kind == "day":
            _, date, now, total, loaded_at = item
            with conn.transaction():
                # День в памяти заменён целиком — прежняя история этой даты неактуальна.
                for table in ("plan_versions", "events", "alerts"):
                    conn.execute(f"DELETE FROM {table} WHERE date = %s", (date,))
                conn.execute(
                    'INSERT INTO days (date, "now", loaded_at, orders_total) VALUES (%s, %s, %s, %s) '
                    'ON CONFLICT (date) DO UPDATE SET "now" = EXCLUDED."now", '
                    "loaded_at = EXCLUDED.loaded_at, orders_total = EXCLUDED.orders_total",
                    (date, now, loaded_at, total),
                )
            return

        if kind == "plan":
            plan, event, util = item[1], None, item[2]
        else:  # "event"
            _, date, event, plan, processing_ms, util = item

        m = _metrics(plan)
        with conn.transaction():
            conn.execute(
                "INSERT INTO plan_versions (date, version, as_of, based_on_event_id, computation_ms, "
                "visits_planned, unassigned, pgp_orders, cancelled, late_risk_active, "
                "travel_sec_total, utilization_cv, utilization_profiles, plan) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (date, version) DO UPDATE SET as_of=EXCLUDED.as_of, "
                "based_on_event_id=EXCLUDED.based_on_event_id, computation_ms=EXCLUDED.computation_ms, "
                "visits_planned=EXCLUDED.visits_planned, unassigned=EXCLUDED.unassigned, "
                "pgp_orders=EXCLUDED.pgp_orders, cancelled=EXCLUDED.cancelled, "
                "late_risk_active=EXCLUDED.late_risk_active, travel_sec_total=EXCLUDED.travel_sec_total, "
                "utilization_cv=EXCLUDED.utilization_cv, utilization_profiles=EXCLUDED.utilization_profiles, "
                "plan=EXCLUDED.plan",
                (plan.date, plan.plan_version, plan.as_of, plan.based_on_event_id, plan.computation_ms,
                 m["visits_planned"], m["unassigned"], m["pgp_orders"], m["cancelled"],
                 m["late_risk_active"], m["travel_sec_total"], m["utilization_cv"],
                 Jsonb(util) if util is not None else None, Jsonb(plan_json(plan))),
            )
            if event is not None:
                conn.execute(
                    "INSERT INTO events (date, event_id, ts, type, payload, accepted, duplicate, "
                    "processing_ms, plan_version) VALUES (%s,%s,%s,%s,%s,true,false,%s,%s) "
                    "ON CONFLICT (date, event_id) DO NOTHING",
                    (date, event.event_id, event.timestamp, event.type,
                     Jsonb(event.model_dump(mode="json")), processing_ms, plan.plan_version),
                )
            rows = [
                (plan.date, a.alert_id, a.created_at, a.type.value if hasattr(a.type, "value") else a.type,
                 a.status.value if hasattr(a.status, "value") else a.status, a.order_id,
                 Jsonb(a.model_dump(mode="json")))
                for a in plan.alerts
            ]
            if rows:
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO alerts (date, alert_id, created_at, type, status, order_id, payload) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (date, alert_id) DO UPDATE SET "
                        "status=EXCLUDED.status, payload=EXCLUDED.payload",
                        rows,
                    )

    # ------------------------------------------------ чтение (для /ui/history)

    def query(self, sql: str, params: tuple = ()) -> list[dict]:
        if not self.enabled:
            raise JournalUnavailable("БД журнала недоступна")
        try:
            return db.query(sql, params)
        except DbUnavailable as exc:
            raise JournalUnavailable(str(exc) if "Ошибка" in str(exc) else "БД журнала недоступна") from None


journal = Journal(os.environ.get("DATABASE_URL"))
