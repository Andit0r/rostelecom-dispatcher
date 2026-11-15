"""Сценарий late: LATE_RISK - заблаговременное предупреждение (>=30 мин до слота) и отсутствие ложных."""

from __future__ import annotations

import datetime as dt

from simlib.data import parse_ts
from simlib.report import ScenarioResult
from simlib.session import Ctx, DaySession, at, plan_stats, visits_of

NAME = "late"
MIN = dt.timedelta(minutes=1)


def _sig(plan) -> dict:
    return {oid: (sid, s.start) for oid, (sid, s) in visits_of(plan).items()}


def run(ctx: Ctx) -> ScenarioResult:
    res = ScenarioResult(NAME, ctx.seed)
    rng = ctx.rng(NAME)
    data = ctx.data
    date = ctx.options.get("late_date") or ctx.data.train_dates()[6]
    now = at(date, "08:30")
    base = data.day_orders(date, now, ctx.scale, rng)
    res.metrics.update({"date": date, "base_orders": len(base)})

    # ---- A. идеальный день: только TIME_TICK, ложных LATE_RISK быть не должно
    s = DaySession(ctx, res, date, now, base, [], time_limit=ctx.time_limit)
    if not s.setup():
        return res
    init = _sig(s.plan)
    step_min, last = (60, "11:00") if ctx.quick else (30, "20:00")
    t = at(date, "09:00")
    ticks = false_pos = changed = 0
    while t <= at(date, last):
        r = s.send(s.tick(t), label="POST event [TIME_TICK]")
        ticks += 1
        if r.ok and s.last_result:
            lr = [a for a in s.last_result.get("new_alerts", []) if a["type"] == "LATE_RISK"]
            false_pos += len(lr)
            if lr:
                res.add("invariant", "ложный LATE_RISK в идеальном дне (только TIME_TICK)",
                        f"{[(a['order_id'], a.get('lateness_min')) for a in lr][:3]}", f"tick {t:%H:%M}")
        cur = _sig(s.plan)
        changed += sum(1 for k, v in cur.items() if init.get(k) != v)
        t += step_min * MIN
    res.check(not [a for a in s.plan.alerts if a.type.value == "LATE_RISK"],
              "LATE_RISK в списке alerts идеального дня", "", "invariant", "ideal_final")
    res.check(changed == 0, "TIME_TICK без событий изменил исполнителя/время визитов", f"{changed} визитов",
              "note", "ideal_ticks")
    res.metrics["ideal_day"] = {"ticks": ticks, "false_late_risk": false_pos, "visits_changed_by_ticks": changed}

    # ---- B. задержки, после которых слот следующего визита под угрозой
    s = DaySession(ctx, res, date, now, base, [], time_limit=ctx.time_limit)
    if not s.setup():
        return res
    attempts = ctx.options.get("late_attempts") or (2 if ctx.quick else 10)
    delayed: set[str] = set()
    stats = {"attempts": 0, "no_candidate": 0, "moved_orders": 0, "moved_with_alert": 0, "moved_timely": 0,
             "target_alert": 0, "target_timely": 0, "actions": {}, "applied": 0}
    for k in range(attempts):
        cand = _candidates(s, delayed)
        if not cand:
            stats["no_candidate"] += 1
            break
        ts, sid, v1_oid, v1_end, shift, v2_oid, v2_slot = cand[0]
        delayed.add(v1_oid)
        prev = s.plan
        body = s.delayed(ts, sid, v1_oid, v1_end + shift)
        r = s.send(body, label="POST event [VISIT_DELAYED]")
        stats["attempts"] += 1
        if not r.ok:
            continue
        plan = s.plan
        before, after = visits_of(prev), visits_of(plan)
        cancelled = {c.order_id for c in plan.cancelled}
        moved = [o for o in before if o not in cancelled and (o not in after or after[o][0] != before[o][0])]
        alerts = [a for a in plan.alerts if a.type.value == "LATE_RISK"]
        by_order: dict[str, list] = {}
        for a in alerts:
            by_order.setdefault(a.order_id, []).append(a)
        for o in moved:
            stats["moved_orders"] += 1
            al = by_order.get(o, [])
            slot0 = parse_ts(s.model.orders[o]["slot"]["start"])
            if al:
                stats["moved_with_alert"] += 1
            if any(a.created_at <= slot0 - 30 * MIN for a in al):
                stats["moved_timely"] += 1
            elif slot0 - ts >= 30 * MIN:
                res.add("contract", "переназначен/снят наряд без своевременного LATE_RISK (>=30 мин до слота)",
                        f"{o}: слот {slot0:%H:%M}, событие {ts:%H:%M}, алертов {len(al)}", f"attempt {k + 1}")
        tgt = [a for a in by_order.get(v2_oid, []) if a.created_at == ts]
        if not tgt and v2_oid not in moved:
            res.add("note", "задержка уводила следующий визит за latest_start, LATE_RISK не создан "
                    "(наряд остался у того же сотрудника: порядок в маршруте пересобран)",
                    f"{v2_oid} (задержка {shift // MIN} мин у {sid})", f"attempt {k + 1}")
        if tgt:
            stats["target_alert"] += 1
            a = tgt[-1]
            stats["actions"][a.suggested_action.value] = stats["actions"].get(a.suggested_action.value, 0) + 1
            if a.applied:
                stats["applied"] += 1
            if a.created_at <= v2_slot - 30 * MIN:
                stats["target_timely"] += 1
            res.check(a.suggested_action.value in ("REASSIGN", "RESCHEDULE", "NONE"),
                      "LATE_RISK: рекомендация вне REASSIGN/RESCHEDULE/NONE", a.suggested_action.value, "contract", f"attempt {k + 1}")
            if a.suggested_action.value == "NONE":
                res.add("note", "LATE_RISK с suggested_action=NONE (контракт ожидает REASSIGN или RESCHEDULE)",
                        f"{a.order_id}: {a.message}", f"attempt {k + 1}")
            if a.suggested_action.value == "REASSIGN":
                res.check(bool(a.suggested_staff_id), "LATE_RISK/REASSIGN без suggested_staff_id", a.alert_id,
                          "contract", f"attempt {k + 1}")
                if a.applied and a.suggested_staff_id:
                    now_staff = after.get(a.order_id, (None,))[0]
                    res.check(now_staff == a.suggested_staff_id, "REASSIGN applied=true, но наряд не у suggested_staff_id",
                              f"{a.order_id}: {now_staff} vs {a.suggested_staff_id}", "invariant", f"attempt {k + 1}")
            if a.suggested_action.value == "RESCHEDULE":
                res.check(a.proposed_slot is not None, "LATE_RISK/RESCHEDULE без proposed_slot", a.alert_id, "contract",
                          f"attempt {k + 1}")
            res.check(a.lateness_min is not None and a.lateness_min > 0 and a.forecast_start is not None
                      or a.suggested_action.value != "NONE", "LATE_RISK без прогноза опоздания (lateness_min/forecast_start)",
                      f"{a.alert_id}: lateness={a.lateness_min} forecast={a.forecast_start}", "note", f"attempt {k + 1}")
    stats["moved_timely_share"] = round(stats["moved_timely"] / stats["moved_orders"], 3) if stats["moved_orders"] else None
    res.metrics["delays"] = stats
    res.metrics["final_plan"] = plan_stats(s.plan)
    if stats["attempts"] == 0:
        res.note("не нашлось подходящих пар визитов для задержки (малый масштаб?): часть B не выполнена")
    return res


def _candidates(s: DaySession, delayed: set[str]) -> list[tuple]:
    """Пары (v1, v2) подряд у сотрудника: задержка v1 на shift минут уводит v2 за latest_start.
    Берём пары с запасом >=45 мин от события до слота v2 и задержкой <=90 мин; сортировка - по времени события."""
    out = []
    model, plan = s.model, s.plan
    t0 = model.model_time
    ck = s.ctx.checker
    idx, dur = ck.matrix.index, ck.matrix.matrix.durations_sec
    for r in plan.routes:
        if r.status.value != "ACTIVE" or r.staff_id in model.unavail:
            continue
        for a, b in zip(r.stops, r.stops[1:]):  # v2 строго следует за v1 (без перерыва/пополнения между ними)
            if a.type.value != "VISIT" or b.type.value != "VISIT":
                continue
            v1, v2 = a, b
            if v1.order_id in delayed or v1.order_id in model.overrides or v2.order_id not in model.orders:
                continue
            ts = v1.start + 5 * MIN
            if v1.start < t0 or ts >= v1.end:
                continue
            o2 = model.orders[v2.order_id]
            slot0, latest = parse_ts(o2["slot"]["start"]), parse_ts(o2["latest_start"])
            if slot0 - ts < 45 * MIN:
                continue
            tr = dt.timedelta(seconds=dur[idx[v1.location_id]][idx[v2.location_id]])
            shift = (latest - (v1.end + tr)) + 10 * MIN  # после такой задержки v2 не успевает к latest_start
            if shift < 10 * MIN or shift > 90 * MIN:
                continue
            out.append((ts, r.staff_id, v1.order_id, v1.end, shift, v2.order_id, slot0))
    out.sort(key=lambda c: (c[0], c[4]))
    return out
