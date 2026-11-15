#!/usr/bin/env python3
"""Бенч первоначального плана на одном обучающем дне.

    python tools/bench/run_day.py [YYYY-MM-DD] [--planner pyvrp] [--time-limit 300] [--budget 60]

Берёт наряды из data/synth/orders_train.json со слотом в этот день и
registered_at ≤ now (08:30), строит план планировщиком напрямую (без HTTP),
проверяет его validator.py и печатает метрики.
Без даты берётся демо-день data/synth (date из events_dense.json).
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.models import (  # noqa: E402
    Order,
    ReferenceData,
    StopType,
    TravelMatrix,
)
from app.service import _build_reference  # noqa: E402
from app.state import DayState, MatrixState  # noqa: E402

DATA = ROOT / "data" / "synth"
TZ = dt.timezone(dt.timedelta(hours=5))


def _csv_value(field: str, value: str):
    if value == "null" or value == "":
        return None
    if field == "profiles":
        return value.split("|")
    if field in ("duration_min", "max_start_delay_min", "deadline_value", "repeat_deadline_value"):
        return int(value)
    if field == "pgp_relevant":
        return value == "true"
    return value


def load_reference():
    with open(DATA / "work_types.csv", encoding="utf-8", newline="") as fh:
        work_types = [{k: _csv_value(k, v) for k, v in row.items()} for row in csv.DictReader(fh)]
    data = ReferenceData.model_validate(
        {
            "service_centers": json.loads((DATA / "service_centers.json").read_text(encoding="utf-8")),
            "staff": json.loads((DATA / "staff.json").read_text(encoding="utf-8")),
            "access_nodes": json.loads((DATA / "access_nodes.json").read_text(encoding="utf-8")),
            "work_types": work_types,
        }
    )
    ref = _build_reference(data)
    tm = TravelMatrix.model_validate_json((DATA / "travel_matrix.json").read_bytes())
    matrix = MatrixState(matrix=tm, index={p: i for i, p in enumerate(tm.points)})
    return ref, matrix


def load_day(date: dt.date, now: dt.datetime) -> DayState:
    raw = json.loads((DATA / "orders_train.json").read_text(encoding="utf-8"))
    orders = {}
    for item in raw:
        o = Order.model_validate(item)
        if o.slot.start.astimezone(TZ).date() == date and o.registered_at <= now:
            orders[o.order_id] = o
    return DayState(date=date, now=now, orders=orders, absent_staff_ids=set(),
                    arrived_at={oid: now for oid in orders})


def _util_group(values: list[float]) -> dict:
    """cv/mean/min/max и хвосты по загрузкам группы (пустые маршруты входят с 0)."""
    if not values:
        return {"n": 0}
    mean = statistics.fmean(values)
    return {
        "n": len(values),
        "cv": round(statistics.pstdev(values) / mean, 3) if len(values) > 1 and mean > 0 else 0.0,
        "mean": round(mean, 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "empty": sum(1 for u in values if u == 0),
        "lt30": sum(1 for u in values if u < 0.3),
        "gt90": sum(1 for u in values if u > 0.9),
    }


def metrics(plan, ref, matrix, day) -> dict:
    idx, dur = matrix.index, matrix.matrix.durations_sec
    travel = 0
    utils = []
    by_profile: dict[str, list[float]] = {}
    all_utils: list[float] = []
    visits = 0
    pairs = 0
    weighted_ok = 0
    for r in plan.routes:
        stops = r.stops
        rt = sum(dur[idx[a.location_id]][idx[b.location_id]] for a, b in zip(stops, stops[1:]))
        travel += rt
        work = 0
        for s in stops:
            if s.type == StopType.VISIT:
                work += (s.end - s.start).total_seconds()
                if s.order_id not in day.orders:
                    pairs += 1  # визит напарника совместного наряда (ORD-…-B): пара — один наряд
                    continue
                visits += 1
                weighted_ok += 2 if day.orders[s.order_id].is_repeat else 1
        st = ref.staff[r.staff_id].shift
        h = lambda x: int(x[:2]) * 3600 + int(x[3:]) * 60  # noqa: E731
        u = (work + rt) / (h(st.end) - h(st.start) - st.break_duration_min * 60) if work else 0.0
        if any(s.type == StopType.VISIT for s in stops):
            utils.append(u)
        if r.status == "ACTIVE":
            all_utils.append(u)
            by_profile.setdefault("/".join(ref.staff[r.staff_id].profiles), []).append(u)
    total = len(day.orders)
    pgp = sum(len(g.order_ids) for g in plan.pgp_groups)
    weighted_total = sum(2 if o.is_repeat else 1 for o in day.orders.values())
    cv = statistics.pstdev(utils) / statistics.fmean(utils) if len(utils) > 1 else 0.0
    return {
        "orders": total,
        "visits": visits,
        "joint_pairs": pairs,
        "unassigned": len(plan.unassigned),
        "pgp": pgp,
        "in_slot_share": visits / total if total else 0.0,
        "in_slot_share_excl_pgp": visits / (total - pgp) if total - pgp else 0.0,
        "weighted_share": weighted_ok / weighted_total if weighted_total else 0.0,
        "travel_h": travel / 3600,
        "staff_used": len(utils),
        "util_mean": statistics.fmean(utils) if utils else 0.0,
        "util_cv": cv,
        "util_all": _util_group(all_utils),
        "util_by_profile": {p: _util_group(v) for p, v in sorted(by_profile.items())},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("date", nargs="?", help="день; по умолчанию date из data/synth/events_dense.json")
    ap.add_argument("--now", default="08:30")
    ap.add_argument("--planner", default=os.environ.get("PLANNER", "pyvrp"))
    ap.add_argument("--time-limit", type=int, default=300, help="PlanRequest.time_limit_sec")
    ap.add_argument("--budget", type=float, help="PLANNER_DAY_BUDGET_SEC")
    ap.add_argument("--break-mode", help="PLANNER_BREAK_MODE: shift_duration|shorten|none")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--json", action="store_true", help="вывести метрики одной строкой JSON")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.budget is not None:
        os.environ["PLANNER_DAY_BUDGET_SEC"] = str(args.budget)
    if args.break_mode:
        os.environ["PLANNER_BREAK_MODE"] = args.break_mode

    from app.planner import make_planner
    from app.planner.validator import validate_plan

    args.date = args.date or json.loads((DATA / "events_dense.json").read_text(encoding="utf-8"))["date"]
    date = dt.date.fromisoformat(args.date)
    now = dt.datetime.combine(date, dt.time.fromisoformat(args.now), tzinfo=TZ)
    ref, matrix = load_reference()
    day = load_day(date, now)
    planner = make_planner(args.planner)
    if hasattr(planner, "seed"):
        planner.seed = args.seed

    started = time.perf_counter()
    plan = planner.build_plan(ref, matrix, day, time_limit_sec=args.time_limit)
    elapsed = time.perf_counter() - started
    violations = validate_plan(plan, ref, matrix, day)
    m = metrics(plan, ref, matrix, day)
    m.update({"date": args.date, "violations": len(violations), "solve_sec": round(elapsed, 2)})
    stats = getattr(planner, "last_stats", {})
    reasons: dict[str, int] = {}
    for u in plan.unassigned:
        reasons[u.reason_code.value] = reasons.get(u.reason_code.value, 0) + 1

    if args.json:
        print(json.dumps({**m, "reasons": reasons, "stats": stats}, ensure_ascii=False))
    else:
        print(f"День {args.date}, now {args.now}, планировщик {args.planner}")
        print(f"  нарядов: {m['orders']}  размещено: {m['visits']}  unassigned: {m['unassigned']}  ПГП: {m['pgp']}"
              f"  совместных пар: {m['joint_pairs']} (JOINT_ORDERS={os.environ.get('JOINT_ORDERS', 'off')})")
        print(f"  причины unassigned: {reasons}")
        print(f"  нарушений H1–H9 и H-J: {len(violations)}")
        for v in violations[:10]:
            print(f"    {v}")
        print(f"  доля нарядов в слот: {m['in_slot_share']:.3f} (без ПГП {m['in_slot_share_excl_pgp']:.3f}, "
              f"взвешенно {m['weighted_share']:.3f})")
        print(f"  время в пути: {m['travel_h']:.1f} ч; задействовано сотрудников: {m['staff_used']}")
        print(f"  загрузка задействованных: средняя {m['util_mean']:.3f}, коэффициент вариации {m['util_cv']:.3f}")
        for name, g in [("все на смене", m["util_all"])] + list(m["util_by_profile"].items()):
            if not g.get("n"):
                continue
            print(f"  загрузка [{name}] n={g['n']}: cv {g['cv']:.3f}, средняя {g['mean']:.3f}, "
                  f"min {g['min']:.3f}, max {g['max']:.3f}; пустых {g['empty']}, <30% {g['lt30']}, >90% {g['gt90']}")
        print(f"  время решения: {elapsed:.1f} с; этапы: {stats}")
    return 0 if not violations else 1


if __name__ == "__main__":
    sys.exit(main())
