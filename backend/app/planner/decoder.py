"""Декодер маршрута: последовательность нарядов сотрудника -> точное расписание остановок.

Расписание строится «как можно раньше» по компактной матрице дня (H6), с проверкой
окон начала визита (H4), смены (H2), АО с пополнением в открытом СЦ (H7, H8) и ровно
одного перерыва внутри break_window (H3). Перерыв ставится на месте предыдущей
остановки, время в пути до него 0.

Пополнение (RESTOCK) вставляется непосредственно перед визитом, на котором остаток
стал бы отрицательным, в СЦ с минимальным крюком среди открытых на всём интервале
пополнения. Позиция перерыва перебирается от самой поздней допустимой к ранней.

Отступления от «как можно раньше», допустимые по H6 (там неравенство):
DEPOT_START ставится к выезду на первый визит, DEPOT_END.arrival — конец смены.

Пробки (TRAFFIC_FACTORS=on, ctx.traffic): время каждого переезда берётся ctx.tt(i, j, t)
по часу отправления t, не меньше базового τ — H6 по базовой матрице сохраняется.
Грубые отсечки вставки ниже идут по базовой τ: это нижняя граница, отсечка остаётся верной.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .context import DayContext, OrderCtx, ScCtx, StaffCtx

# docs/formalization.md, раздел 4: DEPOT_END.arrival = F_k, чтобы при событиях
# незамороженный DEPOT_END не «запирал» освободившегося сотрудника.
DEPOT_END_AT_SHIFT_END = True


def break_slack_sec() -> int:
    """Запас между началом перерыва и break_window.to (этап B, docs/planner-notes.md).

    Перерыв ставится сразу после визита; если этот визит затянется (VISIT_DELAYED), перерыв
    сдвинется. Поэтому сначала ищется самая поздняя позиция перерыва с началом не позже
    `to − запас`, и только если такой нет — самая поздняя допустимая.
    """
    try:
        return int(float(os.environ.get("PLANNER_BREAK_SLACK_MIN", "60")) * 60)
    except ValueError:
        return 3600

DEPOT_START = "DEPOT_START"
VISIT = "VISIT"
BREAK = "BREAK"
RESTOCK = "RESTOCK"
DEPOT_END = "DEPOT_END"


@dataclass
class SchedStop:
    kind: str
    point: int
    arr: int
    st: int
    en: int
    travel: int
    inv: tuple[int, int, int]
    order: OrderCtx | None = None
    sc: ScCtx | None = None


@dataclass
class RouteSchedule:
    staff: StaffCtx
    orders: list[OrderCtx]
    stops: list[SchedStop] = field(default_factory=list)
    travel: int = 0  # с, по матрице, включая пополнения и возврат

    @property
    def work(self) -> int:
        return sum(o.p for o in self.orders)

    @property
    def utilization(self) -> float:
        if not self.orders:
            return 0.0
        return (self.work + self.travel) / self.staff.net_shift


def _forward(
    ctx: DayContext,
    k: StaffCtx,
    seq: list[OrderCtx],
    brk: int | None,
    ignore_eq: bool,
    t0: int,
    bt_limit: int | None = None,
) -> RouteSchedule | None:
    """Один проход. brk — после какого визита перерыв (-1 — сразу после выезда, None — без перерыва).

    bt_limit — крайнее начало перерыва (по умолчанию break_window.to)."""
    bt = k.bt if bt_limit is None else bt_limit
    tau = ctx.tau
    traffic = ctx.traffic
    tt = ctx.tt
    loc = k.sc.point
    t = t0
    inv = k.q0
    travel = 0
    stops = [SchedStop(DEPOT_START, loc, t, t, t, 0, inv, sc=k.sc)]

    def take_break() -> bool:
        nonlocal t
        st = max(t, k.bf)
        if st > bt:
            return False
        en = st + k.beta
        stops.append(SchedStop(BREAK, loc, t, st, en, 0, inv))
        t = en
        return True

    if brk == -1 and not take_break():
        return None

    for j, o in enumerate(seq):
        d = o.dem
        if not ignore_eq and (inv[0] < d[0] or inv[1] < d[1] or inv[2] < d[2]):
            cap = k.cap
            if cap[0] < d[0] or cap[1] < d[1] or cap[2] < d[2]:
                return None
            best = None
            for sc in ctx.scs:
                to_sc = tt(loc, sc.point, t) if traffic else tau[loc][sc.point]
                arr = t + to_sc
                st = max(arr, sc.open)
                en = st + sc.rho
                if en > sc.close:
                    continue
                from_sc = tt(sc.point, o.point, en) if traffic else tau[sc.point][o.point]
                if max(en + from_sc, o.a) > o.b:
                    continue
                detour = to_sc + from_sc
                if best is None or detour < best[0]:
                    best = (detour, sc, arr, st, en, to_sc)
            if best is None:
                return None
            _, sc, arr, st, en, to_sc = best
            inv = cap
            stops.append(SchedStop(RESTOCK, sc.point, arr, st, en, to_sc, inv, sc=sc))
            travel += to_sc
            t = en
            loc = sc.point
        tr = tt(loc, o.point, t) if traffic else tau[loc][o.point]
        arr = t + tr
        st = arr if arr > o.a else o.a
        if st > o.b:
            return None
        en = st + o.p
        if not ignore_eq:
            inv = (inv[0] - d[0], inv[1] - d[1], inv[2] - d[2])
        stops.append(SchedStop(VISIT, o.point, arr, st, en, tr, inv, order=o))
        travel += tr
        t = en
        loc = o.point
        if brk == j and not take_break():
            return None

    tr = tt(loc, k.sc.point, t) if traffic else tau[loc][k.sc.point]
    arr = t + tr
    if arr > k.end:
        return None
    travel += tr
    stops.append(SchedStop(DEPOT_END, k.sc.point, arr, arr, arr, tr, inv, sc=k.sc))
    return RouteSchedule(staff=k, orders=list(seq), stops=stops, travel=travel)


def shift_departure(ctx: DayContext, s0: SchedStop, s1: SchedStop) -> int:
    """Сдвиг DEPOT_START к выезду на первую остановку s1; возвращает изменение пути.

    С пробками время в пути зависит от часа выезда: позднее отправление может попасть в
    другой час, поэтому выезд подбирается так, чтобы приезд был не позже s1.st (иначе
    DEPOT_START остаётся на месте)."""
    if not ctx.traffic:
        depart = max(s0.st, s1.st - s1.travel)
        s0.arr = s0.st = s0.en = depart
        s1.arr = depart + s1.travel
        return 0
    depart, tr = s0.st, s1.travel
    cand = s1.st - s1.travel
    for _ in range(3):
        if cand <= s0.st:
            break
        tr_c = ctx.tt(s0.point, s1.point, cand)
        if cand + tr_c <= s1.st:
            depart, tr = cand, tr_c
            break
        cand = s1.st - tr_c
    delta = tr - s1.travel
    s0.arr = s0.st = s0.en = depart
    s1.travel = tr
    s1.arr = depart + tr
    return delta


def _finalize(sched: RouteSchedule, ctx: DayContext) -> RouteSchedule:
    stops = sched.stops
    first = stops[1]
    if first.kind == VISIT:
        sched.travel += shift_departure(ctx, stops[0], first)
    if DEPOT_END_AT_SHIFT_END:
        last = stops[-1]
        last.arr = last.st = last.en = max(last.arr, sched.staff.end)
    return sched


def decode(
    ctx: DayContext,
    k: StaffCtx,
    seq: list[OrderCtx],
    ignore_eq: bool = False,
    t0: int | None = None,
) -> RouteSchedule | None:
    """Точное расписание для порядка визитов `seq` или None, если он недопустим."""
    if not seq:
        return RouteSchedule(staff=k, orders=[], stops=[], travel=0)
    if t0 is None:
        t0 = max(k.start, ctx.now)
    base = _forward(ctx, k, seq, None, ignore_eq, t0)
    if base is None:
        return None  # перерыв только задерживает: без него тоже недопустимо
    # концы остановок-кандидатов (DEPOT_START и визиты) в проходе без перерыва
    ends = [base.stops[0].en] + [s.en for s in base.stops if s.kind == VISIT]
    slack = break_slack_sec()
    limits = [k.bt - slack, k.bt] if slack > 0 and k.bt - slack >= k.bf else [k.bt]
    for limit in limits:
        for j in range(len(seq) - 1, -2, -1):
            if ends[j + 1] > limit:
                continue
            res = _forward(ctx, k, seq, j, ignore_eq, t0, bt_limit=limit)
            if res is not None:
                return _finalize(res, ctx)
    return None


# ---------------------------------------------------------------- вставка

def insertion_candidates(ctx: DayContext, sched: RouteSchedule, o: OrderCtx) -> list[tuple[int, int]]:
    """Позиции вставки `o` в маршрут, упорядоченные по приросту пути: [(delta, pos)].

    Грубый фильтр по времени: конец предыдущего визита + дорога не позже latest_start.
    """
    k = sched.staff
    tau = ctx.tau
    seq = sched.orders
    if not seq:
        start = max(k.start, ctx.now)
        if start + tau[k.sc.point][o.point] > o.b:
            return []
        return [(tau[k.sc.point][o.point] + tau[o.point][k.sc.point], 0)]
    visit_ends = [s.en for s in sched.stops if s.kind == VISIT]
    out = []
    for pos in range(len(seq) + 1):
        prev_pt = k.sc.point if pos == 0 else seq[pos - 1].point
        next_pt = k.sc.point if pos == len(seq) else seq[pos].point
        prev_end = max(k.start, ctx.now) if pos == 0 else visit_ends[pos - 1]
        if prev_end + tau[prev_pt][o.point] > o.b:
            break  # дальше концы только позже
        # следующий визит должен начаться не раньше нашего окна: грубая отсечка
        if pos < len(seq) and o.a + o.p + tau[o.point][next_pt] > seq[pos].b:
            continue
        delta = tau[prev_pt][o.point] + tau[o.point][next_pt] - tau[prev_pt][next_pt]
        out.append((delta, pos))
    out.sort()
    return out


def best_insertion(
    ctx: DayContext,
    sched: RouteSchedule,
    o: OrderCtx,
    max_tries: int = 6,
    ignore_eq: bool = False,
) -> tuple[int, RouteSchedule] | None:
    """Лучшая допустимая вставка по приросту пути (проверка точным декодером)."""
    best: tuple[int, RouteSchedule] | None = None
    tries = 0
    for _, pos in insertion_candidates(ctx, sched, o):
        seq = sched.orders[:pos] + [o] + sched.orders[pos:]
        res = decode(ctx, sched.staff, seq, ignore_eq=ignore_eq)
        tries += 1
        if res is not None:
            cost = res.travel - sched.travel
            if best is None or cost < best[0]:
                best = (cost, res)
        if tries >= max_tries and best is not None:
            break
        if tries >= 3 * max_tries:
            break
    return best
