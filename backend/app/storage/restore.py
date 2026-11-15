"""Восстановление состояния из PostgreSQL при старте (create_app), вне горячего пути.

Порядок: указатели service_state → справочники и матрица из inputs (тела PUT, как пришли) →
дни текущего поколения справочников из day_state (DayState целиком: наряды, отмены, overrides,
отсутствующие, принятые события, текущий план, режимы планировщика) → отметки сотрудников (marks)
→ состояние демо-проигрывателя /ui. Контекст планировщика (DayContext) отдельно хранить не нужно:
apply_event строит его заново из DayState при каждом событии, поэтому после восстановления
события продолжают версию плана без пересчёта дня.

Если журнал (plan_versions/events) ушёл дальше снимка (сбой между записями), недостающие события
проигрываются заново из таблицы events тем же post_event — plan_version не откатывается.
Любая ошибка дня — лог и пропуск этого дня; ошибка целиком — пустой старт. Сервис не падает.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time
import zlib

from .db import db

log = logging.getLogger("storage.restore")


def restore_enabled() -> bool:
    return (os.environ.get("STATE_RESTORE") or "on").strip().lower() not in ("off", "0", "false", "no")


def _wait_sec() -> float:
    try:
        return max(0.0, float(os.environ.get("STATE_RESTORE_WAIT_SEC", "5")))
    except ValueError:
        return 10.0


def _ts(value):
    return dt.datetime.fromisoformat(value) if value else None


def _input(sha: str) -> bytes | None:
    rows = db.query("SELECT payload FROM inputs WHERE sha256 = %s", (sha,))
    return zlib.decompress(bytes(rows[0]["payload"])) if rows else None


def load_day(p: dict):
    """DayState из payload day_state (state_repo.dump_day). Бросает при повреждённых данных."""
    from ..models import CancelledOrder, Order, Plan
    from ..state import DayState

    orders = {}
    for o in p["orders"]:
        order = Order.model_validate(o)
        orders[order.order_id] = order
    plan = Plan.model_validate(p["plan"]) if p.get("plan") else None
    version = int(p["plan_version"])
    if plan is not None and plan.plan_version != version:
        raise ValueError(f"plan_version снимка {version} не совпадает с планом {plan.plan_version}")
    if plan is None and version:
        raise ValueError("в снимке нет плана при plan_version > 0")
    return DayState(
        date=dt.date.fromisoformat(p["date"]),
        now=_ts(p["now"]),
        orders=orders,
        absent_staff_ids=set(p.get("absent_staff_ids") or ()),
        arrived_at={k: _ts(v) for k, v in (p.get("arrived_at") or {}).items()},
        cancelled={k: CancelledOrder.model_validate(v) for k, v in (p.get("cancelled") or {}).items()},
        plan=plan,
        plan_version=version,
        model_time=_ts(p.get("model_time")),
        events={k: int(v) for k, v in (p.get("events") or {}).items()},
        visit_end_overrides={k: _ts(v) for k, v in (p.get("visit_end_overrides") or {}).items()},
        load_id=p["load_id"],
    )


def _replay_missing(svc, day) -> int:
    """События журнала новее снимка (снимок не успел записаться) — проиграть заново."""
    rows = db.query("SELECT max(version) AS v FROM plan_versions WHERE date = %s", (day.date,))
    top = rows[0]["v"] if rows and rows[0]["v"] is not None else 0
    if top <= day.plan_version:
        return 0
    if svc.store.matrix is None:
        log.warning("Восстановление %s: журнал на версии %d, снимок на %d, но матрицы нет — события не "
                    "проигрываются", day.date, top, day.plan_version)
        return 0
    events = db.query(
        "SELECT event_id, payload FROM events WHERE date = %s AND plan_version > %s ORDER BY plan_version",
        (day.date, day.plan_version),
    )
    n = 0
    for row in events:
        if row["event_id"] in svc.store.days[day.date].events:
            continue
        svc.post_event(day.date.isoformat(), json.dumps(row["payload"], ensure_ascii=False).encode("utf-8"))
        n += 1
    log.warning("Восстановление %s: снимок отставал от журнала (%d < %d), проиграно событий: %d",
                day.date, day.plan_version, top, n)
    return n


def _marks(load_id: str) -> dict[str, dict[str, dict]]:
    out: dict[str, dict[str, dict]] = {}
    for row in db.query("SELECT mark_key, kind, payload FROM marks WHERE load_id = %s", (load_id,)):
        out.setdefault(row["mark_key"], {})[row["kind"]] = row["payload"]
    return out


def restore(app) -> dict:
    """Восстановить состояние app.state.service из БД. Возвращает краткий отчёт (и пишет его в лог)."""
    report: dict = {"restored": False, "days": [], "skipped": []}
    if not db.enabled or not restore_enabled():
        return report
    svc = app.state.service
    with svc.store.lock:
        if svc.store.reference is not None or svc.store.days:
            report["reason"] = "state_not_empty"
            return report
    started = time.perf_counter()
    if not db.wait_ready(_wait_sec()):
        log.warning("Восстановление состояния пропущено: БД недоступна (старт с пустым состоянием)")
        report["reason"] = "db_unavailable"
        return report
    try:
        _restore(app, svc, report)
    except Exception:
        log.exception("Восстановление состояния не удалось: старт с пустым состоянием")
        with svc.store.lock:
            svc.store.reference = None
            svc.store.matrix = None
            svc.store.days.clear()
            svc.store.ref_gen = None
        report.update(restored=False, days=[], reason="error")
        return report
    report["ms"] = int((time.perf_counter() - started) * 1000)
    if report["restored"]:
        log.info("Восстановлено из БД за %d мс: дни %s, пропущены %s", report["ms"], report["days"], report["skipped"])
    return report


def _restore(app, svc, report: dict) -> None:
    ptr = {r["key"]: r["value"] for r in db.query("SELECT key, value FROM service_state")}
    ref_ptr = ptr.get("reference")
    if not ref_ptr:
        report["reason"] = "nothing_saved"
        return
    raw = _input(ref_ptr["sha256"])
    if raw is None:
        raise ValueError("тело справочников не найдено в inputs")
    ref = svc.load_reference(raw)
    matrix = None
    if ptr.get("matrix"):
        raw_m = _input(ptr["matrix"]["sha256"])
        matrix = svc.load_matrix(raw_m) if raw_m is not None else None
    with svc.store.lock:
        svc.store.reference = ref
        svc.store.matrix = matrix
        svc.store.ref_gen = ref_ptr.get("gen")
    svc.store.notify_reference(ref)  # справочник сотрудников и аккаунты по восстановленным справочникам
    report.update(restored=True, matrix=matrix is not None)

    rows = db.query(
        "SELECT date, load_id, scenario, payload, demo, updated_at FROM day_state WHERE ref_gen = %s ORDER BY date",
        (ref_ptr.get("gen"),),
    )
    demo_row = None
    for row in rows:
        try:
            day = load_day(row["payload"])
            modes = row["payload"].get("planner_modes")
            with svc.store.lock:
                svc.store.days[day.date] = day
                if modes is not None and hasattr(svc.planner, "_day_modes"):
                    svc.planner._day_modes[day.date] = (bool(modes[0]), bool(modes[1]))
            replayed = _replay_missing(svc, day) if day.plan is not None else 0
            marks = _marks(day.load_id)
            from ..employee_api import seed_marks

            with svc.store.lock:
                seed_marks(app, svc, svc.store.days[day.date], marks)
            report["days"].append({"date": day.date.isoformat(), "plan_version": svc.store.days[day.date].plan_version,
                                   "marks": sum(len(v) for v in marks.values()), "replayed": replayed})
            if row["demo"] and (demo_row is None or row["updated_at"] >= demo_row["updated_at"]):
                demo_row = row
        except Exception as exc:
            with svc.store.lock:
                svc.store.days.pop(row["date"], None)
            log.exception("Восстановление дня %s пропущено: данные повреждены", row["date"])
            report["skipped"].append({"date": str(row["date"]), "error": exc.__class__.__name__})
    if demo_row is not None:
        try:
            from ..ui_api import restore_demo

            restore_demo(app, demo_row["demo"])
            report["demo"] = demo_row["demo"].get("scenario")
        except Exception:
            log.exception("Состояние демо-проигрывателя не восстановлено (день и план восстановлены)")
