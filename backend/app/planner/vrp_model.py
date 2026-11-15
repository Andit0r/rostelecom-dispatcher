"""Отображение дня на PyVRP 0.14 (docs/formalization.md, раздел 4; проверки — docs/planner-notes.md).

* Наряд -> `Client` со своей `Location` (нужно для запрета дуг по профилям H1):
  окно [slot.start, latest_start], service = норматив, delivery = АО, prize, required=False.
* Сотрудник -> `VehicleType(num_available=1)`: старт/финиш в своём СЦ, окно смены,
  capacity = equipment_capacity, initial_load = capacity - equipment_start,
  reload_depots = все СЦ (отдельные объекты Depot с окном [open, close - restock] и
  service_duration = restock), max_reloads.
* H1 -> профили: сотрудники с одинаковым множеством допустимых нарядов делят профиль,
  дуги в недопустимые наряды получают запретное значение.
* Перерыв в PyVRP точно не выражается, его ставит декодер. PLANNER_BREAK_MODE:
  `shift_duration` (по умолчанию) — длительность маршрута ≤ F − S − β;
  `shorten` — tw_late = F − β; `client` — обязательный клиент-перерыв своего
  сотрудника (по замерам хуже, см. docs/planner-notes.md); `none` — без учёта.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
from pyvrp import Client, Depot, Location, ProblemData, VehicleType
from pyvrp import solve as pyvrp_solve
from pyvrp.stop import MaxRuntime

from .context import DayContext, OrderCtx

FORBIDDEN = 10**8


@dataclass
class Weights:
    """Веса целевой функции в секундах пути (docs/formalization.md, раздел 3)."""

    w3: int
    w2: int
    w1: int

    @classmethod
    def from_ctx(cls, ctx: DayContext) -> "Weights":
        tau_max = max(ctx.tau_max, 60)
        w2 = 3 * tau_max
        return cls(w3=1, w2=w2, w1=5 * w2)


def break_mode() -> str:
    return os.environ.get("PLANNER_BREAK_MODE", "shift_duration")


def max_reloads() -> int:
    return int(os.environ.get("PLANNER_MAX_RELOADS", "2"))


def build_problem(ctx: DayContext, orders: list[OrderCtx], weights: Weights) -> tuple[ProblemData, list[int]]:
    """ProblemData и список индексов StaffCtx по типам ТС."""
    n_sc = len(ctx.scs)
    locations = [Location(0.0, 0.0, name=sc.sc_id) for sc in ctx.scs]
    loc_points = [sc.point for sc in ctx.scs]
    for o in orders:
        locations.append(Location(0.0, 0.0, name=o.order_id))
        loc_points.append(o.point)

    depots = [Depot(i, tw_early=0, tw_late=48 * 3600, service_duration=0, name=f"{sc.sc_id}") for i, sc in enumerate(ctx.scs)]
    for i, sc in enumerate(ctx.scs):
        depots.append(
            Depot(
                i,
                tw_early=sc.open,
                tw_late=max(sc.open, sc.close - sc.rho),
                service_duration=sc.rho,
                name=f"{sc.sc_id}-restock",
            )
        )
    reload_ids = list(range(n_sc, 2 * n_sc))
    sc_pos = {sc.sc_id: i for i, sc in enumerate(ctx.scs)}

    clients = [
        Client(
            n_sc + j,
            delivery=list(o.dem),
            service_duration=o.p,
            tw_early=max(o.a, ctx.now),
            tw_late=o.b,
            prize=weights.w1 * o.weight,
            required=False,
            name=o.order_id,
        )
        for j, o in enumerate(orders)
    ]

    mode = break_mode()
    n_orders = len(orders)
    n_real = n_sc + n_orders
    # Режим "client": перерыв — обязательный клиент своего сотрудника с окном
    # [break_window.from, to] и service = break_duration. Дуга в него 0, из него в Y —
    # минимальное входящее время в Y (нижняя оценка дуги prev→Y). Позицию перерыва
    # декодер выбирает заново; здесь он только резервирует время внутри окна.
    break_client: dict[int, int] = {}
    if mode == "client":
        for k in ctx.staff:
            if k.beta <= 0:
                continue
            break_client[k.idx] = len(clients)
            locations.append(Location(0.0, 0.0, name=f"break-{k.staff_id}"))
            clients.append(
                Client(
                    n_real + len(break_client) - 1,
                    delivery=[0, 0, 0],
                    service_duration=k.beta,
                    tw_early=k.bf,
                    tw_late=k.bt,
                    required=True,
                    name=f"break-{k.staff_id}",
                )
            )
    n_loc = len(locations)

    # ---- профили по множеству допустимых нарядов (и своему перерыву)
    tmp: dict[int, set[int]] = {k.idx: set() for k in ctx.staff}
    for j, o in enumerate(orders):
        for kidx in o.eligible:
            tmp[kidx].add(j)
    allowed_by_staff: dict[int, tuple] = {
        k: (frozenset(v), break_client.get(k)) for k, v in tmp.items()
    }
    profile_of: dict[tuple, int] = {}
    for k in ctx.staff:
        profile_of.setdefault(allowed_by_staff[k.idx], len(profile_of))

    idx = np.asarray(loc_points, dtype=np.int64)
    base_dur = np.zeros((n_loc, n_loc), dtype=np.int64)
    base_dur[:n_real, :n_real] = ctx.dur_np[np.ix_(idx, idx)]
    if break_client:
        real = base_dur[:n_real, :n_real].astype(np.float64)
        np.fill_diagonal(real, np.inf)
        min_in = real.min(axis=0)
        min_in[~np.isfinite(min_in)] = 0
        base_dur[n_real:, :n_real] = min_in.astype(np.int64)[None, :]
        base_dur[:, n_real:] = 0
    break_cols = list(range(n_real, n_loc))
    dist_mats, dur_mats = [], []
    for (allowed, own_break), _ in sorted(profile_of.items(), key=lambda kv: kv[1]):
        dur = base_dur.copy()
        banned = [n_sc + j for j in range(n_orders) if j not in allowed]
        banned += [c for c in break_cols if own_break is None or c != n_sc + own_break]
        if banned:
            dur[:, banned] = FORBIDDEN
            dur[banned, banned] = 0
        dist_mats.append(dur.copy())
        dur_mats.append(dur)

    vehicle_types = []
    staff_of_type: list[int] = []
    for k in ctx.staff:
        start = max(k.start, ctx.now)
        tw_late = k.end
        shift_duration = np.iinfo(np.int64).max
        if mode == "shorten":
            tw_late = k.end - k.beta
        elif mode == "shift_duration":
            shift_duration = max(0, k.end - k.start - k.beta)
        vehicle_types.append(
            VehicleType(
                num_available=1,
                capacity=list(k.cap),
                start_depot=sc_pos[k.sc.sc_id],
                end_depot=sc_pos[k.sc.sc_id],
                tw_early=start,
                tw_late=max(start, tw_late),
                shift_duration=int(shift_duration),
                unit_distance_cost=1,
                profile=profile_of[allowed_by_staff[k.idx]],
                initial_load=[c - q for c, q in zip(k.cap, k.q0)],
                reload_depots=reload_ids,
                max_reloads=max_reloads(),
                name=k.staff_id,
            )
        )
        staff_of_type.append(k.idx)

    data = ProblemData(
        locations=locations,
        clients=clients,
        depots=depots,
        vehicle_types=vehicle_types,
        distance_matrices=dist_mats,
        duration_matrices=dur_mats,
    )
    return data, staff_of_type


def solve_sequences(
    ctx: DayContext,
    orders: list[OrderCtx],
    weights: Weights,
    runtime_sec: float,
    seed: int = 1,
) -> tuple[dict[int, list[OrderCtx]], dict]:
    """Решает задачу PyVRP и возвращает порядок визитов по сотрудникам (индексы StaffCtx)."""
    if not orders or not ctx.staff:
        return {}, {"feasible": True, "iterations": 0}
    data, staff_of_type = build_problem(ctx, orders, weights)
    result = pyvrp_solve(data, stop=MaxRuntime(max(0.1, runtime_sec)), seed=seed, display=False)
    best = result.best
    seqs: dict[int, list[OrderCtx]] = {}
    for route in best.routes():
        kidx = staff_of_type[route.vehicle_type()]
        # в schedule() у активности клиента idx — номер клиента (0..num_clients-1)
        # (клиенты с номером ≥ len(orders) — перерывы режима "client")
        seqs[kidx] = [
            orders[act.idx] for act in route.schedule() if act.is_client() and act.idx < len(orders)
        ]
    info = {
        "feasible": best.is_feasible(),
        "iterations": result.num_iterations,
        "cost": result.cost(),
        "runtime": result.runtime,
    }
    return seqs, info
