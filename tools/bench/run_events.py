#!/usr/bin/env python3
"""Бенч перепланирования по событиям: имитация оркестратора организаторов через HTTP (TestClient).

    python tools/bench/run_events.py [--scenario data/synth/events_example.json] [--budget 30]

Сценарий дня (data/api_spec.yaml, п. 1 и EventScenarioFile):
PUT /reference → PUT /reference/travel-matrix → PUT /days/{date} (наряды со слотом в этот день
и registered_at ≤ now) → POST /plan → для каждого события по возрастанию timestamp: сначала
сработавшие к этому моменту триггеры (timestamp = VISIT.start + fire_after_start_min,
staff_id — исполнитель из текущего плана; триггер пропускается, если наряда нет в маршруте),
затем само событие; после каждого — GET /plan. Оставшиеся триггеры отправляются в конце.

Метрики: задержка обработки события (макс/медиана/p95), нарушения validator.py H1–H10 по всем
версиям, LATE_RISK и доля своевременных (created_at ≤ slot.start − 30 мин) среди нарядов,
которые после событий сменили исполнителя или были сняты из маршрута, ПГП-группы против
эталона (fact.pgp_incident_id в orders_train.json).
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
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tools" / "bench"))

from run_day import _csv_value  # noqa: E402

DATA = ROOT / "data" / "synth"
TZ = dt.timezone(dt.timedelta(hours=5))


def reference_payload() -> dict:
    with open(DATA / "work_types.csv", encoding="utf-8", newline="") as fh:
        work_types = [{k: _csv_value(k, v) for k, v in row.items()} for row in csv.DictReader(fh)]
    return {
        "service_centers": json.loads((DATA / "service_centers.json").read_text(encoding="utf-8")),
        "staff": json.loads((DATA / "staff.json").read_text(encoding="utf-8")),
        "access_nodes": json.loads((DATA / "access_nodes.json").read_text(encoding="utf-8")),
        "work_types": work_types,
    }


def parse_ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def visits_of(plan) -> dict[str, tuple[str, object]]:
    return {s.order_id: (r.staff_id, s) for r in plan.routes for s in r.stops if s.type == "VISIT"}


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default=str(DATA / "events_example.json"))
    ap.add_argument("--budget", type=float, default=30.0, help="PLANNER_DAY_BUDGET_SEC для POST /plan")
    ap.add_argument("--time-limit", type=int, default=300)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--dump", help="сохранить итоговый план (JSON) в этот файл")
    ap.add_argument("--save-initial", help="сохранить первоначальный план (для воспроизведения прогона)")
    ap.add_argument("--load-initial", help="подменить первоначальный план сохранённым (отладка)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    os.environ["PLANNER_DAY_BUDGET_SEC"] = str(args.budget)

    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.models import Plan
    from app.planner import make_planner
    from app.planner.validator import validate_plan, validate_transition
    from app.store import Store

    scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    date = scenario["date"]
    now = parse_ts(scenario["now"])
    all_orders = json.loads((DATA / "orders_train.json").read_text(encoding="utf-8"))
    day_orders = [o for o in all_orders if o["slot"]["start"][:10] == date]
    loaded = [o for o in day_orders if parse_ts(o["registered_at"]) <= now]
    incidents: dict[str, set[str]] = {}
    for o in day_orders:
        inc = (o.get("fact") or {}).get("pgp_incident_id")
        if inc:
            incidents.setdefault(inc, set()).add(o["order_id"])
    order_info = {o["order_id"]: o for o in day_orders}
    for e in scenario["events"]:
        if e["type"] == "NEW_ORDER":
            order_info[e["order"]["order_id"]] = e["order"]

    store = Store()
    app = create_app(planner=make_planner(), store=store)
    base = "/api/v1"
    out: dict = {"scenario": Path(args.scenario).name, "date": date}
    with TestClient(app) as client:
        t0 = time.perf_counter()
        r = client.put(f"{base}/reference", json=reference_payload())
        assert r.status_code == 200, r.text
        r = client.put(f"{base}/reference/travel-matrix", content=(DATA / "travel_matrix.json").read_bytes(),
                       headers={"content-type": "application/json"})
        assert r.status_code == 200, r.text
        r = client.put(f"{base}/days/{date}", json={"date": date, "now": scenario["now"], "orders": loaded})
        assert r.status_code == 200, r.text
        out["load_sec"] = round(time.perf_counter() - t0, 2)
        t1 = time.perf_counter()
        r = client.post(f"{base}/days/{date}/plan", json={"time_limit_sec": args.time_limit})
        assert r.status_code == 200, r.text
        out["plan_sec"] = round(time.perf_counter() - t1, 2)
        plan = Plan.model_validate(r.json())
        day_key = dt.date.fromisoformat(date)
        if args.save_initial:
            Path(args.save_initial).write_text(plan.model_dump_json(by_alias=True), encoding="utf-8")
        if args.load_initial:
            plan = Plan.model_validate_json(Path(args.load_initial).read_text(encoding="utf-8"))
            store.days[day_key].plan = plan

        versions = [plan]
        viol = Counter()
        viol_examples: list[str] = []
        first_seen: dict[tuple, str] = {}  # (код, сотрудник, наряд) -> «forced» | «other»

        def check(prev_plan, curr_plan, moment, event, message: str = "") -> None:
            day = store.days[day_key]
            vs = validate_plan(curr_plan, store.reference, store.matrix, day)
            if prev_plan is not None:
                vs += validate_transition(prev_plan, curr_plan, store.reference, store.matrix, day, moment, event)
            for v in vs:
                viol[v.code] += 1
                key = (v.code, v.staff_id, v.order_id)
                if key not in first_seen:
                    # нарушение вынуждено событием: планировщик сообщил, что хвост недопустим даже пустым
                    forced = v.code in ("H2", "H3") and f"неустранимо: {v.staff_id}" in (message or "")
                    first_seen[key] = "forced" if forced else "other"
                    viol_examples.append(f"v{curr_plan.plan_version} {v}"
                                         + (" [вынуждено событием]" if forced else ""))

        check(None, plan, now, None)

        latencies: list[float] = []
        proc_ms: list[int] = []
        by_type: dict[str, list[float]] = {}
        rejected: list[str] = []
        moved: dict[str, dict] = {}  # order_id -> первое снятие/переназначение
        tick_changes: list[str] = []
        from pydantic import TypeAdapter

        from app.models import Event
        event_adapter = TypeAdapter(Event)

        def send(body: dict) -> None:
            nonlocal plan
            started = time.perf_counter()
            resp = client.post(f"{base}/days/{date}/events", json=body)
            elapsed = (time.perf_counter() - started) * 1000
            if resp.status_code != 200:
                rejected.append(f"{body['event_id']} {body['type']}: {resp.status_code} {resp.text[:200]}")
                return
            res = resp.json()
            latencies.append(elapsed)
            proc_ms.append(res.get("processing_ms") or 0)
            by_type.setdefault(body["type"], []).append(elapsed)
            if body["type"] == "TIME_TICK" and res.get("changed_staff_ids"):
                tick_changes.append(body["event_id"])
            new = Plan.model_validate(client.get(f"{base}/days/{date}/plan").json())
            ev = event_adapter.validate_python(body)
            check(plan, new, ev.timestamp, ev, res.get("message") or "")
            before, after = visits_of(plan), visits_of(new)
            cancelled = {c.order_id for c in new.cancelled}
            for oid, (sid, _) in before.items():
                if oid in cancelled:
                    continue
                if oid not in after or after[oid][0] != sid:
                    moved.setdefault(oid, {"at": ev.timestamp, "event": body["type"],
                                           "to": after[oid][0] if oid in after else "removed"})
            if args.verbose:
                print(f"  {body['timestamp'][11:19]} {body['type']:<18} {elapsed:7.0f} мс  {res.get('message') or ''}")
            versions.append(new)
            plan = new

        events = sorted(scenario["events"], key=lambda e: parse_ts(e["timestamp"]))
        pending = list(scenario["triggers"])
        fired = skipped = 0
        last_ts = now

        def fire_due(limit: dt.datetime | None) -> None:
            nonlocal fired, last_ts
            while pending:
                cands = []
                vis = visits_of(plan)
                for tr in pending:
                    if tr["order_id"] not in vis:
                        continue
                    sid, stop = vis[tr["order_id"]]
                    at = stop.start + dt.timedelta(minutes=tr["fire_after_start_min"])
                    cands.append((at, tr, sid, stop))
                if not cands:
                    return
                at, tr, sid, stop = min(cands, key=lambda c: c[0])
                if limit is not None and at > limit:
                    return
                at = max(at, last_ts)
                body = {"event_id": tr["trigger_id"], "type": tr["type"], "timestamp": at.isoformat(),
                        "staff_id": sid, "order_id": tr["order_id"]}
                if tr["type"] == "VISIT_DELAYED":
                    wt = store.reference.work_types[order_info[tr["order_id"]]["work_type_code"]]
                    end = stop.start + dt.timedelta(minutes=wt.duration_min + (tr.get("extra_duration_min") or 0))
                    body["expected_end"] = end.isoformat()
                else:
                    body["actual_end"] = at.isoformat()
                    body["transferred_to"] = tr.get("transferred_to") or "STATION_EQUIPMENT"
                pending.remove(tr)
                fired += 1
                last_ts = at
                send(body)

        for e in events:
            ts = parse_ts(e["timestamp"])
            fire_due(ts)
            last_ts = max(last_ts, ts)
            send(e)
        fire_due(None)
        skipped = len(pending)

        final = plan
        if args.dump:
            Path(args.dump).write_text(final.model_dump_json(indent=1, by_alias=True), encoding="utf-8")
        # ---- LATE_RISK
        late = [a for a in final.alerts if a.type == "LATE_RISK"]
        late_by_order: dict[str, list] = {}
        for a in late:
            late_by_order.setdefault(a.order_id, []).append(a)
        timely = with_alert = impossible = 0
        for oid, info in moved.items():
            slot_start = parse_ts(order_info[oid]["slot"]["start"])
            alerts = late_by_order.get(oid, [])
            if alerts:
                with_alert += 1
            if any(a.created_at <= slot_start - dt.timedelta(minutes=30) for a in alerts):
                timely += 1
            if info["at"] > slot_start - dt.timedelta(minutes=30):
                impossible += 1
        ids = [a.alert_id for a in final.alerts]
        # список только пополняется: каждая версия — префикс следующей по alert_id
        grows = all(
            [a.alert_id for a in v.alerts] == [a.alert_id for a in w.alerts][: len(v.alerts)]
            for v, w in zip(versions, versions[1:])
        )

        # ---- ПГП
        group_orders = {oid: g.group_id for g in final.pgp_groups for oid in g.order_ids}
        inc_orders = {oid: inc for inc, members in incidents.items() for oid in members}
        tp = sum(1 for oid in group_orders if oid in inc_orders)
        cancelled_final = {c.order_id for c in final.cancelled}
        inc_live = [oid for oid in inc_orders if oid not in cancelled_final]
        pgp = {
            "groups": len(final.pgp_groups),
            "incidents": len(incidents),
            "group_orders": len(group_orders),
            "incident_orders": len(inc_orders),
            "precision": round(tp / len(group_orders), 3) if group_orders else None,
            "recall": round(tp / len(inc_orders), 3) if inc_orders else None,
            "recall_excl_cancelled": round(tp / len(inc_live), 3) if inc_live else None,
            "detail": [],
        }
        for inc, members in sorted(incidents.items()):
            regs = sorted(parse_ts(order_info[o]["registered_at"]) for o in members)
            third = regs[2] if len(regs) >= 3 else None
            gids = {group_orders.get(o) for o in members} - {None}
            g = next((x for x in final.pgp_groups if x.group_id in gids), None)
            pgp["detail"].append({
                "incident": inc,
                "orders": len(members),
                "group": g.group_id if g else None,
                "in_group": sum(1 for o in members if o in group_orders),
                "detected_at": g.detected_at.isoformat() if g else None,
                "third_registered": third.isoformat() if third else None,
            })

        visits = sum(1 for r in final.routes for s in r.stops if s.type == "VISIT")
        out.update({
            "events_sent": len(latencies) + len(rejected),
            "events_ok": len(latencies),
            "triggers_fired": fired,
            "triggers_skipped": skipped,
            "rejected": rejected,
            "latency_ms": {
                "max": round(max(latencies), 1) if latencies else 0,
                "median": round(statistics.median(latencies), 1) if latencies else 0,
                "p95": round(pct(latencies, 0.95), 1),
                "processing_ms_max": max(proc_ms) if proc_ms else 0,
                "by_type_max": {k: round(max(v), 1) for k, v in sorted(by_type.items())},
                "over_10s": sum(1 for x in latencies if x > 10000),
            },
            "versions": len(versions),
            "time_tick_with_changes": len(tick_changes),
            "violations": dict(viol),
            "violations_distinct": len(first_seen),
            "violations_forced_by_event": sum(1 for x in first_seen.values() if x == "forced"),
            "violation_examples": viol_examples,
            "late_risk": {
                "alerts": len(late),
                "moved_orders": len(moved),
                "moved_with_alert": with_alert,
                "moved_timely": timely,
                "timely_share": round(timely / len(moved), 3) if moved else None,
                "moved_too_late_to_be_timely": impossible,
                "alert_ids_unique": len(ids) == len(set(ids)),
                "alerts_only_grow": grows,
            },
            "pgp": pgp,
            "final": {
                "plan_version": final.plan_version,
                "orders": len(store.days[day_key].orders),
                "visits": visits,
                "unassigned": len(final.unassigned),
                "pgp_orders": len(group_orders),
                "cancelled": len(final.cancelled),
                "alerts": Counter(a.type.value for a in final.alerts),
            },
        })

    if args.json:
        print(json.dumps(out, ensure_ascii=False, default=str))
    else:
        lat = out["latency_ms"]
        lr = out["late_risk"]
        print(f"Сценарий {out['scenario']} ({date}): план {out['plan_sec']} с, версий {out['versions']}")
        print(f"  событий принято {out['events_ok']} из {out['events_sent']}; триггеров отправлено "
              f"{fired}, пропущено {skipped}; отклонено: {len(rejected)}")
        for x in rejected:
            print(f"    {x}")
        print(f"  задержка события, мс: макс {lat['max']}, медиана {lat['median']}, p95 {lat['p95']}, "
              f"> 10 с: {lat['over_10s']}; по типам (макс): {lat['by_type_max']}")
        print(f"  TIME_TICK с изменением маршрутов: {out['time_tick_with_changes']}")
        print(f"  нарушения H1–H10 по всем версиям: {sum(viol.values())} {dict(viol)}; различных "
              f"{out['violations_distinct']}, из них вынуждены событием {out['violations_forced_by_event']}")
        for x in viol_examples:
            print(f"    {x}")
        print(f"  LATE_RISK: {lr['alerts']}; нарядов переназначено/снято: {lr['moved_orders']}, "
              f"с LATE_RISK: {lr['moved_with_alert']}, своевременно: {lr['moved_timely']} "
              f"(доля {lr['timely_share']}); сняты уже позже slot.start − 30 мин: {lr['moved_too_late_to_be_timely']}")
        print(f"  alert_id уникальны: {lr['alert_ids_unique']}, список только пополняется: {lr['alerts_only_grow']}")
        print(f"  ПГП: групп {pgp['groups']}, эталонных инцидентов {pgp['incidents']}, "
              f"точность {pgp['precision']}, полнота {pgp['recall']} (без отменённых {pgp['recall_excl_cancelled']})")
        for d in pgp["detail"]:
            print(f"    {d}")
        f = out["final"]
        print(f"  итог: нарядов {f['orders']}, визитов {f['visits']}, unassigned {f['unassigned']}, "
              f"ПГП {f['pgp_orders']}, отменено {f['cancelled']}, предупреждения {dict(f['alerts'])}")
    unforced = out["violations_distinct"] - out["violations_forced_by_event"]
    return 0 if not unforced and not out["latency_ms"]["over_10s"] else 1


if __name__ == "__main__":
    sys.exit(main())
