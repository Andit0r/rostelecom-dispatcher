"""Сценарный анализ для интерфейса диспетчера (/ui/scenario/*), вне контракта /api/v1.

Сценарии работают на копии данных загруженного дня и ничего не меняют в Store:
снимок (справочники, матрица, наряды, отсутствующие, текущий план) берётся под
store.lock, расчёт идёт вне блокировки отдельным экземпляром того же планировщика.

База сравнения — первоначальный план дня:
* если план дня ещё версии 1 (событий не было) — это и есть текущий план, он не пересчитывается;
* если события уже были, текущий план отражает их, поэтому база строится заново на копии
  дня «как на момент загрузки» (наряды с arrived_at ≤ now, без отмен и событий) с тем же
  бюджетом, что и сценарий, — так база и сценарий сравнимы.

Сценарий «пробки по часам» (/traffic) базу не использует: строит два плана на копии
(без коэффициентов и с ними) и исполняет оба в пробках (traffic_replay).
"""

from __future__ import annotations

import dataclasses
import math
import random
import statistics
import threading
import time
from typing import Literal, Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .errors import ApiError
from .models import Equipment, Plan, Profile, StopType
from .planner.context import DayContext, hour_factors, traffic_enabled
from .planner.validator import validate_plan
from .service import _parse, _parse_date
from .staff_directory import display_name, remember_recommendation
from .state import DayState, MatrixState, ReferenceState

router = APIRouter(prefix="/ui/scenario", tags=["ui"])

EQUIPMENT_KINDS = ("ONT", "ROUTER", "STB")
BIG_BACKPACK = 99  # «бесконечный» рюкзак для оценки запаса
_busy = threading.Lock()  # один сценарий за раз: расчёт занимает CPU на десятки секунд


class StaffCutRequest(BaseModel):
    date: Optional[str] = None
    remove_count: int = Field(ge=0, le=15)
    strategy: str = Field(
        default="random",
        pattern=r"^(random|least_loaded|by_profile:(INSTALL|LTP3)|by_service_center:SC-[0-9A-Za-z_-]+)$",
    )
    seed: Optional[int] = None
    budget_sec: float = Field(default=15.0, ge=1, le=120)


class TrafficRequest(BaseModel):
    date: Optional[str] = None
    budget_sec: float = Field(default=15.0, ge=1, le=120)


class EquipmentRequest(BaseModel):
    date: Optional[str] = None
    mode: Literal["no_restock"] = "no_restock"
    budget_sec: float = Field(default=15.0, ge=1, le=120)


@dataclasses.dataclass
class _Snapshot:
    ref: ReferenceState
    matrix: MatrixState
    day: DayState  # день «как при загрузке», без плана и событий
    base_plan: Optional[Plan]  # текущий план, если он версии 1; иначе None — базу надо строить


# ---------------------------------------------------------------- снимок и расчёт

def _resolve_date(request: Request, value: Optional[str]) -> str:
    """Дата из запроса, иначе активный день (как /ui/plan/status). Сценарии считают на копии
    данных дня из Store, файлы каталога не нужны — день, пришедший через PUT /api/v1/days, подходит."""
    if value:
        return value
    from .ui_api import active_day
    day = active_day(request)
    if day is None:
        raise ApiError(409, "PLAN_NOT_BUILT",
                       "Сценарий недоступен: нет дня с построенным планом (загрузите день и постройте план)")
    return day.isoformat()


def _snapshot(svc, date_str: str) -> _Snapshot:
    day_date = _parse_date(date_str)
    with svc.store.lock:
        ref, matrix = svc._reference(), svc._matrix()
        day = svc._day(day_date)
        if day.plan is None:
            raise ApiError(409, "PLAN_NOT_BUILT", f"План на {day_date} ещё не построен")
        orders = {oid: o for oid, o in day.orders.items() if day.arrived_at.get(oid, day.now) <= day.now}
        initial = DayState(
            date=day.date,
            now=day.now,
            orders=orders,
            absent_staff_ids=set(day.absent_staff_ids),
            arrived_at={oid: day.now for oid in orders},
        )
        fresh = day.plan_version == 1 and not day.events
        base_plan = day.plan.model_copy(deep=True) if fresh else None
    return _Snapshot(ref=ref, matrix=matrix, day=initial, base_plan=base_plan)


def _time_limit(budget_sec: float) -> int:
    """time_limit_sec, при котором внутренний бюджет планировщика ≈ budget_sec
    (он берёт min(PLANNER_DAY_BUDGET_SEC, 0,8·limit − 3)), как _plan_time_limit в ui_api."""
    return max(1, min(300, math.ceil((budget_sec + 3.0) / 0.8)))


def _solve(svc, ref: ReferenceState, matrix: MatrixState, day: DayState, budget_sec: float,
           traffic: Optional[bool] = None) -> Plan:
    planner = type(svc.planner)()  # отдельный экземпляр: last_stats живого планировщика не трогаем
    if hasattr(planner, "seed") and hasattr(svc.planner, "seed"):
        planner.seed = svc.planner.seed
    if hasattr(planner, "traffic"):
        planner.traffic = traffic if traffic is not None else getattr(svc.planner, "traffic", None)
    return planner.build_plan(ref, matrix, day.clone(), time_limit_sec=_time_limit(budget_sec))


def _base_plan(svc, snap: _Snapshot, budget_sec: float) -> tuple[Plan, str]:
    if snap.base_plan is not None:
        return snap.base_plan, "current_plan_v1"
    return _solve(svc, snap.ref, snap.matrix, snap.day, budget_sec), "rebuilt_initial"


# ---------------------------------------------------------------- метрики

def _visits(plan: Plan) -> dict[str, str]:
    return {s.order_id: r.staff_id for r in plan.routes for s in r.stops if s.type == StopType.VISIT}


def metrics(plan: Plan, orders_total: int) -> dict:
    """Метрики плана, как в tools/bench/run_day.py: доля нарядов в слот = визиты / все наряды дня."""
    visits = len(_visits(plan))
    reasons: dict[str, int] = {}
    for u in plan.unassigned:
        reasons[str(u.reason_code)] = reasons.get(str(u.reason_code), 0) + 1
    used = [r for r in plan.routes if r.totals and r.totals.visits > 0]
    utils = [r.totals.utilization for r in used]
    mean = statistics.fmean(utils) if utils else 0.0
    cv = statistics.pstdev(utils) / mean if len(utils) > 1 and mean > 0 else 0.0
    travel = sum(r.totals.travel_sec for r in plan.routes if r.totals)
    return {
        "orders_total": orders_total,
        "visits": visits,
        "unassigned": len(plan.unassigned),
        "unassigned_by_reason": dict(sorted(reasons.items())),
        "pgp_orders": sum(len(g.order_ids) for g in plan.pgp_groups),
        "in_slot_share": round(visits / orders_total, 4) if orders_total else 0.0,
        "travel_h": round(travel / 3600, 2),
        "staff_used": len(used),
        "util_mean": round(mean, 4),
        "util_cv": round(cv, 4),
        "restocks": sum(1 for r in plan.routes for s in r.stops if s.type == StopType.RESTOCK),
    }


def _delta(base: dict, other: dict) -> dict:
    return {
        k: round(other[k] - v, 4)
        for k, v in base.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool) and k != "orders_total"
    }


# ---------------------------------------------------------------- выбор сотрудников

def _pick_staff(ref: ReferenceState, snap: _Snapshot, base: Plan, req: StaffCutRequest) -> list[str]:
    on_shift = sorted(sid for sid in ref.staff if sid not in snap.day.absent_staff_ids)
    rng = random.Random(req.seed if req.seed is not None else 1)
    strategy = req.strategy
    if strategy == "least_loaded":
        util = {r.staff_id: (r.totals.utilization if r.totals else 0.0) for r in base.routes}
        pool = sorted(on_shift, key=lambda sid: (util.get(sid, 0.0), sid))
        chosen = pool[: req.remove_count]
    else:
        if strategy == "random":
            pool = on_shift
        elif strategy.startswith("by_profile:"):
            profile = Profile(strategy.split(":", 1)[1])
            pool = [sid for sid in on_shift if profile in ref.staff[sid].profiles]
        else:
            sc = strategy.split(":", 1)[1]
            if sc not in ref.service_centers:
                raise ApiError(422, "UNKNOWN_SERVICE_CENTER", f"СЦ {sc} не найден",
                               [{"field": "strategy", "message": f"СЦ {sc} не найден"}])
            pool = [sid for sid in on_shift if ref.staff[sid].service_center_id == sc]
        if len(pool) < req.remove_count:
            chosen = []
        else:
            chosen = rng.sample(pool, req.remove_count)
    if len(chosen) < req.remove_count:
        raise ApiError(422, "NOT_ENOUGH_STAFF",
                       f"По стратегии {strategy} на смене только {len(pool)} сотрудников, "
                       f"а убрать нужно {req.remove_count}",
                       [{"field": "remove_count", "message": f"не больше {len(pool)}"}])
    return sorted(chosen)


# ---------------------------------------------------------------- сценарии

def run_staff_cut(svc, date_str: str, req: StaffCutRequest) -> dict:
    started = time.perf_counter()
    snap = _snapshot(svc, date_str)
    ref = snap.ref
    base, base_source = _base_plan(svc, snap, req.budget_sec)
    removed = _pick_staff(ref, snap, base, req)

    if removed:
        day = snap.day.clone()
        day.absent_staff_ids |= set(removed)
        scenario = _solve(svc, ref, snap.matrix, day, req.budget_sec)
    else:
        scenario = base  # никого не убрали — сценарий совпадает с базой, пересчёт не нужен

    total = len(snap.day.orders)
    m_base, m_scen = metrics(base, total), metrics(scenario, total)
    base_vis, scen_vis = _visits(base), _visits(scenario)
    unassigned = {u.order_id: u for u in scenario.unassigned}
    pgp = {oid for g in scenario.pgp_groups for oid in g.order_ids}
    affected = []
    for oid, sid in base_vis.items():
        if oid in scen_vis:
            continue
        o = snap.day.orders.get(oid)
        u = unassigned.get(oid)
        affected.append({
            "order_id": oid,
            "base_staff_id": sid,
            "base_staff_removed": sid in removed,
            "work_type_code": o.work_type_code if o else None,
            "is_repeat": bool(o and o.is_repeat),
            "slot": o.slot.model_dump(mode="json") if o else None,
            "address": o.address if o else None,
            "reason_code": str(u.reason_code) if u else ("PGP" if oid in pgp else None),
            "reason_text": u.reason_text if u else None,
        })
    affected.sort(key=lambda a: (a["slot"] or {}).get("start", ""))
    gained = sorted(oid for oid in scen_vis if oid not in base_vis)
    reassigned = sum(1 for oid, sid in base_vis.items() if oid in scen_vis and scen_vis[oid] != sid)

    base_routes = {r.staff_id: r for r in base.routes}
    removed_info = []
    for sid in removed:
        s = ref.staff[sid]
        r = base_routes.get(sid)
        removed_info.append({
            "staff_id": sid,
            "display_name": display_name(sid, s.display_name),
            "profiles": [str(p) for p in s.profiles],
            "service_center_id": s.service_center_id,
            "base_visits": r.totals.visits if r and r.totals else 0,
        })

    return {
        "date": date_str,
        "strategy": req.strategy,
        "remove_count": req.remove_count,
        "seed": req.seed if req.seed is not None else 1,
        "budget_sec": req.budget_sec,
        "base_source": base_source,
        "removed": removed_info,
        "base": m_base,
        "scenario": m_scen,
        "delta": _delta(m_base, m_scen),
        "affected_orders": affected,
        "newly_routed": gained,
        "reassigned": reassigned,
        "time_sec": round(time.perf_counter() - started, 2),
    }


def _percentile(values: list[int], p: float) -> int:
    """Перцентиль по ближайшему рангу (целые штуки АО)."""
    if not values:
        return 0
    s = sorted(values)
    return s[max(0, math.ceil(p * len(s)) - 1)]


def run_equipment(svc, date_str: str, req: EquipmentRequest) -> dict:
    started = time.perf_counter()
    snap = _snapshot(svc, date_str)
    ref = snap.ref
    base, base_source = _base_plan(svc, snap, req.budget_sec)

    big = Equipment(ONT=BIG_BACKPACK, ROUTER=BIG_BACKPACK, STB=BIG_BACKPACK)
    big_ref = dataclasses.replace(
        ref,
        staff={sid: s.model_copy(update={"equipment_start": big, "equipment_capacity": big})
               for sid, s in ref.staff.items()},
    )
    free = _solve(svc, big_ref, snap.matrix, snap.day, req.budget_sec)

    # расход АО за смену по каждому сотруднику в плане «без заездов»
    need: dict[str, dict[str, int]] = {}
    for r in free.routes:
        used = {k: 0 for k in EQUIPMENT_KINDS}
        n = 0
        for s in r.stops:
            if s.type != StopType.VISIT:
                continue
            o = snap.day.orders.get(s.order_id)
            if o is None:
                continue
            n += 1
            for k in EQUIPMENT_KINDS:
                used[k] += getattr(o.required_equipment, k)
        if n:
            need[r.staff_id] = used

    recommended = []
    for profile in (Profile.INSTALL, Profile.LTP3):
        staff = [s for s in ref.staff.values() if profile in s.profiles and s.staff_id not in snap.day.absent_staff_ids]
        if not staff:
            continue
        rows = [need[s.staff_id] for s in staff if s.staff_id in need]
        item = {"profile": str(profile), "staff_on_shift": len(staff), "staff_with_visits": len(rows)}
        over = 0
        for k in EQUIPMENT_KINDS:
            vals = [row[k] for row in rows]
            cap = max(getattr(s.equipment_capacity, k) for s in staff)
            item[k] = {
                "p50": _percentile(vals, 0.5),
                "p90": _percentile(vals, 0.9),
                "max": max(vals) if vals else 0,
                "current_start_avg": round(statistics.fmean(getattr(s.equipment_start, k) for s in staff), 1),
                "capacity": cap,
            }
        for s in staff:
            row = need.get(s.staff_id)
            if row and any(row[k] > getattr(s.equipment_start, k) for k in EQUIPMENT_KINDS):
                over += 1
        item["staff_exceeding_current_start"] = over
        recommended.append(item)

    total = len(snap.day.orders)
    m_now, m_free = metrics(base, total), metrics(free, total)
    remember_recommendation(date_str, recommended)  # подсказка «рекомендуемый рюкзак» в справочнике
    return {
        "date": date_str,
        "mode": req.mode,
        "budget_sec": req.budget_sec,
        "base_source": base_source,
        "current": m_now,
        "no_restock": m_free,
        "delta": _delta(m_now, m_free),
        "visits_gained": m_free["visits"] - m_now["visits"],
        "travel_saved_h": round(m_now["travel_h"] - m_free["travel_h"], 2),
        "recommended": recommended,
        "time_sec": round(time.perf_counter() - started, 2),
    }


# ---------------------------------------------------------------- пробки по часам

def traffic_replay(ref: ReferenceState, matrix: MatrixState, day: DayState, plan: Plan) -> dict:
    """Исполнение плана в пробках: тот же порядок остановок, переезды — ctx.tt по часу выезда.

    Остановка начинается не раньше запланированного начала и не раньше приезда; визит,
    начатый позже latest_start, — позднее прибытие (то, о чём предупреждал бы LATE_RISK).
    Для плана, построенного с коэффициентами, расписание совпадает с планом и опозданий нет.
    """
    ctx = DayContext(ref, matrix, day, list(day.orders.values()), day.now, traffic=True)
    travel = late = late_sec = on_time = 0
    for r in plan.routes:
        stops = r.stops
        if not any(s.type == StopType.VISIT for s in stops):
            continue
        t = ctx.to_sec(stops[0].end)
        loc = ctx.point_of[stops[0].location_id]
        for s in stops[1:]:
            st0, en0 = ctx.to_sec(s.start), ctx.to_sec(s.end)
            if s.type == StopType.BREAK:
                t = max(t, st0) + (en0 - st0)
                continue
            pt = ctx.point_of[s.location_id]
            tr = ctx.tt(loc, pt, t)
            travel += tr
            st = max(t + tr, st0)
            if s.type == StopType.VISIT and s.order_id in ctx.order_by_id:
                o = ctx.order_by_id[s.order_id]
                if st > o.b:
                    late += 1
                    late_sec += st - o.b
                else:
                    on_time += 1
            t, loc = st + (en0 - st0), pt
    return {"travel_traffic_h": round(travel / 3600, 2), "late_visits": late,
            "late_min": round(late_sec / 60), "on_time_visits": on_time}


def run_traffic(svc, date_str: str, req: TrafficRequest) -> dict:
    started = time.perf_counter()
    snap = _snapshot(svc, date_str)
    ref, matrix, day = snap.ref, snap.matrix, snap.day
    total = len(day.orders)
    out = {}
    for name, traffic in (("off", False), ("on", True)):
        t0 = time.perf_counter()
        plan = _solve(svc, ref, matrix, day, req.budget_sec, traffic=traffic)
        m = metrics(plan, total)
        m.update(traffic_replay(ref, matrix, day, plan))
        m["in_slot_share_traffic"] = round(m["on_time_visits"] / total, 4) if total else 0.0
        m["violations"] = len(validate_plan(plan, ref, matrix, day))
        m["time_sec"] = round(time.perf_counter() - t0, 2)
        out[name] = m
    return {
        "date": date_str,
        "budget_sec": req.budget_sec,
        "traffic_env": traffic_enabled(),
        "factors": [{"hour": h, "factor": f} for h, f in enumerate(hour_factors(matrix)) if f > 1.0],
        "off": out["off"],
        "on": out["on"],
        "delta": _delta(out["off"], out["on"]),
        "time_sec": round(time.perf_counter() - started, 2),
    }


# ---------------------------------------------------------------- маршруты

async def _run(request: Request, model, fn) -> dict:
    req = _parse(model.model_validate_json, (await request.body()) or b"{}")
    svc = request.app.state.service
    date_str = _resolve_date(request, req.date)
    _parse_date(date_str)
    if not _busy.acquire(blocking=False):
        raise ApiError(409, "SCENARIO_BUSY", "Уже идёт расчёт другого сценария, повторите позже")
    try:
        return await run_in_threadpool(fn, svc, date_str, req)
    finally:
        _busy.release()


@router.post("/staff-cut")
async def staff_cut(request: Request) -> dict:
    return await _run(request, StaffCutRequest, run_staff_cut)


@router.post("/equipment")
async def equipment(request: Request) -> dict:
    return await _run(request, EquipmentRequest, run_equipment)


@router.post("/traffic")
async def traffic(request: Request) -> dict:
    return await _run(request, TrafficRequest, run_traffic)
