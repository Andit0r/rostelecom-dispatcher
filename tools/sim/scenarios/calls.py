"""Сценарий calls: звонки диспетчеру - поток NEW_ORDER (одиночные, пачки по 5, параллельные POST)."""

from __future__ import annotations

import datetime as dt
import threading
from concurrent.futures import ThreadPoolExecutor

from simlib import schema
from simlib.data import ALL_CODES, OrderFactory, iso
from simlib.report import ScenarioResult
from simlib.session import Ctx, DaySession, at, plan_stats

NAME = "calls"


def run(ctx: Ctx) -> ScenarioResult:
    res = ScenarioResult(NAME, ctx.seed)
    rng = ctx.rng(NAME)
    data = ctx.data
    date = ctx.options.get("calls_date") or ctx.data.train_dates()[3]
    now = at(date, "08:30")
    empty = bool(ctx.options.get("calls_empty"))
    base = [] if empty else data.day_orders(date, now, ctx.scale, rng)
    n_single = ctx.options.get("calls_single") or (6 if ctx.quick else 30)
    n_batches = ctx.options.get("calls_batches") or (2 if ctx.quick else 6)
    n_par = ctx.options.get("calls_parallel") or (10 if ctx.quick else 20)
    res.metrics.update({"date": date, "base_orders": len(base), "singles": n_single, "batches": n_batches,
                        "batch_size": 5, "parallel": n_par})
    s = DaySession(ctx, res, date, now, base, [], time_limit=ctx.time_limit)
    if not s.setup():
        return res
    fac = OrderFactory(data, rng, date)
    # узлы с ПГП-типами заняты: новые REPAIR/DEGRADATION берём с чужих узлов, чтобы случайно не собрать ПГП
    busy_nodes = {o["access_node_id"] for o in base if data.wt[o["work_type_code"]]["pgp_relevant"]}

    def make(ts: dt.datetime, code: str | None = None) -> dict:
        code = code or rng.choice(ALL_CODES)
        avoid = busy_nodes if data.wt[code]["pgp_relevant"] else None
        o = fac.make(ts, code, avoid_nodes=avoid)
        if data.wt[code]["pgp_relevant"]:
            busy_nodes.add(o["access_node_id"])
        return o

    ts = at(date, "09:00")
    codes = list(ALL_CODES)
    rng.shuffle(codes)
    cycle = 0
    sent_bodies: list[dict] = []
    # 1) одиночные звонки с паузой 0-3 с модельного времени
    for i in range(n_single):
        code = codes[cycle % len(codes)] if i < len(ALL_CODES) * 2 else None
        cycle += 1
        ts += dt.timedelta(seconds=rng.randint(0, 3))
        ev = s.new_order(ts, make(ts, code))
        r = s.send(ev, label="POST event (single)")
        sent_bodies.append(ev)
        _count_latency(res, r)
    # 2) пачки по 5 за один момент
    for b in range(n_batches):
        ts += dt.timedelta(seconds=rng.randint(1, 3))
        for k in range(5):
            code = ALL_CODES[(b * 5 + k) % len(ALL_CODES)] if b < 2 else None
            ev = s.new_order(ts, make(ts, code))
            r = s.send(ev, label="POST event (batch)")
            sent_bodies.append(ev)
            _count_latency(res, r)
    # 3) идемпотентность последнего события
    last = sent_bodies[-1]
    before = s.fetch_plan("dup_before")
    r = s.client.post_event(date, last, "POST event (duplicate)")
    res.events += 1
    if res.check(r.ok, "повтор event_id: ожидался 200", f"{r.status} {r.text or r.data}", "contract", "duplicate"):
        d = r.data
        res.check(d.get("duplicate") is True and d.get("accepted") is True and d.get("plan_version") == s.model.version,
                  "повтор event_id: duplicate=true и тот же plan_version", f"{d}", "contract", "duplicate")
        after = s.fetch_plan("dup_after")
        if before.ok and after.ok:
            res.check(before.data == after.data, "повтор event_id изменил план", "", "contract", "duplicate")
    # 4) параллельные POST
    _parallel(ctx, res, s, rng, make, ts, n_par)
    final = s.plan
    if final is not None:
        res.metrics["final_plan"] = plan_stats(final)
        res.metrics["alerts_final"] = plan_stats(final)["alerts"]
    res.metrics["events_sent"] = res.events
    return res


def _count_latency(res: ScenarioResult, r) -> None:
    res.metrics["max_event_sec"] = round(max(res.metrics.get("max_event_sec", 0.0), r.elapsed), 3)


def _parallel(ctx: Ctx, res: ScenarioResult, s: DaySession, rng, make, ts: dt.datetime, n: int) -> None:
    date = s.date
    ts = ts + dt.timedelta(seconds=2)
    base_version = s.model.version
    base_plan = s.plan
    events = [s.new_order(ts, make(ts)) for _ in range(n)]
    dup = events[0]  # одно событие уходит дважды одновременно
    jobs = events + [dup]
    results: list = [None] * len(jobs)
    stop = threading.Event()
    reader_log: list[tuple[int, int, list[str]]] = []

    def post(i: int):
        results[i] = ctx.client.post_event(date, jobs[i], "POST event (parallel)")

    def reader(rid: int):
        last = 0
        while not stop.is_set():
            g = ctx.client.get_plan(date)
            if g.ok:
                v = g.data.get("plan_version", 0) if isinstance(g.data, dict) else 0
                bad = [] if isinstance(v, int) else ["plan_version не int"]
                reader_log.append((rid, v, bad + ([f"версия вернулась назад {last}->{v}"] if v < last else [])))
                last = max(last, v)
            else:
                reader_log.append((rid, 0, [f"GET plan {g.status}"]))

    readers = [threading.Thread(target=reader, args=(i,), daemon=True) for i in range(3)]
    for t in readers:
        t.start()
    with ThreadPoolExecutor(max_workers=min(20, len(jobs))) as pool:
        list(pool.map(post, range(len(jobs))))
    stop.set()
    for t in readers:
        t.join(timeout=15)
    res.events += len(jobs)
    ok_resp = [r for r in results if r is not None]
    bad = [r for r in ok_resp if r.status != 200]
    res.check(not bad, "параллельные POST /events: не все приняты", "; ".join(f"{r.status} {r.data}" for r in bad[:3]),
              "contract", "parallel")
    good = [r.data for r in ok_resp if r.status == 200]
    for d in good:
        errs = schema.validate(d, schema.EVENT_RESULT)
        res.check(not errs, "параллельный EventResult не по схеме", "; ".join(errs[:3]), "contract", "parallel")
    fresh = [d for d in good if not d.get("duplicate")]
    dups = [d for d in good if d.get("duplicate")]
    res.check(len(fresh) == n and len(dups) == 1,
              "одновременный повтор event_id: ожидалось ровно одно принятое и один duplicate",
              f"принято {len(fresh)}, duplicate {len(dups)}", "contract", "parallel_dup")
    versions = sorted(d["plan_version"] for d in fresh)
    res.check(versions == list(range(base_version + 1, base_version + 1 + n)),
              "plan_version при параллельных событиях не образуют последовательность +1",
              f"{versions[:8]}.. ожидалось {base_version + 1}..{base_version + n}", "invariant", "parallel_versions")
    if dups:
        res.check(dups[0]["plan_version"] >= base_version + 1, "duplicate вернул устаревший plan_version",
                  str(dups[0]), "contract", "parallel_dup")
    slow = [r.elapsed for r in ok_resp]
    if slow:
        res.metrics["parallel_max_sec"] = round(max(slow), 3)
        if max(slow) > 10:
            res.add("note", "параллельный POST /events дольше 10 с (очередь за блокировкой дня)",
                    f"макс {max(slow):.1f} с при {len(jobs)} одновременных запросах", "parallel")
    rd_bad = [x for x in reader_log if x[2]]
    res.check(not rd_bad, "параллельный GET /plan вернул несогласованный/невалидный план",
              str(rd_bad[:2]), "invariant", "parallel_readers")
    res.metrics["parallel_reads"] = len(reader_log)
    # события применяем к модели в порядке версий, затем проверяем итоговый план целиком
    by_id = {e["event_id"]: e for e in events}
    for d in sorted(fresh, key=lambda d: d["plan_version"]):
        s.model.apply(by_id[d["event_id"]])
    s.model.version = base_version + len(fresh)
    g = s.fetch_plan("parallel_final")
    if g.ok:
        plan = ctx.checker.check(res, s.model, g.data, base_plan, None, "parallel_final",
                                 expected_version=base_version + len(fresh))
        if plan is not None:
            s.plan, s.plan_json = plan, g.data
