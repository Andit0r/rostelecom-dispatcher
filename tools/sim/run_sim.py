#!/usr/bin/env python3
"""Симулятор нагрузки и сквозные проверки: работает только через HTTP API (как внешний клиент).

    python tools/sim/run_sim.py --base-url http://localhost:8080 --scenario all --seed 1 --report out.md

Запускать интерпретатором backend/.venv (нужны pydantic и httpx; модели и validator.py берутся из backend
только для независимой проверки плана). Сервер не изменяется и не импортируется.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib
import json
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

SCENARIOS = ["contract", "bulk", "calls", "churn", "pgp", "late"]  # contract первым: проверяет чистое состояние


def build_client(base_url: str):
    import httpx

    from simlib.client import ApiClient

    http = httpx.Client(base_url=base_url.rstrip("/"), timeout=330.0,
                        limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))
    return ApiClient(http)


def run_scenarios(ctx, names: list[str], log=print) -> list:
    results = []
    for name in names:
        mod = importlib.import_module(f"scenarios.{name}")
        t0 = time.perf_counter()
        before = ctx.client.snapshot_counts()
        log(f"== сценарий {name} ...")
        try:
            res = mod.run(ctx)
        except Exception as exc:  # noqa: BLE001 - сбой сценария не должен ронять прогон
            from simlib.report import ScenarioResult

            res = ScenarioResult(name, ctx.seed)
            res.add("error", "исключение в сценарии", "".join(traceback.format_exception_only(type(exc), exc)).strip()
                    + " | " + traceback.format_exc()[-700:], "run")
        res.duration_sec = time.perf_counter() - t0
        res.latency = scenario_latency(ctx.client, before)
        results.append(res)
        log(f"   {res.status}: проверок {res.checks}, не прошло {res.failed}, событий {res.events}, "
            f"{res.duration_sec:.1f} с")
    return results


def scenario_latency(client, before: dict[str, int]) -> dict:
    from simlib.client import pct

    out = {}
    with client.lock:
        for label, v in client.samples.items():
            part = v[before.get(label, 0):]
            if part:
                out[label] = {"n": len(part), "p50": round(pct(part, 0.5), 3), "p95": round(pct(part, 0.95), 3),
                              "max": round(max(part), 3)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:8080")
    ap.add_argument("--scenario", default="all", help="bulk|calls|churn|pgp|late|contract|all или список через запятую")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--report", help="путь отчёта .md; рядом пишется .json")
    ap.add_argument("--scale", type=float, default=1.0, help="доля нарядов дня (0..1); 1 - полный день")
    ap.add_argument("--time-limit", type=int, default=60, help="time_limit_sec для POST /plan (1..300)")
    ap.add_argument("--quick", action="store_true", help="уменьшенные длины сценариев (как в pytest)")
    ap.add_argument("--date", help="дата дня YYYY-MM-DD для bulk/calls/churn/pgp/late: наряды эталонного дня "
                                   "(events_dense) переносятся на неё; по умолчанию сегодня или ближайший рабочий день")
    ap.add_argument("--native-dates", action="store_true",
                    help="без переноса: каждый сценарий берёт свой день из orders_train.json (как в pytest)")
    ap.add_argument("--calls-single", type=int)
    ap.add_argument("--calls-batches", type=int)
    ap.add_argument("--calls-parallel", type=int)
    ap.add_argument("--calls-empty", action="store_true", help="calls с пустого дня")
    ap.add_argument("--churn-intensity", type=float, help="событий в модельный час")
    ap.add_argument("--churn-absent-share", type=float, help="доля персонала, выбывающего за день (0..1)")
    ap.add_argument("--churn-tick-min", type=int)
    ap.add_argument("--late-attempts", type=int)
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    names = SCENARIOS if args.scenario == "all" else [x.strip() for x in args.scenario.split(",") if x.strip()]
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        ap.error(f"неизвестный сценарий: {unknown}; доступны {SCENARIOS}")

    from simlib.data import SynthData, default_date
    from simlib.report import build_json, render_markdown
    from simlib.session import Ctx

    client = build_client(args.base_url)
    h = client.health()
    if h.status != 200:
        print(f"сервер {args.base_url} не отвечает на /api/v1/health: {h.status} {h.text}")
        return 2
    data = SynthData()
    day_options: dict = {}
    if not args.native_dates:
        target = args.date or default_date()
        try:
            dt.date.fromisoformat(target)
        except ValueError:
            ap.error(f"--date: ожидается YYYY-MM-DD, получено {target!r}")
        info = data.retarget(target)
        print(f"[i] день {target}: наряды эталонного дня {info['reference']} перенесены ({info['orders']} со слотом в этот день)")
        if info["weekend"]:
            print(f"[!] {target} — выходной: наряды и контрольные сроки рассчитаны по рабочим дням; день всё равно загружается")
        day_options = {k: target for k in ("date", "calls_date", "churn_date", "pgp_date", "late_date")}
    elif args.date:
        day_options = {"date": args.date}
    options = {k: v for k, v in {
        **day_options, "calls_single": args.calls_single, "calls_batches": args.calls_batches,
        "calls_parallel": args.calls_parallel, "calls_empty": args.calls_empty,
        "churn_intensity": args.churn_intensity, "churn_absent_share": args.churn_absent_share,
        "churn_tick_min": args.churn_tick_min, "late_attempts": args.late_attempts}.items() if v is not None}
    ctx = Ctx(client, data, seed=args.seed, scale=args.scale, time_limit=args.time_limit,
              quick=args.quick, options=options)
    results = run_scenarios(ctx, names)
    meta = {"base_url": args.base_url, "seed": args.seed, "scale": args.scale, "time_limit": args.time_limit,
            "quick": args.quick, "scenarios": names, "generated": dt.datetime.now().isoformat(timespec="seconds"),
            "health": h.data}
    lat = client.latency_table()
    md = render_markdown(results, meta, lat, client.server_errors)
    if args.report:
        p = Path(args.report)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8")
        p.with_suffix(".json").write_text(json.dumps(build_json(results, meta, lat, client.server_errors),
                                                     ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"отчёт: {p} и {p.with_suffix('.json')}")
    else:
        print(md)
    failed = [r.name for r in results if r.status != "PASS"]
    print("итог:", "все сценарии прошли" if not failed else f"упали: {', '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
