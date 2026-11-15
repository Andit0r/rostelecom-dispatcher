"""Сценарий pgp: групповые инциденты на узле доступа (>=3 pgp_relevant наряда за 60 мин)."""

from __future__ import annotations

import datetime as dt

from simlib.data import OrderFactory, iso
from simlib.report import ScenarioResult
from simlib.session import Ctx, DaySession, at, plan_stats, visits_of

NAME = "pgp"
MIN = dt.timedelta(minutes=1)


def where(plan, oid: str) -> str:
    if oid in visits_of(plan):
        return "route"
    if any(u.order_id == oid for u in plan.unassigned):
        return "unassigned"
    if any(oid in g.order_ids for g in plan.pgp_groups):
        return "pgp"
    if any(c.order_id == oid for c in plan.cancelled):
        return "cancelled"
    return "nowhere"


def group_with(plan, oid: str):
    return next((g for g in plan.pgp_groups if oid in g.order_ids), None)


def run(ctx: Ctx) -> ScenarioResult:
    res = ScenarioResult(NAME, ctx.seed)
    rng = ctx.rng(NAME)
    data = ctx.data
    date = ctx.options.get("pgp_date") or ctx.data.train_dates()[5]
    now = at(date, "08:30")
    tl = min(ctx.time_limit, 5 if ctx.quick else 15)
    base_n = 12 if ctx.quick else 40
    pool = [o for o in data.day_orders(date, now, 1.0) if not data.wt[o["work_type_code"]]["pgp_relevant"]]
    nodes = sorted(data.nodes)
    rng.shuffle(nodes)
    node_iter = iter(nodes)
    cases = {}

    def reg(fn):
        cases[fn.__name__] = fn
        return fn

    def new_session(extra_fn=None) -> tuple[DaySession, OrderFactory]:
        fac = OrderFactory(data, rng, date, start_no=9000)
        extra = extra_fn(fac) if extra_fn else []
        base = sorted(rng.sample(pool, min(base_n, len(pool))), key=lambda o: o["order_id"]) + extra
        s = DaySession(ctx, res, date, now, base, [], time_limit=tl)
        return s, fac

    def mk(fac, ts, node, code="REPAIR", slot_hour=13):
        return fac.make(ts, code, node_id=node, slot_hour=slot_hour)

    def push(s, fac, ts, node, code="REPAIR", slot_hour=13):
        o = mk(fac, ts, node, code, slot_hour)
        s.send(s.new_order(ts, o), label="POST event [NEW_ORDER]")
        return o

    def alerts_of(plan, typ):
        return [a for a in plan.alerts if a.type.value == typ]

    @reg
    def two_no_pgp():
        s, fac = new_session()
        if not s.setup():
            return
        n = next(node_iter)
        a = push(s, fac, at(date, "09:00"), n)
        b = push(s, fac, at(date, "09:20"), n, "DEGRADATION")
        p = s.plan
        res.check(not p.pgp_groups and not alerts_of(p, "PGP_DETECTED"), "[2 наряда] ПГП не должно быть",
                  f"групп {len(p.pgp_groups)}", "contract", "two_no_pgp")
        res.check(where(p, a["order_id"]) != "pgp" and where(p, b["order_id"]) != "pgp",
                  "[2 наряда] наряды не должны уходить в ПГП", "", "contract", "two_no_pgp")

    @reg
    def three_in_60():
        s, fac = new_session()
        if not s.setup():
            return
        n = next(node_iter)
        t3 = at(date, "09:50")
        o1 = push(s, fac, at(date, "09:00"), n)
        o2 = push(s, fac, at(date, "09:25"), n)
        res.check(not s.plan.pgp_groups, "[3 за 50 мин] группа до третьего наряда", "", "contract", "three_in_60")
        o3 = push(s, fac, t3, n, "DEGRADATION")
        p = s.plan
        ids = {o["order_id"] for o in (o1, o2, o3)}
        g = group_with(p, o3["order_id"])
        if not res.check(g is not None, "[3 за 50 мин] ПГП-группа не создана", "", "contract", "three_in_60"):
            return
        res.check(set(g.order_ids) == ids, "[3 за 50 мин] состав группы не равен трём нарядам",
                  f"{sorted(g.order_ids)} vs {sorted(ids)}", "contract", "three_in_60")
        res.check(g.access_node_id == n and g.action.value == "TRANSFER_TO_STATION",
                  "[3 за 50 мин] access_node_id/action группы", f"{g.access_node_id} {g.action}", "contract", "three_in_60")
        res.check(g.detected_at == t3, "[3 за 50 мин] detected_at не равен моменту третьего наряда",
                  f"{g.detected_at} vs {t3}", "contract", "three_in_60")
        pa = alerts_of(p, "PGP_DETECTED")
        res.check(len(pa) == 1 and pa[0].group_id == g.group_id and pa[0].suggested_action.value == "TRANSFER_TO_STATION"
                  and pa[0].status.value == "ACTIVE", "[3 за 50 мин] ровно один PGP_DETECTED с group_id и TRANSFER_TO_STATION",
                  f"{[(a.group_id, a.suggested_action.value, a.status.value) for a in pa]}", "contract", "three_in_60")
        new_types = [a["type"] for a in (s.last_result or {}).get("new_alerts", [])]
        res.check("PGP_DETECTED" in new_types, "[3 за 50 мин] PGP_DETECTED нет в EventResult.new_alerts",
                  str(new_types), "contract", "three_in_60")
        res.check(all(where(p, i) == "pgp" for i in ids), "[3 за 50 мин] наряды ПГП остались в маршрутах/unassigned",
                  str({i: where(p, i) for i in ids}), "contract", "three_in_60")

    @reg
    def boundary_60():
        s, fac = new_session()
        if not s.setup():
            return
        n = next(node_iter)
        ids = [push(s, fac, at(date, hm), n)["order_id"] for hm in ("09:00", "09:30", "10:00")]
        g = group_with(s.plan, ids[0])
        res.check(g is not None and set(g.order_ids) == set(ids), "[ровно 60 мин] 3 наряда в окне 60 мин - это ПГП",
                  f"{g and sorted(g.order_ids)}", "contract", "boundary_60")

    @reg
    def stretched():
        s, fac = new_session()
        if not s.setup():
            return
        n1, n2 = next(node_iter), next(node_iter)
        for hm in ("09:00", "09:31", "10:02"):  # размах 62 мин
            push(s, fac, at(date, hm), n1)
        for hm in ("10:30", "11:35", "12:40"):  # шаг 65 мин
            push(s, fac, at(date, hm), n2, "DEGRADATION")
        res.check(not s.plan.pgp_groups and not alerts_of(s.plan, "PGP_DETECTED"),
                  "[растянутые >60 мин] ПГП не должно быть", f"групп {len(s.plan.pgp_groups)}", "contract", "stretched")

    @reg
    def four_five():
        s, fac = new_session()
        if not s.setup():
            return
        n = next(node_iter)
        ids = []
        gid = det = None
        for i, hm in enumerate(("09:00", "09:20", "09:40", "10:00", "10:30")):
            ids.append(push(s, fac, at(date, hm), n, "REPAIR" if i % 2 == 0 else "DEGRADATION")["order_id"])
            g = group_with(s.plan, ids[0])
            if i == 2:
                if not res.check(g is not None, "[4-5 нарядов] группа не создана на третьем", "", "contract", "four_five"):
                    return
                gid, det = g.group_id, g.detected_at
            elif i > 2 and g is not None:
                res.check(g.group_id == gid and g.detected_at == det,
                          "[4-5 нарядов] group_id/detected_at изменились при пополнении группы",
                          f"{g.group_id} {g.detected_at} vs {gid} {det}", "contract", "four_five")
        g = group_with(s.plan, ids[0])
        res.check(g is not None and set(g.order_ids) == set(ids), "[4-5 нарядов] в группе не все 5 нарядов",
                  f"{g and len(g.order_ids)} из 5", "contract", "four_five")
        res.check(len(s.plan.pgp_groups) == 1 and len(alerts_of(s.plan, "PGP_DETECTED")) == 1,
                  "[4-5 нарядов] должна быть одна группа и один PGP_DETECTED",
                  f"групп {len(s.plan.pgp_groups)}, алертов {len(alerts_of(s.plan, 'PGP_DETECTED'))}", "contract", "four_five")

    @reg
    def two_nodes():
        s, fac = new_session()
        if not s.setup():
            return
        g_node, h_node = next(node_iter), next(node_iter)
        plan_ids: dict[str, list[str]] = {g_node: [], h_node: []}
        for hm, node in (("09:00", g_node), ("09:05", h_node), ("09:10", g_node), ("09:15", h_node),
                         ("09:20", g_node), ("09:25", h_node)):
            plan_ids[node].append(push(s, fac, at(date, hm), node)["order_id"])
        p = s.plan
        groups = {g.access_node_id: g for g in p.pgp_groups}
        res.check(len(p.pgp_groups) == 2 and set(groups) == {g_node, h_node}
                  and all(set(groups[n].order_ids) == set(v) for n, v in plan_ids.items()),
                  "[два узла] ожидались две отдельные группы по узлам", f"{[(g.access_node_id, len(g.order_ids)) for g in p.pgp_groups]}",
                  "contract", "two_nodes")
        res.check(len({g.group_id for g in p.pgp_groups}) == len(p.pgp_groups), "[два узла] group_id повторяются",
                  "", "contract", "two_nodes")

    @reg
    def initial_group():
        n = next(node_iter)
        extra = []
        s, fac = new_session(lambda f: extra.extend(f.make(at(date, hm), "REPAIR", node_id=n, slot_hour=13)
                                                    for hm in ("08:00", "08:10", "08:20")) or extra)
        if not s.setup():
            return
        ids = {o["order_id"] for o in extra}
        g = group_with(s.plan, extra[0]["order_id"])
        res.check(g is not None and set(g.order_ids) == ids, "[ПГП в первоначальном плане] 3 наряда узла за 20 мин не сгруппированы",
                  f"{g and sorted(g.order_ids)}", "contract", "initial_group")
        if g is not None:
            res.check(at(date, "08:20") <= g.detected_at <= now, "[ПГП в первоначальном плане] detected_at вне [08:20, now]",
                      str(g.detected_at), "note", "initial_group")
            res.check(bool(alerts_of(s.plan, "PGP_DETECTED")), "[ПГП в первоначальном плане] нет PGP_DETECTED в alerts",
                      "", "note", "initial_group")

    @reg
    def last_on_assigned():
        n = next(node_iter)
        extra = []
        s, fac = new_session(lambda f: extra.extend(f.make(at(date, hm), "REPAIR", node_id=n, slot_hour=13)
                                                    for hm in ("08:00", "08:10")) or extra)
        if not s.setup():
            return
        ids = [o["order_id"] for o in extra]
        before = {i: where(s.plan, i) for i in ids}
        res.check(not s.plan.pgp_groups, "[ПГП на последний наряд] группа в первоначальном плане при 2 нарядах", "",
                  "contract", "last_on_assigned")
        res.metrics["last_on_assigned_before"] = before
        o3 = push(s, fac, at(date, "08:40"), n, "DEGRADATION")
        ids.append(o3["order_id"])
        g = group_with(s.plan, o3["order_id"])
        res.check(g is not None and set(g.order_ids) == set(ids), "[ПГП на последний наряд] ранее распределённые не ушли в группу",
                  f"{g and sorted(g.order_ids)}", "contract", "last_on_assigned")
        res.check(all(where(s.plan, i) == "pgp" for i in ids), "[ПГП на последний наряд] наряды группы остались в маршрутах",
                  str({i: where(s.plan, i) for i in ids}), "contract", "last_on_assigned")

    @reg
    def non_pgp_same_node():
        s, fac = new_session()
        if not s.setup():
            return
        n = next(node_iter)
        o1 = push(s, fac, at(date, "09:00"), n)
        add = push(s, fac, at(date, "09:05"), n, "ADD_SERVICE", 15)
        o2 = push(s, fac, at(date, "09:10"), n)
        o3 = push(s, fac, at(date, "09:20"), n, "DEGRADATION")
        survey = push(s, fac, at(date, "09:25"), n, "DISMANTLE", 15)
        g = group_with(s.plan, o1["order_id"])
        res.check(g is not None and set(g.order_ids) == {o["order_id"] for o in (o1, o2, o3)},
                  "[не-ПГП того же узла] состав группы", f"{g and sorted(g.order_ids)}", "contract", "non_pgp_same_node")
        for o in (add, survey):
            res.check(where(s.plan, o["order_id"]) in ("route", "unassigned"),
                      "[не-ПГП того же узла] наряд другого типа затронут ПГП", f"{o['work_type_code']}: {where(s.plan, o['order_id'])}",
                      "contract", "non_pgp_same_node")

    @reg
    def in_flow():
        s, fac = new_session()
        if not s.setup():
            return
        n = next(node_iter)
        pg_times = {"09:10", "09:25", "09:40"}
        pg_ids = []
        ts = at(date, "09:00")
        i = 0
        while ts <= at(date, "10:00"):
            hm = f"{ts:%H:%M}"
            if hm in pg_times:
                pg_ids.append(push(s, fac, ts, n, "REPAIR")["order_id"])
            else:
                code = ["ADD_SERVICE", "INST_FTTB_MKD", "SURVEY_PRIVATE", "DISMANTLE"][i % 4]
                o = fac.make(ts, code, slot_hour=15)
                s.send(s.new_order(ts, o), label="POST event [NEW_ORDER]")
            i += 1
            ts += 5 * MIN
        g = group_with(s.plan, pg_ids[0])
        res.check(g is not None and set(g.order_ids) == set(pg_ids), "[ПГП в потоке звонков] состав группы",
                  f"{g and sorted(g.order_ids)}", "contract", "in_flow")

    @reg
    def started_visit_excluded():
        n = next(node_iter)
        holder = []
        s, fac = new_session(lambda f: holder.append(f.make(at(date, "08:30"), "REPAIR", node_id=n, slot_hour=9)) or holder)
        x = holder[0]
        if not s.setup():
            return
        v = visits_of(s.plan).get(x["order_id"])
        if v is None:
            res.note("started_visit_excluded: стартовый наряд не попал в маршрут, подслучай пропущен")
            return
        t = v[1].start + MIN
        if t > at(date, "09:26"):
            res.note(f"started_visit_excluded: визит стартует в {v[1].start:%H:%M}, поздно для окна 60 мин от 08:30, подслучай пропущен")
            return
        a = push(s, fac, t, n, "REPAIR")
        b = push(s, fac, t + 3 * MIN, n, "DEGRADATION")
        p = s.plan
        g = group_with(p, a["order_id"])
        res.check(where(p, x["order_id"]) == "route", "[начатый визит ПГП-типа] начатый наряд ушёл из маршрута",
                  where(p, x["order_id"]), "contract", "started_visit")
        if g is None:
            res.note("started_visit_excluded: группа из двух новых нарядов при одном начатом не создана (допустимо, если 3-й начат)")
        else:
            res.check(x["order_id"] not in g.order_ids, "[начатый визит ПГП-типа] начатый наряд включён в группу", "",
                      "contract", "started_visit")

    selected = ctx.options.get("pgp_cases")
    ran = []
    for name, fn in cases.items():
        if selected and name not in selected:
            continue
        ran.append(name)
        fn()
    res.metrics["cases"] = ran
    return res
