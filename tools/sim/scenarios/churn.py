"""Сценарий churn: текучка дня - TIME_TICK, поломки/болезни, задержки, отмены, передачи, поток новых нарядов."""

from __future__ import annotations

import datetime as dt
import math

from simlib.data import ALL_CODES, OrderFactory
from simlib.report import ScenarioResult
from simlib.session import Ctx, DaySession, at, plan_stats, visits_of

NAME = "churn"


def _poisson(rng, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def run(ctx: Ctx) -> ScenarioResult:
    res = ScenarioResult(NAME, ctx.seed)
    rng = ctx.rng(NAME)
    data = ctx.data
    date = ctx.options.get("churn_date") or ctx.data.train_dates()[4]
    now = at(date, "08:30")
    intensity = float(ctx.options.get("churn_intensity") or (8 if ctx.quick else 12))  # событий в модельный час
    absent_share = float(ctx.options.get("churn_absent_share") if ctx.options.get("churn_absent_share") is not None
                         else (0.1 if ctx.quick else 0.2))
    tick_min = int(ctx.options.get("churn_tick_min") or (30 if ctx.quick else 15))
    t_start = at(date, "09:00")
    t_end = at(date, "11:30") if ctx.quick else at(date, "20:00")
    base = data.day_orders(date, now, ctx.scale, rng)
    res.metrics.update({"date": date, "base_orders": len(base), "intensity_per_hour": intensity,
                        "absent_share": absent_share, "tick_min": tick_min,
                        "window": f"{t_start:%H:%M}-{t_end:%H:%M}"})
    s = DaySession(ctx, res, date, now, base, [], time_limit=ctx.time_limit)
    if not s.setup():
        return res
    fac = OrderFactory(data, rng, date)
    busy_nodes = {o["access_node_id"] for o in base if data.wt[o["work_type_code"]]["pgp_relevant"]}
    max_unavail = max(1, int(absent_share * len(data.staff_ids)))
    counts: dict[str, int] = {}
    skipped: dict[str, int] = {}
    timeline: list[dict] = []
    weights = [("NEW_ORDER", 0.40), ("VISIT_DELAYED", 0.20), ("ORDER_CANCELLED", 0.15),
               ("ORDER_TRANSFERRED", 0.10), ("STAFF_UNAVAILABLE", 0.15)]

    def pick_type() -> str:
        x, acc = rng.random(), 0.0
        for name, w in weights:
            acc += w
            if x <= acc:
                return name
        return "NEW_ORDER"

    def build(kind: str, t: dt.datetime) -> dict | None:
        plan = s.plan
        vis = visits_of(plan)
        if kind == "NEW_ORDER":
            code = rng.choice(ALL_CODES)
            pg = data.wt[code]["pgp_relevant"]
            o = fac.make(t, code, avoid_nodes=busy_nodes if pg else None)
            if pg:
                busy_nodes.add(o["access_node_id"])
            return s.new_order(t, o)
        in_prog = [(oid, sid, st) for oid, (sid, st) in vis.items() if st.start <= t < st.end]
        if kind == "VISIT_DELAYED":
            if not in_prog:
                return None
            oid, sid, st = rng.choice(in_prog)
            return s.delayed(t, sid, oid, st.end + dt.timedelta(minutes=rng.randint(10, 45)))
        if kind == "ORDER_TRANSFERRED":
            cand = [x for x in in_prog if x[2].start < t]
            if not cand:
                return None
            oid, sid, st = rng.choice(cand)
            return s.transferred(t, sid, oid, rng.choice(["STATION_EQUIPMENT", "TRUNK_CABLE"]))
        if kind == "ORDER_CANCELLED":
            cand = [oid for oid in s.model.orders if oid not in s.model.cancelled
                    and (oid not in vis or vis[oid][1].start > t)]
            if not cand:
                return None
            return s.cancel(t, rng.choice(sorted(cand)), rng.choice(["NOT_CONFIRMED", "CUSTOMER_REQUEST"]))
        if kind == "STAFF_UNAVAILABLE":
            if len(s.model.unavail) >= max_unavail:
                return None
            cand = [r for r in plan.routes if r.staff_id not in s.model.unavail and r.status.value == "ACTIVE"]
            future = [r for r in cand if any(st.type.value == "VISIT" and st.start > t for st in r.stops)]
            pool = future or cand
            if not pool:
                return None
            r = rng.choice(pool)
            until = None if rng.random() < 0.45 else t + dt.timedelta(minutes=rng.randint(30, 240))
            return s.unavailable(t, r.staff_id, until, rng.choice(["VEHICLE_BREAKDOWN", "ILLNESS", "OTHER"]))
        return None

    def sample(t: dt.datetime) -> None:
        st = plan_stats(s.plan)
        timeline.append({"t": f"{t:%H:%M}", "v": st["version"], "visits": st["visits"], "unassigned": st["unassigned"],
                         "pgp": st["pgp_orders"], "cancelled": st["cancelled"],
                         "unavail": len(s.model.unavail)})

    t = t_start
    last_ts = now
    while t <= t_end:
        ts = max(t, last_ts)
        r = s.send(s.tick(ts), label="POST event [TIME_TICK]")
        counts["TIME_TICK"] = counts.get("TIME_TICK", 0) + 1
        last_ts = ts
        sample(ts)
        n = _poisson(rng, intensity * tick_min / 60.0)
        times = sorted(t + dt.timedelta(seconds=rng.randint(1, tick_min * 60 - 1)) for _ in range(n))
        for et in times:
            et = max(et, last_ts)
            kind = pick_type()
            body = build(kind, et)
            if body is None:
                skipped[kind] = skipped.get(kind, 0) + 1
                kind = "NEW_ORDER"
                body = build(kind, et)
            s.send(body, label=f"POST event [{body['type']}]")
            counts[body["type"]] = counts.get(body["type"], 0) + 1
            last_ts = et
        t += dt.timedelta(minutes=tick_min)
    final = s.plan
    sample(last_ts)
    st = plan_stats(final)
    total_orders = len(s.model.orders)
    res.metrics.update({
        "events_by_type": counts, "skipped_no_candidate": skipped, "replans": st["version"] - 1,
        "orders_total": total_orders, "final": st,
        "placed_share": round(st["visits"] / total_orders, 3) if total_orders else None,
        "unavailable_staff": {k: [v[0].strftime("%H:%M"), v[1].strftime("%H:%M") if v[1] else None]
                              for k, v in s.model.unavail.items()},
        "timeline_every": f"{tick_min} мин", "timeline": timeline[:: max(1, len(timeline) // 12)],
    })
    # вернувшиеся по until сотрудники получают работу после until?
    back = 0
    for sid, (frm, until) in s.model.unavail.items():
        if until is None:
            continue
        r = next((x for x in final.routes if x.staff_id == sid), None)
        if r and any(x.type.value == "VISIT" and x.start >= until for x in r.stops):
            back += 1
    res.metrics["returned_by_until_with_new_visits"] = back
    res.note("контракт не содержит события «сотрудник снова доступен»: возврат моделируется только заранее "
             "заданным `until` в STAFF_UNAVAILABLE (повторное событие по тому же сотруднику не симулируется)")
    return res
