"""Сценарий bulk: все наряды дня сразу (PUT day + POST plan с time_limit), GET plan, повторный POST."""

from __future__ import annotations

import json
import time

from simlib.data import expected_pgp
from simlib.report import ScenarioResult
from simlib.session import Ctx, DaySession, at, plan_stats, visits_of

NAME = "bulk"


def run(ctx: Ctx) -> ScenarioResult:
    res = ScenarioResult(NAME, ctx.seed)
    rng = ctx.rng(NAME)
    date = ctx.options.get("date") or ctx.data.train_dates()[2]
    now = at(date, "08:30")
    orders = ctx.data.day_orders(date, now, ctx.scale, rng)
    absent = sorted(rng.sample(ctx.data.staff_ids, 2))  # два сотрудника не на смене
    res.metrics.update({"date": date, "orders_in": len(orders), "absent": absent, "time_limit_sec": ctx.time_limit})
    s = DaySession(ctx, res, date, now, orders, absent)
    t0 = time.perf_counter()
    if not s.setup():
        return res
    plan, model = s.plan, s.model
    st = plan_stats(plan)
    res.metrics["plan_sec"] = round(time.perf_counter() - t0, 2)
    res.metrics["initial_plan"] = st
    # 1. учёт: назначено + unassigned + ПГП (+ cancelled) = вход, без дублей и посторонних
    visits = visits_of(plan)
    ids_in = set(model.orders)
    placed = set(visits) | {u.order_id for u in plan.unassigned} | {o for g in plan.pgp_groups for o in g.order_ids} \
        | {c.order_id for c in plan.cancelled}
    res.check(placed == ids_in, "не все наряды учтены ровно в одном месте (H9)",
              f"нет в плане: {sorted(ids_in - placed)[:5]}, лишних: {sorted(placed - ids_in)[:5]}", "violation", "accounting")
    n_items = st["visits"] + st["unassigned"] + st["pgp_orders"] + st["cancelled"]
    res.check(n_items == len(ids_in), "сумма assigned+unassigned+ПГП+cancelled != числу входных нарядов",
              f"{n_items} != {len(ids_in)}", "violation", "accounting")
    # 2. маршруты: по одному на каждого на смене, отсутствующих нет
    route_ids = [r.staff_id for r in plan.routes]
    on_shift = set(ctx.data.staff_ids) - set(absent)
    res.check(set(route_ids) == on_shift and len(route_ids) == len(set(route_ids)),
              "маршруты не совпадают с множеством сотрудников на смене",
              f"лишние {sorted(set(route_ids) - on_shift)[:3]}, нет {sorted(on_shift - set(route_ids))[:3]}",
              "contract", "routes")
    # 3. первоначальный день: по ТЗ без ПГП-групп в обучающих днях; сверка с независимым детектором
    exp = expected_pgp(list(model.orders.values()), ctx.data.wt)
    got = [set(g.order_ids) for g in plan.pgp_groups]
    res.check(sorted(map(sorted, exp)) == sorted(map(sorted, got)),
              "ПГП-группы первоначального плана не совпадают с независимым детектором (>=3 за 60 мин)",
              f"ожидалось {sorted(map(sorted, exp))[:3]}, получено {sorted(map(sorted, got))[:3]}", "contract", "pgp")
    # 4. GET == POST
    g = s.fetch_plan("get_plan")
    if g.ok:
        res.check(_norm(g.data) == _norm(s.plan_json), "GET /plan не совпадает с ответом POST /plan",
                  "после первоначального построения тела различаются", "contract", "get_eq_post")
    # 5. повторный POST: 200, версия 1, детерминизм «насколько заявлено» - только допустимость и состав
    r2 = ctx.client.post_plan(date, ctx.time_limit)
    if res.check(r2.ok, "повторный POST /plan до событий вернул не 200", f"{r2.status} {r2.text or r2.data}",
                 "contract", "repost"):
        plan2 = ctx.checker.check(res, model, r2.data, None, None, "repost", expected_version=1)
        if plan2 is not None:
            st2 = plan_stats(plan2)
            res.metrics["repeat_plan"] = st2
            res.metrics["repeat_visits_delta"] = st2["visits"] - st["visits"]
            res.check(r2.elapsed <= ctx.time_limit + 1.0, "повторный POST /plan дольше time_limit_sec",
                      f"{r2.elapsed:.1f} с", "latency", "repost")
            same = _signature(plan2) == _signature(plan)
            res.metrics["repeat_identical"] = same
            if not same:
                res.note("повторный POST /plan даёт другой набор визитов (контракт не требует детерминизма; "
                         f"разница визитов {st2['visits'] - st['visits']:+d})")
        g2 = s.fetch_plan("get_after_repost")
        if g2.ok:
            res.check(_norm(g2.data) == _norm(r2.data), "GET /plan после повторного POST не совпадает с ним",
                      "", "contract", "get_eq_repost")
    # 6. LATE_RISK/UNASSIGNED-алерты на идеальном плане: LATE_RISK быть не должно
    late = [a for a in plan.alerts if a.type.value == "LATE_RISK"]
    res.check(not late, "LATE_RISK в первоначальном плане (до событий)", f"{len(late)} шт.", "invariant", "alerts")
    res.metrics["latency_post_plan_sec"] = round(r2.elapsed if r2.ok else 0, 2)
    return res


def _norm(plan: dict) -> str:
    p = dict(plan)
    p.pop("computation_ms", None)
    return json.dumps(p, sort_keys=True, ensure_ascii=False)


def _signature(plan) -> frozenset:
    return frozenset((r.staff_id, s.order_id, s.start) for r in plan.routes for s in r.stops if s.type.value == "VISIT")
