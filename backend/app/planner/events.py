"""Перепланирование по событиям (этап B): docs/formalization.md, разделы 5–7.

Модель исполнения (data/api_spec.yaml, раздел 3): сотрудники выполняют текущую версию
плана в точности, отклонения приходят событиями. Поэтому маршрут, которого событие не
коснулось, переносится в новую версию без изменений, а пересчитываются только
«затронутые» маршруты.

Маршрут затронутого сотрудника делится на замороженный префикс (остановки со
`start < t`; по VISIT_DELAYED / ORDER_TRANSFERRED — ещё и визит из события с
`start ≤ t`) и хвост — последовательность нарядов, которую заново расписывает
декодер хвоста `decode_tail`:

* первая остановка хвоста, совпадающая с прежней (тот же тип/наряд и тот же
  предшественник), уезжает в `end` последней замороженной остановки; любая другая —
  не раньше `max(end, t)` (H10, правило нового предшественника);
* все остановки хвоста начинаются не раньше `t` (и не раньше `until` у выбывшего);
* перерыв (H3) ставится ровно один, если его нет в префиксе; позиция перебирается
  от поздней к ранней; у выбывшего перерыв не нужен, если остаток окна перерыва
  целиком попал в недоступность (docs/planner-notes.md, «Этап B: допущения»);
* RESTOCK — перед визитом, где остаток АО ушёл бы в минус (как в decoder.py);
* DEPOT_END — в конце смены (как в decoder.py).

Алгоритм на событие (внутренний дедлайн PLANNER_EVENT_DEADLINE_SEC = 7 с):
1. детерминированная правка по типу события, проверка противоречий (422);
2. прогноз «без вмешательства» по затронутым маршрутам (ослабленный проход
   декодера без верхних границ окон) — источник LATE_RISK;
3. ремонт затронутых маршрутов: перестановка внутри маршрута, иначе снятие наряда
   (сначала с меньшим весом, затем с запасом ≥ 30 мин до слота, затем более поздний);
4. вставка снятых, нового и (в освободившееся время) нераспределённых нарядов в
   хвосты допустимых сотрудников по минимальному приросту пути, для снятых — цепочка
   выталкивания длины 2 (выталкивается наряд с запасом ≥ 30 мин до слота);
5. предупреждения: LATE_RISK на каждый наряд, сменивший исполнителя или снятый из
   маршрута, и на каждый наряд с прогнозом опоздания; UNASSIGNED на новый
   нераспределённый наряд; закрытие предупреждений по нарядам, вернувшимся в маршрут;
6. самопроверка validate_plan + validate_transition; наряды с нарушением в хвосте
   снимаются в unassigned (не должно срабатывать).
"""

from __future__ import annotations

import datetime as dt
import math
import os
import time
from dataclasses import dataclass, field

from ..errors import ApiError
from ..models import (
    Alert,
    AlertAction,
    AlertStatus,
    AlertType,
    CancelledOrder,
    Equipment,
    Event,
    PgpAction,
    PgpGroup,
    Plan,
    ReasonCode,
    Route,
    RouteStatus,
    RouteTotals,
    Stop,
    StopStatus,
    StopType,
    UnassignedAction,
    UnassignedOrder,
)
from ..state import DayState, MatrixState, ReferenceState
from . import assemble
from .base import PlanUpdate
from .context import DayContext, OrderCtx, StaffCtx
from .decoder import BREAK, DEPOT_END, DEPOT_START, RESTOCK, VISIT, SchedStop, break_slack_sec, shift_departure
from .pgp import PGP_MIN_ORDERS, PGP_WINDOW
from . import joint as jnt
from .validator import validate_plan, validate_transition

TIMELY_SEC = 30 * 60  # LATE_RISK своевременен, если создан не позднее slot.start − 30 мин
EJECT_WINDOW_SEC = 3 * 3600


def event_deadline_sec() -> float:
    try:
        return float(os.environ.get("PLANNER_EVENT_DEADLINE_SEC", "7"))
    except ValueError:
        return 7.0


def _key(stop: Stop) -> tuple:
    """Идентичность остановки для сравнения версий (H10: «тот же предшественник»)."""
    if stop.type == StopType.VISIT:
        return (VISIT, stop.order_id)
    if stop.type == StopType.RESTOCK:
        return (RESTOCK, stop.location_id)
    return (stop.type.value,)


# ---------------------------------------------------------------- состояние сотрудника

@dataclass
class TailSched:
    stops: list[SchedStop]
    travel: int
    starts: list[int]  # начало визитов seq
    ends: list[int]


@dataclass
class Anchor:
    k: StaffCtx
    prev_route: Route | None
    prefix: list[Stop]
    loc: int  # точка последней замороженной остановки (или СЦ при пустом префиксе)
    en: int  # её конец, с
    inv: tuple[int, int, int]
    break_done: bool
    prefix_visits: int
    first_key: tuple | None  # прежняя первая остановка хвоста
    status: RouteStatus
    unav_from: int | None = None
    until: int | None = None  # конец недоступности, если он ещё впереди (сотрудник продолжает смену из СЦ)
    unav_until: int | None = None  # конец недоступности из события (None — до конца смены)
    blocked: bool = False  # недоступен до конца смены
    closed: bool = False  # DEPOT_END уже заморожен
    seq: list[OrderCtx] = field(default_factory=list)
    prev_seq_ids: list[str] = field(default_factory=list)
    prev_break_before: set[str] | None = None  # наряды хвоста до перерыва в прежнем плане
    sched: TailSched | None = None
    sched_ok: bool | None = None  # None — ещё не декодирован
    touched: bool = False

    @property
    def available(self) -> bool:
        return not (self.blocked or self.closed)

    def break_exempt(self) -> bool:
        """Допущение этапа B: перерыв не нужен, если остаток окна перерыва (после выбытия)
        целиком попал в недоступность."""
        if self.status != RouteStatus.UNAVAILABLE:
            return False
        return self.unav_until is None or self.unav_until > self.k.bt

    def needs_break(self, seq: list[OrderCtx]) -> bool:
        if self.break_done or self.break_exempt():
            return False
        return self.prefix_visits > 0 or bool(seq)

    def needs_depot_end(self, seq: list[OrderCtx], need_break: bool) -> bool:
        if self.blocked or self.closed:
            return False
        if self.status == RouteStatus.ACTIVE:
            return bool(self.prefix) or bool(seq) or need_break
        return bool(seq) or need_break


def _pass(
    ctx: DayContext,
    A: Anchor,
    seq: list[OrderCtx],
    brk: int | None,
    t: int,
    relaxed: bool = False,
    bt_limit: int | None = None,
) -> TailSched | None:
    """Один проход декодера хвоста; brk — после какого визита seq перерыв (-1 — сразу)."""
    tau = ctx.tau
    traffic = ctx.traffic
    k = A.k
    bt = k.bt if bt_limit is None else bt_limit
    until = A.until
    stops: list[SchedStop] = []
    starts: list[int] = []
    ends: list[int] = []
    travel = 0
    if A.prefix:
        loc, cur, inv = A.loc, A.en, A.inv
        first = True
    else:
        t0 = max(k.start, t, until or 0)
        loc, cur, inv = k.sc.point, t0, k.q0
        stops.append(SchedStop(DEPOT_START, loc, t0, t0, t0, 0, inv, sc=k.sc))
        first = False

    last_tr = 0  # время в пути последнего переезда из arrival (с пробками — по часу выезда)

    def leg(i: int, j: int, at: int) -> int:
        return ctx.tt(i, j, at) if traffic else tau[i][j]

    def arrival(key: tuple, dest: int) -> int:
        nonlocal last_tr
        if first:
            base = cur if key == A.first_key else max(cur, t)
            last_tr = leg(loc, dest, base)
            arr = base + last_tr
            if until is not None:
                arr = max(arr, until + leg(k.sc.point, dest, until))
            return arr
        last_tr = leg(loc, dest, cur)
        return cur + last_tr

    def take_break() -> bool:
        nonlocal cur, first
        arr = (cur if (BREAK,) == A.first_key else max(cur, t)) if first else cur
        first = False
        st = max(arr, k.bf, t, until or 0)
        if st > bt and not relaxed:
            return False
        en = st + k.beta
        stops.append(SchedStop(BREAK, loc, arr, st, en, 0, inv))
        cur = en
        return True

    if brk == -1 and not take_break():
        return None

    for j, o in enumerate(seq):
        d = o.dem
        if not relaxed and (inv[0] < d[0] or inv[1] < d[1] or inv[2] < d[2]):
            cap = k.cap
            if cap[0] < d[0] or cap[1] < d[1] or cap[2] < d[2]:
                return None
            best = None
            for sc in ctx.scs:
                arr = arrival((RESTOCK, sc.sc_id), sc.point)
                to_sc = last_tr
                st = max(arr, sc.open, t)
                en = st + sc.rho
                if en > sc.close:
                    continue
                from_sc = leg(sc.point, o.point, en)
                if max(en + from_sc, o.a) > o.b:
                    continue
                detour = to_sc + from_sc
                if best is None or detour < best[0]:
                    best = (detour, sc, arr, st, en, to_sc)
            if best is None:
                return None
            _, sc, arr, st, en, to_sc = best
            first = False
            inv = cap
            stops.append(SchedStop(RESTOCK, sc.point, arr, st, en, to_sc, inv, sc=sc))
            travel += to_sc
            cur, loc = en, sc.point
        arr = arrival((VISIT, o.order_id), o.point)
        tr = last_tr
        first = False
        st = max(arr, o.a, t)
        if st > o.b and not relaxed:
            return None
        en = st + o.p
        inv = (inv[0] - d[0], inv[1] - d[1], inv[2] - d[2])
        stops.append(SchedStop(VISIT, o.point, arr, st, en, tr, inv, order=o))
        starts.append(st)
        ends.append(en)
        travel += tr
        cur, loc = en, o.point
        if brk == j and not take_break():
            return None

    need_break = A.needs_break(seq)
    if A.needs_depot_end(seq, need_break):
        arr = arrival((DEPOT_END,), k.sc.point)
        tr = last_tr
        if arr > k.end and not relaxed:
            return None
        at = max(arr, k.end, t)
        stops.append(SchedStop(DEPOT_END, k.sc.point, at, at, at, tr, inv, sc=k.sc))
        travel += tr
    return TailSched(stops=stops, travel=travel, starts=starts, ends=ends)


def decode_tail(ctx: DayContext, A: Anchor, seq: list[OrderCtx], t: int) -> TailSched | None:
    """Точное расписание хвоста для порядка seq или None."""
    if not A.available:
        return TailSched([], 0, [], []) if not seq else None
    if not A.needs_break(seq):
        res = _pass(ctx, A, seq, None, t)
    else:
        base = _pass(ctx, A, seq, None, t)
        if base is None:
            return None
        start0 = max(A.en, t) if A.prefix else max(A.k.start, t, A.until or 0)
        ends = [start0] + base.ends
        res = None
        k = A.k
        slack = break_slack_sec()
        limits = [k.bt - slack, k.bt] if slack > 0 and k.bt - slack >= max(k.bf, t) else [k.bt]
        for limit in limits:
            for j in range(len(seq) - 1, -2, -1):
                if ends[j + 1] > limit:
                    continue
                res = _pass(ctx, A, seq, j, t, bt_limit=limit)
                if res is not None:
                    break
            if res is not None:
                break
    if res is None:
        return None
    if not A.prefix and len(res.stops) >= 2 and res.stops[1].kind == VISIT:
        # DEPOT_START — к выезду на первый визит (как decoder._finalize)
        res.travel += shift_departure(ctx, res.stops[0], res.stops[1])
    return res


def forecast(ctx: DayContext, A: Anchor, seq: list[OrderCtx], t: int) -> dict[str, int]:
    """Прогноз начала визитов «без вмешательства»: прежний порядок и позиция перерыва,
    без верхних границ окон и без пополнений."""
    if not A.available:
        return {}
    brk = None
    if A.needs_break(seq):
        before = A.prev_break_before
        if before is None:
            brk = -1
        else:
            brk = sum(1 for o in seq if o.order_id in before) - 1
    res = _pass(ctx, A, seq, brk, t, relaxed=True)
    if res is None:
        return {}
    return {o.order_id: st for o, st in zip(seq, res.starts)}


# ---------------------------------------------------------------- планировщик события

class EventPlanner:
    def __init__(self, ref: ReferenceState, matrix: MatrixState, day: DayState, event: Event,
                 traffic: bool | None = None, joint: bool | None = None) -> None:
        self.started = time.perf_counter()
        self.deadline = self.started + event_deadline_sec()
        self.ref, self.matrix, self.day, self.event = ref, matrix, day, event
        assert day.plan is not None
        self.prev: Plan = day.plan
        if event.type == "NEW_ORDER":
            day.orders[event.order.order_id] = event.order
            day.arrived_at[event.order.order_id] = event.timestamp
        self.ctx = DayContext(ref, matrix, day, list(day.orders.values()), event.timestamp, traffic=traffic,
                              joint=joint)
        self.t = self.ctx.to_sec(event.timestamp)
        # совместные наряды (planner/joint.py): main_id -> закреплённый основной
        self.pin: dict[str, OrderCtx] = {}
        self.joint_fail: dict[str, str] = {}
        self.joint_done: set[str] = set()
        self._pairs: dict[str, tuple[str, str]] = {}
        self._joint_init()
        self.ts = self.ctx.to_dt(self.t)
        self.message: list[str] = []
        self.stats: dict = {}

        # учёт: где наряды прежней версии
        self.prev_staff: dict[str, str] = {}
        for r in self.prev.routes:
            for s in r.stops:
                if s.type == StopType.VISIT and s.order_id:
                    self.prev_staff[s.order_id] = r.staff_id
        self.unassigned: dict[str, UnassignedOrder] = {u.order_id: u.model_copy() for u in self.prev.unassigned}
        self.groups: list[PgpGroup] = [g.model_copy(update={"order_ids": list(g.order_ids)}) for g in self.prev.pgp_groups]
        self.alerts: list[Alert] = [a.model_copy() for a in self.prev.alerts]
        self.ids = assemble.AlertIds(day.date, self._max_alert_no())
        self.n_prev_alerts = len(self.alerts)
        self.pool_new: list[OrderCtx] = []
        self.unplaced: dict[str, tuple[OrderCtx, str | None, bool]] = {}
        self.budget_exhausted: set[str] = set()
        self.ejected_unplaced: set[str] = set()

        self.anchors: dict[str, Anchor] = {}
        force: set[tuple[str, str]] = set()
        if event.type in ("VISIT_DELAYED", "ORDER_TRANSFERRED"):
            force.add((event.staff_id, event.order_id))
            # напарник начал визит одновременно с основным — он тоже начат
            pid = jnt.partner_id(event.order_id)
            for r in self.prev.routes:
                if any(s.type == StopType.VISIT and s.order_id == pid for s in r.stops):
                    force.add((r.staff_id, pid))
        prev_routes = {r.staff_id: r for r in self.prev.routes}
        for k in self.ctx.staff:
            self.anchors[k.staff_id] = self._anchor(k, prev_routes.get(k.staff_id), force)
        if self.pin:
            for A in self.anchors.values():
                A.seq = [self.pin.get(o.order_id, o) for o in A.seq]

        self.late: dict[str, int] = {}  # прогноз опоздания: order_id -> ŝ
        self.displaced: list[tuple[OrderCtx, str, bool]] = []  # (наряд, прежний сотрудник, выбыл ли)
        self.inserted_from_unassigned: set[str] = set()
        self.unav_staff: str | None = None

    # ------------------------------------------------------------ совместные наряды
    def _joint_init(self) -> None:
        """Пары из прежней версии: визит напарника ORD-…-B получает OrderCtx с окном [s, s],
        основной наряд в хвосте — закреплённую копию (planner/joint.py)."""
        ctx = self.ctx
        for r in self.prev.routes:
            for s in r.stops:
                m = jnt.main_of(s.order_id, self.day.orders) if s.type == StopType.VISIT else None
                if m is None or m not in ctx.order_by_id:
                    continue
                st = ctx.to_sec(s.start)
                main = ctx.order_by_id[m]
                ctx.order_by_id[s.order_id] = jnt.partner_ctx(main, st)
                self.pin[m] = jnt.pinned(main, st)
        # пары прежней версии сопровождаются и при выключенном режиме (новые не создаются)
        self.joint = ctx.joint or bool(self.pin)

    def _drop_partner(self, main_id: str) -> None:
        """Снять не начатый визит напарника (основной снят или отменён)."""
        pid = jnt.partner_id(main_id)
        hit = self.find_visit(pid)
        if hit is None:
            return
        A, frozen = hit
        if frozen:
            return
        A.seq = [o for o in A.seq if o.order_id != pid]
        self.touch(A)
        self.message.append(f"напарник {pid} снят у {A.k.staff_id}")

    def joint_place(self, main: OrderCtx, src: str | None, unav: bool) -> str | None:
        """Пара (основной, напарник, общее начало) по минимальному приросту пути; иначе unplaced."""
        m = main.order_id
        if m in self.day.cancelled:
            return None

        def try_insert(kidx: int, x: OrderCtx):
            A = self.anchors[self.ctx.staff[kidx].staff_id]
            r = self.insertion(A, x, max_tries=4)
            return None if r is None else (r[0], (A, r[1], r[2]))

        found = None
        if self.time_left() and self.ctx.joint:
            found = jnt.search(main, jnt.start_grid(main, self.t), main.eligible, try_insert, deadline=self.deadline)
        if found is None:
            n = sum(1 for i in main.eligible if self.anchors[self.ctx.staff[i].staff_id].available)
            self.joint_fail[m] = jnt.unassigned_text(main, n)[1]
            self.unplaced[m] = (main, src, unav)
            return None
        _, _, (A1, sch1, seq1), pm, _, (A2, sch2, seq2), pp = found
        A1.seq, A1.sched, A1.sched_ok, A1.touched = seq1, sch1, True, True
        A2.seq, A2.sched, A2.sched_ok, A2.touched = seq2, sch2, True, True
        self.pin[m] = pm
        self.ctx.order_by_id[pp.order_id] = pp
        self.message.append(f"совместный {m}: {A1.k.staff_id} + {A2.k.staff_id} в {self.ctx.to_dt(pm.a):%H:%M}")
        return A1.k.staff_id

    def joint_redo(self, o: OrderCtx, src: str | None, unav: bool) -> None:
        """Снят основной визит или визит напарника: пересобрать пару."""
        m = jnt.main_of(o.order_id) or o.order_id
        if m in self.joint_done or m not in self.ctx.order_by_id:
            return
        self.joint_done.add(m)
        main = self.ctx.order_by_id[m]
        if o.order_id != m:
            hit = self.find_visit(m)
            if hit is None or hit[1]:
                return  # основного нет в маршруте (отменён) или он уже начат
            A = hit[0]
            # другой напарник к тому же началу
            if self.time_left() and self.place(o, exclude=A.k.staff_id) is not None:
                return
            A.seq = [x for x in A.seq if x.order_id != m]
            self.touch(A)
            sched, removed = self.repair(A)
            A.sched, A.sched_ok = sched, True
            for x in removed:
                if self.place(x) is None:
                    self.unplaced[x.order_id] = (x, A.k.staff_id, False)
            src = A.k.staff_id
        else:
            self._drop_partner(m)
        self.joint_place(main, src, unav)

    def _joint_pairs_now(self) -> dict[str, tuple[str, str]]:
        main_staff: dict[str, str] = {}
        partner_staff: dict[str, str] = {}
        for A in self.anchors.values():
            ids = [s.order_id for s in A.prefix if s.type == StopType.VISIT and s.order_id]
            ids += [o.order_id for o in A.seq]
            for oid in ids:
                m = jnt.main_of(oid, self.day.orders)
                if m is not None:
                    partner_staff[m] = A.k.staff_id
                else:
                    main_staff[oid] = A.k.staff_id
        return {m: (main_staff[m], p) for m, p in partner_staff.items() if m in main_staff}

    # ------------------------------------------------------------ helpers
    def _max_alert_no(self) -> int:
        n = 0
        for a in self.prev.alerts:
            try:
                n = max(n, int(a.alert_id.rsplit("-", 1)[1]))
            except (IndexError, ValueError):
                pass
        return n

    def time_left(self) -> bool:
        return time.perf_counter() < self.deadline

    def _anchor(self, k: StaffCtx, route: Route | None, force: set[tuple[str, str]]) -> Anchor:
        ctx, t = self.ctx, self.t
        stops = route.stops if route is not None else []
        n_frozen = 0
        for i, s in enumerate(stops):
            if ctx.to_sec(s.start) < t:
                n_frozen = i + 1
        for i, s in enumerate(stops):
            if (s.type == StopType.VISIT and (k.staff_id, s.order_id) in force
                    and ctx.to_sec(s.start) <= t):
                n_frozen = max(n_frozen, i + 1)
        prefix = [s.model_copy() for s in stops[:n_frozen]]
        tail = stops[n_frozen:]
        inv = k.q0
        break_done = False
        visits = 0
        for s in prefix:
            if s.type == StopType.VISIT and s.order_id in self.day.orders:
                d = self.day.orders[s.order_id].required_equipment
                inv = (inv[0] - d.ONT, inv[1] - d.ROUTER, inv[2] - d.STB)
                visits += 1
            elif s.type == StopType.VISIT and jnt.main_of(s.order_id, self.day.orders) is not None:
                visits += 1  # визит напарника: АО не списывается, но перерыв нужен (H3)
            elif s.type == StopType.RESTOCK:
                inv = k.cap
            elif s.type == StopType.BREAK:
                break_done = True
        if prefix:
            loc = ctx.point_of[prefix[-1].location_id]
            en = ctx.to_sec(prefix[-1].end)
        else:
            loc, en = k.sc.point, k.start
        seq = [ctx.order_by_id[s.order_id] for s in tail if s.type == StopType.VISIT and s.order_id in ctx.order_by_id]
        before: set[str] | None = None
        if not break_done:
            acc: set[str] = set()
            for s in tail:
                if s.type == StopType.BREAK:
                    before = set(acc)
                    break
                if s.type == StopType.VISIT and s.order_id:
                    acc.add(s.order_id)
        A = Anchor(
            k=k,
            prev_route=route,
            prefix=prefix,
            loc=loc,
            en=en,
            inv=inv,
            break_done=break_done,
            prefix_visits=visits,
            first_key=_key(tail[0]) if (tail and prefix) else None,
            status=route.status if route is not None else RouteStatus.ACTIVE,
            seq=seq,
            prev_seq_ids=[o.order_id for o in seq],
            prev_break_before=before,
        )
        if route is not None and route.status == RouteStatus.UNAVAILABLE:
            A.unav_from = ctx.to_sec(route.unavailable_from) if route.unavailable_from else None
            if route.unavailable_until is None:
                A.blocked = True
            else:
                u = ctx.to_sec(route.unavailable_until)
                A.unav_until = u
                if u > t:
                    A.until = u
                if u >= k.end:
                    A.blocked = True
        if prefix and prefix[-1].type == StopType.DEPOT_END:
            A.closed = True
        return A

    def sched(self, A: Anchor) -> TailSched | None:
        if A.sched_ok is None:
            A.sched = decode_tail(self.ctx, A, A.seq, self.t)
            A.sched_ok = A.sched is not None
        return A.sched

    def touch(self, A: Anchor) -> None:
        A.touched = True
        A.sched_ok = None

    def find_visit(self, order_id: str) -> tuple[Anchor, bool] | None:
        """(сотрудник, в префиксе ли) для наряда в маршруте."""
        for A in self.anchors.values():
            if any(s.type == StopType.VISIT and s.order_id == order_id for s in A.prefix):
                return A, True
            if any(o.order_id == order_id for o in A.seq):
                return A, False
        return None

    def resolve_alerts(self, order_id: str | None = None, group_id: str | None = None) -> None:
        for a in self.alerts:
            if a.status != AlertStatus.ACTIVE:
                continue
            if (order_id and a.order_id == order_id) or (group_id and a.group_id == group_id):
                a.status = AlertStatus.RESOLVED
                a.resolved_at = self.ts

    def add_alert(self, alert: Alert) -> None:
        self.alerts.append(alert)

    # ------------------------------------------------------------ шаг 1: событие
    def apply(self) -> None:
        ev = self.event
        kind = ev.type
        if kind == "NEW_ORDER":
            o = self.ctx.order_by_id[ev.order.order_id]
            if not self.pgp_new_order(o):
                self.pool_new = [o]
            return
        if kind == "ORDER_CANCELLED":
            self.cancel(ev.order_id)
            return
        if kind in ("VISIT_DELAYED", "ORDER_TRANSFERRED"):
            self.visit_end(ev)
            return
        if kind == "STAFF_UNAVAILABLE":
            self.unavailable(ev)
            return
        # TIME_TICK: план исполняется в точности, прогнозы не меняются

    def cancel(self, oid: str) -> None:
        cancelled = CancelledOrder(order_id=oid, cancelled_at=self.ts, event_id=self.event.event_id)
        hit = self.find_visit(oid)
        if hit is not None:
            A, frozen = hit
            if frozen:
                self.message.append(f"Визит по {oid} уже начат — остаётся в маршруте как выполненный")
                return
            A.seq = [o for o in A.seq if o.order_id != oid]
            self.touch(A)
            self.day.cancelled[oid] = cancelled
            self.message.append(f"{oid} отменён, маршрут {A.k.staff_id} пересчитан")
            if self.joint:
                self._drop_partner(oid)
            return
        self.day.cancelled[oid] = cancelled
        if oid in self.unassigned:
            del self.unassigned[oid]
            self.resolve_alerts(order_id=oid)
        for g in list(self.groups):
            if oid in g.order_ids:
                g.order_ids = [x for x in g.order_ids if x != oid]
                if len(g.order_ids) >= 2:
                    g.comment = self._pgp_comment(g)
                else:
                    # группа из одного наряда невозможна (minItems 2): остаток — снова в распределение
                    self.groups.remove(g)
                    self.resolve_alerts(group_id=g.group_id)
                    for x in g.order_ids:
                        self.pool_new = self.pool_new + [self.ctx.order_by_id[x]]
        self.message.append(f"{oid} отменён")

    def visit_end(self, ev) -> None:
        A = self.anchors.get(ev.staff_id)
        idx = None
        if A is not None:
            for i, s in enumerate(A.prefix):
                if s.type == StopType.VISIT and s.order_id == ev.order_id:
                    idx = i
        if idx is None:
            raise ApiError(422, "ORDER_NOT_IN_ROUTE",
                           f"Визит по {ev.order_id} не начат у сотрудника {ev.staff_id}",
                           [{"field": "order_id", "message": "визит не начат у этого сотрудника"}])
        stop = A.prefix[idx]
        new_end = ev.expected_end if ev.type == "VISIT_DELAYED" else ev.actual_end
        new_end_sec = self.ctx.to_sec(new_end)
        if new_end_sec < self.ctx.to_sec(stop.start):
            raise ApiError(422, "INVALID_VISIT_END", "Новый конец визита раньше его начала",
                           [{"field": "expected_end" if ev.type == "VISIT_DELAYED" else "actual_end",
                             "message": "раньше начала визита"}])
        if idx != len(A.prefix) - 1 and new_end_sec > self.ctx.to_sec(A.prefix[idx + 1].start):
            raise ApiError(422, "EVENT_CONFLICTS_PLAN",
                           f"Следующая остановка {A.k.staff_id} уже начата до нового конца визита {ev.order_id}",
                           [{"field": "order_id", "message": "следующая остановка уже начата"}])
        A.prefix[idx] = stop.model_copy(update={"end": self.ctx.to_dt(new_end_sec)})
        self.day.visit_end_overrides[ev.order_id] = self.ctx.to_dt(new_end_sec)
        if idx == len(A.prefix) - 1:
            A.en = new_end_sec
        self.touch(A)
        what = "задерживается до" if ev.type == "VISIT_DELAYED" else "передан, визит окончен в"
        self.message.append(f"{ev.order_id} у {A.k.staff_id} {what} {self.ctx.to_dt(new_end_sec):%H:%M}")
        if self.joint:
            self._partner_end(ev.order_id, new_end_sec)

    def _partner_end(self, main_id: str, new_end_sec: int) -> None:
        """Напарник заканчивает вместе с основным: тот же новый конец, хвост пересчитывается."""
        pid = jnt.partner_id(main_id)
        for B in self.anchors.values():
            for i, s in enumerate(B.prefix):
                if s.type != StopType.VISIT or s.order_id != pid:
                    continue
                if i != len(B.prefix) - 1 and new_end_sec > self.ctx.to_sec(B.prefix[i + 1].start):
                    self.message.append(f"напарник {B.k.staff_id} уже начал следующую остановку — уходит по плану")
                    return
                old, old_en = B.prefix[i], B.en
                B.prefix[i] = s.model_copy(update={"end": self.ctx.to_dt(new_end_sec)})
                if i == len(B.prefix) - 1:
                    B.en = new_end_sec
                if decode_tail(self.ctx, B, [], self.t) is None:
                    # с продлением у напарника не остаётся допустимого хвоста (перерыв/конец смены):
                    # он уходит по плану, основной заканчивает один
                    B.prefix[i], B.en = old, old_en
                    self.message.append(f"напарник {B.k.staff_id} уходит по плану: продление нарушило бы его смену")
                    return
                self.day.visit_end_overrides[pid] = self.ctx.to_dt(new_end_sec)
                self.touch(B)
                self.message.append(f"напарник {B.k.staff_id} остаётся до {self.ctx.to_dt(new_end_sec):%H:%M}")
                return

    def unavailable(self, ev) -> None:
        A = self.anchors.get(ev.staff_id)
        if A is None:
            raise ApiError(422, "STAFF_NOT_ON_SHIFT", f"Сотрудник {ev.staff_id} не на смене в этот день",
                           [{"field": "staff_id", "message": "сотрудник не на смене"}])
        A.status = RouteStatus.UNAVAILABLE
        A.unav_from = self.t
        A.until = A.unav_until = None
        A.blocked = ev.until is None
        if ev.until is not None:
            u = self.ctx.to_sec(ev.until)
            A.until = A.unav_until = u
            if u >= A.k.end:
                A.blocked = True
        self.unav_staff = A.k.staff_id
        self.touch(A)

    # ------------------------------------------------------------ ПГП
    def _reg(self, oid: str) -> dt.datetime:
        return self.day.orders[oid].registered_at

    def pgp_new_order(self, o: OrderCtx) -> bool:
        """ПГП при NEW_ORDER (formalization §6). True — наряд ушёл в группу."""
        if not o.work_type.pgp_relevant:
            return False
        node = o.model.access_node_id
        r = o.model.registered_at
        for g in self.groups:
            if g.access_node_id != node:
                continue
            last = max(self._reg(x) for x in g.order_ids)
            if dt.timedelta(0) <= r - last <= PGP_WINDOW or (r < last and last - r <= PGP_WINDOW):
                g.order_ids.append(o.order_id)
                g.comment = self._pgp_comment(g)
                self.message.append(f"{o.order_id} добавлен в группу ПГП {g.group_id}")
                return True
        window = [
            x for x in self.day.orders.values()
            if x.access_node_id == node
            and x.order_id not in self.day.cancelled
            and self.ref.work_types[x.work_type_code].pgp_relevant
            and r - PGP_WINDOW <= x.registered_at <= r
        ]
        if len(window) < PGP_MIN_ORDERS:
            return False
        in_groups = {x for g in self.groups for x in g.order_ids}
        members = []
        for x in sorted(window, key=lambda x: (x.registered_at, x.order_id)):
            if x.order_id in in_groups:
                continue
            hit = self.find_visit(x.order_id)
            if hit is not None and hit[1]:
                continue  # визит начат: остаётся в маршруте, но учитывается в пороге
            members.append(x.order_id)
        if len(members) < 2 or o.order_id not in members:
            return False
        gid = self._next_group_id()
        group = PgpGroup(group_id=gid, access_node_id=node, order_ids=members, detected_at=self.ts,
                         action=PgpAction.TRANSFER_TO_STATION)
        group.comment = self._pgp_comment(group)
        self.groups.append(group)
        for x in members:
            if x == o.order_id:
                continue
            hit = self.find_visit(x)
            if hit is not None:
                A = hit[0]
                A.seq = [y for y in A.seq if y.order_id != x]
                self.touch(A)
            if x in self.unassigned:
                del self.unassigned[x]
                self.resolve_alerts(order_id=x)
        self.add_alert(assemble.pgp_alert(self.ids, group, self.ts))
        self.message.append(f"Выявлена ПГП {gid} на {node}: {len(members)} наряда")
        return True

    def _next_group_id(self) -> str:
        n = 0
        for gid in [g.group_id for g in self.groups] + [a.group_id for a in self.alerts if a.group_id]:
            try:
                n = max(n, int(gid.rsplit("-", 1)[1]))
            except (IndexError, ValueError):
                pass
        return f"PGP-{self.day.date:%Y%m%d}-{n + 1:02d}"

    def _pgp_comment(self, g: PgpGroup) -> str:
        regs = [self._reg(x) for x in g.order_ids]
        span = int(round((max(regs) - min(regs)).total_seconds() / 60))
        node = self.ref.access_nodes.get(g.access_node_id)
        name = node.name if node is not None else g.access_node_id
        return f"{len(g.order_ids)} обращения с {name} за {span} мин"

    # ------------------------------------------------------------ шаги 2–3: прогноз и ремонт
    def forecast_and_repair(self) -> None:
        t = self.t
        if self.event.type == "TIME_TICK":
            # прогноз по всем маршрутам: при точном исполнении плана опозданий нет, но если
            # хвост перестал укладываться в окна (например, после сбоя бюджета), чиним сразу
            for A in self.anchors.values():
                if A.available and not A.touched and A.seq:
                    fc = forecast(self.ctx, A, A.seq, t)
                    if any(s > self.ctx.order_by_id[oid].b for oid, s in fc.items()):
                        self.touch(A)
            self.stats["tick_touched"] = sum(1 for A in self.anchors.values() if A.touched)
        for A in self.anchors.values():
            if not A.touched:
                continue
            for oid, s in forecast(self.ctx, A, A.seq, t).items():
                if s > self.ctx.order_by_id[oid].b:
                    self.late[oid] = s
            sched, removed = self.repair(A)
            A.sched, A.sched_ok = sched, True
            unav = A.status == RouteStatus.UNAVAILABLE and A.k.staff_id == self.unav_staff
            for o in removed:
                self.displaced.append((o, A.k.staff_id, unav))

    def _removal_key(self, o: OrderCtx) -> tuple:
        untimely = 0 if o.a - self.t >= TIMELY_SEC else 1
        return (o.weight, untimely, -o.a)

    def repair(self, A: Anchor) -> tuple[TailSched, list[OrderCtx]]:
        ctx, t = self.ctx, self.t
        seq = list(A.seq)
        removed: list[OrderCtx] = []
        if not A.available:
            A.seq = []
            return TailSched([], 0, [], []), seq
        while True:
            res = decode_tail(ctx, A, seq, t)
            if res is not None:
                A.seq = seq
                return res, removed
            # перестановка одного наряда внутри маршрута
            if len(seq) <= 14 and self.time_left():
                moved = None
                for i in range(len(seq)):
                    rest = seq[:i] + seq[i + 1:]
                    for j in range(len(seq)):
                        if j == i:
                            continue
                        cand = rest[:j] + [seq[i]] + rest[j:]
                        r2 = decode_tail(ctx, A, cand, t)
                        if r2 is not None and (moved is None or r2.travel < moved[0].travel):
                            moved = (r2, cand)
                if moved is not None:
                    A.seq = moved[1]
                    return moved[0], removed
            # снятие одного наряда
            best = None
            for i in range(len(seq)):
                cand = seq[:i] + seq[i + 1:]
                r2 = decode_tail(ctx, A, cand, t)
                if r2 is not None:
                    key = (self._removal_key(seq[i]), r2.travel)
                    if best is None or key < best[0]:
                        best = (key, i, r2, cand)
            if best is not None:
                removed.append(seq[best[1]])
                A.seq = best[3]
                return best[2], removed
            if not seq:
                # даже пустой хвост недопустим (например, задержка визита съела окно перерыва):
                # неустранимо, ставим перерыв как можно раньше и возвращаемся в СЦ
                A.seq = []
                brk = -1 if A.needs_break([]) else None
                res = _pass(ctx, A, [], brk, t, relaxed=True) or TailSched([], 0, [], [])
                self.stats["unavoidable"] = self.stats.get("unavoidable", 0) + 1
                self.message.append(f"неустранимо: {A.k.staff_id} — после изменения начатого визита "
                                    f"нельзя соблюсти перерыв или конец смены")
                return res, removed
            # одиночное снятие не помогло: снимаем наряд, на котором ломается префикс
            cut = len(seq) - 1
            for n in range(1, len(seq) + 1):
                if decode_tail(ctx, A, seq[:n], t) is None:
                    cut = n - 1
                    break
            removed.append(seq.pop(cut))

    # ------------------------------------------------------------ шаг 4: вставка
    def insertion(self, A: Anchor, o: OrderCtx, seq: list[OrderCtx] | None = None,
                  base: TailSched | None = None, max_tries: int = 6) -> tuple[int, TailSched, list[OrderCtx]] | None:
        if not A.available or self.t >= A.k.end:
            return None
        if seq is None:
            base = self.sched(A)
            if base is None:
                return None
            seq = A.seq
        assert base is not None
        tau, k, t = self.ctx.tau, A.k, self.t
        cands = []
        for pos in range(len(seq) + 1):
            if pos == 0:
                prev_pt = A.loc if A.prefix else k.sc.point
                prev_end = A.en if A.prefix else max(k.start, t, A.until or 0)
            else:
                prev_pt, prev_end = seq[pos - 1].point, base.ends[pos - 1]
            if max(prev_end, t) + tau[prev_pt][o.point] > o.b:
                break
            next_pt = seq[pos].point if pos < len(seq) else k.sc.point
            if pos < len(seq) and o.a + o.p + tau[o.point][next_pt] > seq[pos].b:
                continue
            delta = tau[prev_pt][o.point] + tau[o.point][next_pt] - tau[prev_pt][next_pt]
            cands.append((delta, pos))
        cands.sort()
        best = None
        tries = 0
        for _, pos in cands:
            cand = seq[:pos] + [o] + seq[pos:]
            res = decode_tail(self.ctx, A, cand, t)
            tries += 1
            if res is not None:
                cost = res.travel - base.travel
                if best is None or cost < best[0]:
                    best = (cost, res, cand)
            if (tries >= max_tries and best is not None) or tries >= 3 * max_tries:
                break
        return best

    def best_staff(self, o: OrderCtx, exclude: str | None = None) -> tuple[int, Anchor, TailSched, list[OrderCtx]] | None:
        best = None
        for kidx in o.eligible:
            A = self.anchors[self.ctx.staff[kidx].staff_id]
            if A.k.staff_id == exclude:
                continue
            res = self.insertion(A, o)
            if res is not None and (best is None or res[0] < best[0]):
                best = (res[0], A, res[1], res[2])
        return best

    def place(self, o: OrderCtx, exclude: str | None = None) -> str | None:
        best = self.best_staff(o, exclude)
        if best is None:
            return None
        _, A, sched, seq = best
        A.seq, A.sched, A.sched_ok, A.touched = seq, sched, True, True
        return A.k.staff_id

    def eject_place(self, o: OrderCtx) -> str | None:
        """Цепочка длины 2: o встаёт на место x (запас x ≥ 30 мин до слота), x переезжает
        к другому сотруднику или снимается, если его вес меньше."""
        if self.joint and jnt.is_joint_code(o.work_type.code):
            return None
        for kidx in o.eligible:
            if not self.time_left():
                return None
            A = self.anchors[self.ctx.staff[kidx].staff_id]
            if not A.available or self.sched(A) is None:
                continue
            for x in list(A.seq):
                if (x.weight > o.weight or x.a - self.t < TIMELY_SEC or abs(x.a - o.a) > EJECT_WINDOW_SEC
                        or x.order_id not in self.prev_staff
                        or (self.joint and jnt.is_joint_code(x.work_type.code))):
                    continue
                rest = [y for y in A.seq if y is not x]
                base = decode_tail(self.ctx, A, rest, self.t)
                if base is None:
                    continue
                ins = self.insertion(A, o, rest, base, max_tries=3)
                if ins is None:
                    continue
                moved = self.best_staff(x, exclude=A.k.staff_id)
                if moved is None and x.weight >= o.weight:
                    continue
                A.seq, A.sched, A.sched_ok, A.touched = ins[2], ins[1], True, True
                if moved is not None:
                    _, B, sched_b, seq_b = moved
                    B.seq, B.sched, B.sched_ok, B.touched = seq_b, sched_b, True, True
                else:
                    self.displaced.append((x, A.k.staff_id, False))
                    self.ejected_unplaced.add(x.order_id)
                return A.k.staff_id
        return None

    def insert_all(self) -> None:
        disp = sorted(self.displaced, key=lambda d: (-d[0].weight, len(d[0].eligible), d[0].a, d[0].order_id))
        for o, src, unav in disp:
            if self.joint and jnt.is_joint_code(o.work_type.code):
                self.joint_redo(o, src, unav)
                continue
            if not self.time_left():
                self.unplaced[o.order_id] = (o, src, unav)
                self.budget_exhausted.add(o.order_id)
                continue
            if self.place(o) is None and self.eject_place(o) is None:
                self.unplaced[o.order_id] = (o, src, unav)
        # снятые при выталкивании — ещё одна попытка
        for oid in list(self.ejected_unplaced):
            entry = next((d for d in self.displaced if d[0].order_id == oid), None)
            if entry is not None and self.time_left() and self.place(entry[0]) is None:
                self.unplaced[oid] = entry
        for o in self.pool_new:
            if not self.time_left():
                self.unplaced[o.order_id] = (o, None, False)
                self.budget_exhausted.add(o.order_id)
                continue
            if self.joint and jnt.is_joint(o):
                sid = self.joint_place(o, None, False)
            else:
                sid = self.place(o)
                if sid is None:
                    self.unplaced[o.order_id] = (o, None, False)
            if sid is None:
                self.message.append(f"{o.order_id} не распределён")
            else:
                self.message.append(f"{o.order_id} назначен {sid}")
        # освободившееся время: нераспределённые — в затронутые маршруты
        freed = [A for A in self.anchors.values() if A.touched and A.available]
        if freed:
            ids = {A.k.idx for A in freed}
            pool = [self.ctx.order_by_id[oid] for oid in self.unassigned if oid in self.ctx.order_by_id]
            pool = [o for o in pool if o.b >= self.t and ids.intersection(o.eligible)
                    and not (self.joint and jnt.is_joint(o))]
            pool.sort(key=lambda o: (-o.weight, len(o.eligible), o.a, o.order_id))
            for o in pool:
                if not self.time_left():
                    break
                best = None
                for A in freed:
                    if A.k.idx not in o.eligible:
                        continue
                    res = self.insertion(A, o, max_tries=3)
                    if res is not None and (best is None or res[0] < best[0]):
                        best = (res[0], A, res[1], res[2])
                if best is not None:
                    _, A, sched, seq = best
                    A.seq, A.sched, A.sched_ok = seq, sched, True
                    self.inserted_from_unassigned.add(o.order_id)
        self.stats["unassigned_inserted"] = len(self.inserted_from_unassigned)
        if self.inserted_from_unassigned:
            self.message.append(f"в освободившееся время вставлено нераспределённых: {len(self.inserted_from_unassigned)}")
        placed = len(self.displaced) - len([d for d in self.displaced if d[0].order_id in self.unplaced])
        if self.displaced:
            self.message.append(f"снято с маршрутов {len(self.displaced)}, передано другим {placed}")

    # ------------------------------------------------------------ unassigned
    def classify(self, o: OrderCtx, unav: bool, exhausted: bool) -> UnassignedOrder:
        ctx, t = self.ctx, self.t
        slot = assemble.proposed_slot(ctx, o)
        action = UnassignedAction.RESCHEDULE if slot is not None else UnassignedAction.ESCALATE
        window = f"{ctx.to_dt(o.a):%H:%M}–{ctx.to_dt(o.b):%H:%M}"
        if unav:
            return UnassignedOrder(order_id=o.order_id, reason_code=ReasonCode.STAFF_UNAVAILABLE,
                                   reason_text=f"Исполнитель {self.unav_staff} выбыл, замену в окне {window} найти не удалось",
                                   suggested_action=action, proposed_slot=slot, since=self.ts)
        if exhausted:
            return UnassignedOrder(order_id=o.order_id, reason_code=ReasonCode.OTHER,
                                   reason_text="Исчерпан бюджет расчёта события до вставки наряда",
                                   suggested_action=UnassignedAction.ESCALATE, since=self.ts)
        staff = [self.anchors[ctx.staff[i].staff_id] for i in o.eligible]
        staff = [A for A in staff if A.available]
        if self.joint and o.order_id in self.joint_fail:
            if len(staff) < 2:
                return UnassignedOrder(order_id=o.order_id, reason_code=ReasonCode.NO_QUALIFIED_STAFF,
                                       reason_text=self.joint_fail[o.order_id],
                                       suggested_action=UnassignedAction.REASSIGN, since=self.ts)
            return UnassignedOrder(order_id=o.order_id, reason_code=ReasonCode.NO_SHIFT_TIME,
                                   reason_text=self.joint_fail[o.order_id],
                                   suggested_action=action, proposed_slot=slot, since=self.ts)
        if not staff:
            need = f"профилем {'/'.join(o.work_type.profiles)}, технологией {o.model.technology}"
            if o.model.requires_height:
                need += " и допуском к работам на высоте"
            return UnassignedOrder(order_id=o.order_id, reason_code=ReasonCode.NO_QUALIFIED_STAFF,
                                   reason_text=f"На смене нет доступного сотрудника с {need}",
                                   suggested_action=UnassignedAction.REASSIGN, since=self.ts)
        reachable = False
        for A in staff:
            loc = A.loc if A.prefix else A.k.sc.point
            free = max(A.en, t, A.until or 0) if A.prefix else max(A.k.start, t, A.until or 0)
            st = max(free + ctx.tt(loc, o.point, free), o.a)
            if st <= o.b and st + o.p + ctx.tt(o.point, A.k.sc.point, st + o.p) <= A.k.end:
                reachable = True
                break
        if not reachable:
            return UnassignedOrder(order_id=o.order_id, reason_code=ReasonCode.SLOT_UNREACHABLE,
                                   reason_text=f"Ни один из {len(staff)} допустимых сотрудников не успевает начать визит в окне {window}",
                                   suggested_action=action, proposed_slot=slot, since=self.ts)
        return UnassignedOrder(order_id=o.order_id, reason_code=ReasonCode.NO_SHIFT_TIME,
                               reason_text=(f"У {len(staff)} допустимых сотрудников не хватает времени: визит в окне "
                                            f"{window} не встаёт без срыва уже назначенных визитов"),
                               suggested_action=action, proposed_slot=slot, since=self.ts)

    # ------------------------------------------------------------ сборка
    def route_of(self, A: Anchor) -> Route:
        prev = A.prev_route
        if not A.touched and prev is not None and A.status == prev.status:
            r = prev.model_copy(update={"stops": [s.model_copy() for s in prev.stops]})
            return r
        ctx = self.ctx
        sched = A.sched if A.sched_ok else decode_tail(ctx, A, A.seq, self.t)
        if sched is None:  # не должно случаться: ремонт оставляет допустимый хвост
            sched = TailSched([], 0, [], [])
        stops: list[Stop] = [s.model_copy() for s in A.prefix]
        prev_point = A.loc if A.prefix else None
        for s in sched.stops:
            dist = 0 if (prev_point is None or s.kind == BREAK) else ctx.dist[prev_point][s.point]
            o = s.order
            expl = None
            if o is not None:
                expl = (f"{A.k.staff_id} ({'/'.join(A.k.model.profiles)}, {o.model.technology}); "
                        f"в пути {round(s.travel / 60)} мин; запас до крайнего начала {max(0, (o.b - s.st) // 60)} мин; "
                        f"перепланировано в {self.ts:%H:%M}")
                if self._pairs:
                    expl = "; ".join([expl] + assemble.joint_note(self._pairs, o.order_id))
            stops.append(Stop(
                seq=1, type=StopType(s.kind), location_id=ctx.point_ids[s.point],
                order_id=o.order_id if o is not None else None,
                arrival=ctx.to_dt(s.arr), start=ctx.to_dt(s.st), end=ctx.to_dt(s.en),
                travel_sec=s.travel, distance_m=dist, status=StopStatus.PLANNED,
                equipment_after=Equipment(ONT=max(0, s.inv[0]), ROUTER=max(0, s.inv[1]), STB=max(0, s.inv[2])),
                explanation=expl,
            ))
            prev_point = s.point
        if not any(s.type == StopType.VISIT for s in stops) and not A.prefix and A.status == RouteStatus.ACTIVE:
            stops = []
        for i, s in enumerate(stops, start=1):
            s.seq = i
        travel = sum(s.travel_sec or 0 for s in stops)
        work = sum((s.end - s.start).total_seconds() for s in stops if s.type == StopType.VISIT)
        visits = sum(1 for s in stops if s.type == StopType.VISIT)
        totals = RouteTotals(
            visits=visits,
            travel_sec=travel if visits else 0,
            distance_m=sum(s.distance_m or 0 for s in stops),
            work_min=int(work // 60),
            utilization=round((work + travel) / A.k.net_shift, 4) if visits else 0.0,
        )
        unav_from = unav_until = None
        if A.status == RouteStatus.UNAVAILABLE:
            if prev is not None and prev.status == RouteStatus.UNAVAILABLE and A.k.staff_id != self.unav_staff:
                unav_from, unav_until = prev.unavailable_from, prev.unavailable_until
            else:
                unav_from = self.ts
                unav_until = getattr(self.event, "until", None)
        return Route(staff_id=A.k.staff_id, service_center_id=A.k.sc.sc_id, status=A.status,
                     unavailable_from=unav_from, unavailable_until=unav_until, stops=stops, totals=totals)

    def assemble(self) -> Plan:
        self._restore()
        self._pairs = self._joint_pairs_now() if self.joint else {}
        routes = [self.route_of(self.anchors[k.staff_id]) for k in self.ctx.staff]
        new_staff: dict[str, str] = {}
        for r in routes:
            for s in r.stops:
                if s.type == StopType.VISIT and s.order_id:
                    new_staff[s.order_id] = r.staff_id

        # нераспределённые: вставленные — из списка, снятые и новые — в список
        for oid in list(self.unassigned):
            if oid in new_staff:
                del self.unassigned[oid]
                self.resolve_alerts(order_id=oid)
        pgp_ids = {x for g in self.groups for x in g.order_ids}
        for oid, (o, src, unav) in self.unplaced.items():
            if oid in new_staff or oid in pgp_ids or oid in self.day.cancelled:
                continue
            if jnt.main_of(oid, self.day.orders) is not None:
                continue  # визит напарника — не наряд дня, в unassigned не попадает
            u = self.classify(o, unav, oid in self.budget_exhausted)
            self.unassigned[oid] = u
            if src is None:
                self.add_alert(assemble.unassigned_alert(self.ids, u, self.ts))

        # LATE_RISK: каждый наряд, сменивший исполнителя или снятый из маршрута, и прогноз опоздания
        for oid, p in self.prev_staff.items():
            if oid in self.day.cancelled or jnt.main_of(oid, self.day.orders) is not None:
                continue
            q = new_staff.get(oid)
            if q == p and oid not in self.late:
                continue
            o = self.ctx.order_by_id.get(oid)
            if o is None:
                continue
            f = self.late.get(oid)
            if q == p:
                # прогноз опоздания, устранённый у того же исполнителя: предупреждаем, только если
                # наряд переставлен в маршруте (иначе его спасло снятие соседа — оно и предупреждено)
                if self.find_frozen(oid) or not self._reordered(p, oid):
                    continue
                self.resolve_alerts(order_id=oid)
                self.add_alert(self.late_alert(o, p, f, AlertAction.NONE, None, None, True,
                                               f"Прогноз опоздания {oid} у {p}; устранено перестановкой в маршруте"))
            elif q is not None:
                self.resolve_alerts(order_id=oid)
                reason = self._why(p)
                self.add_alert(self.late_alert(o, p, f, AlertAction.REASSIGN, q, None, True,
                                               f"{reason}: {oid} передан {q}"))
            elif oid in pgp_ids:
                self.add_alert(self.late_alert(o, p, f, AlertAction.TRANSFER_TO_STATION, None, None, True,
                                               f"{oid} снят из маршрута {p}: группа ПГП, передан в станционное подразделение"))
            else:
                u = self.unassigned.get(oid)
                slot = u.proposed_slot if u is not None else None
                action = AlertAction.RESCHEDULE if slot is not None else AlertAction.NONE
                reason = self._why(p)
                self.add_alert(self.late_alert(o, p, f, action, None, slot, True,
                                               f"{reason}: {oid} снят из маршрута, замены нет"
                                               + (f" — предложен перенос на {slot.start:%d.%m %H:%M}" if slot else "")))
        cancelled = list(self.day.cancelled.values())
        unassigned = list(self.unassigned.values())
        plan = Plan(date=self.day.date, plan_version=self.prev.plan_version, based_on_event_id=self.event.event_id,
                    as_of=self.ts, routes=routes, unassigned=unassigned, pgp_groups=self.groups,
                    cancelled=cancelled, alerts=self.alerts)
        return plan

    def _reordered(self, staff_id: str, oid: str) -> bool:
        A = self.anchors[staff_id]
        now_ids = [o.order_id for o in A.seq]
        common = set(now_ids) & set(A.prev_seq_ids)
        before = [x for x in A.prev_seq_ids if x in common]
        after = [x for x in now_ids if x in common]
        return oid in common and before.index(oid) != after.index(oid)

    def find_frozen(self, oid: str) -> bool:
        hit = self.find_visit(oid)
        return hit is not None and hit[1]

    def _why(self, staff_id: str) -> str:
        ev = self.event
        if ev.type == "STAFF_UNAVAILABLE" and ev.staff_id == staff_id:
            return f"{staff_id} выбыл"
        if ev.type == "VISIT_DELAYED" and ev.staff_id == staff_id:
            return f"{staff_id} задерживается на {ev.order_id}"
        return f"Перепланирование маршрута {staff_id} ({ev.type})"

    def late_alert(self, o: OrderCtx, staff_id: str, f: int | None, action: AlertAction, new_staff: str | None,
                   slot, applied: bool, message: str) -> Alert:
        resolved = action in (AlertAction.REASSIGN, AlertAction.NONE, AlertAction.TRANSFER_TO_STATION) and applied
        return Alert(
            alert_id=self.ids.next(),
            created_at=self.ts,
            type=AlertType.LATE_RISK,
            status=AlertStatus.RESOLVED if resolved else AlertStatus.ACTIVE,
            resolved_at=self.ts if resolved else None,
            order_id=o.order_id,
            staff_id=staff_id,
            forecast_start=self.ctx.to_dt(f) if f is not None else None,
            lateness_min=math.ceil((f - o.b) / 60) if f is not None and f > o.b else None,
            suggested_action=action,
            suggested_staff_id=new_staff,
            proposed_slot=slot,
            applied=applied,
            message=message,
        )

    # ------------------------------------------------------------ самопроверка
    def violations(self, plan: Plan) -> list:
        viol = validate_plan(plan, self.ref, self.matrix, self.day, joint=self.joint)
        viol += validate_transition(self.prev, plan, self.ref, self.matrix, self.day, self.ts, self.event)
        # нарушения, которые уже были в прежней версии (например, задержка за конец смены), не наши
        prev_viol = {(v.code, v.order_id, v.staff_id)
                     for v in validate_plan(self.prev, self.ref, self.matrix, self.day, joint=self.joint)}
        return [v for v in viol if (v.code, v.order_id, v.staff_id) not in prev_viol]

    def check(self, plan: Plan) -> Plan:
        new = self.violations(plan)
        self.stats["violations"] = len(new)
        if not new:
            return plan
        self.stats["violations_detail"] = [str(v) for v in new[:5]]
        bad = {v.order_id for v in new if v.order_id}
        if self.joint:  # пара снимается целиком
            bad |= {jnt.partner_id(x) for x in bad} | {m for x in bad if (m := jnt.main_of(x))}
        fixed = False
        for A in self.anchors.values():
            drop = [o for o in A.seq if o.order_id in bad]
            if not drop:
                continue
            A.seq = [o for o in A.seq if o.order_id not in bad]
            A.touched, A.sched_ok = True, None
            sched, removed = self.repair(A)
            A.sched, A.sched_ok = sched, True
            for o in drop + removed:
                self.unplaced[o.order_id] = (o, A.k.staff_id, False)
                self.budget_exhausted.add(o.order_id)
            fixed = True
        if not fixed:
            return plan
        plan = self.assemble()
        self.stats["violations_after_fix"] = len(self.violations(plan))
        return plan

    def _snapshot(self) -> None:
        self._snap = ({k: v.model_copy() for k, v in self.unassigned.items()},
                      [a.model_copy() for a in self.alerts], self.ids.n)

    def _restore(self) -> None:
        un, alerts, n = self._snap
        self.unassigned = {k: v.model_copy() for k, v in un.items()}
        self.alerts = [a.model_copy() for a in alerts]
        self.ids.n = n

    @property
    def new_alerts(self) -> list[Alert]:
        return self.alerts[self.n_prev_alerts:]

    def run(self) -> PlanUpdate:
        self.apply()
        self.forecast_and_repair()
        self.insert_all()
        self._snapshot()
        plan = self.check(self.assemble())
        set_statuses(plan, self.ctx, self.t)
        plan.summary = assemble.summary(plan, len(self.day.orders))
        self.stats["utilization"] = assemble.utilization_breakdown(
            plan, {k.staff_id: "/".join(k.model.profiles) for k in self.ctx.staff})
        changed = []
        prev_routes = {r.staff_id: r for r in self.prev.routes}
        for r in plan.routes:
            p = prev_routes.get(r.staff_id)
            if p is None or _sig(p) != _sig(r) or p.status != r.status:
                changed.append(r.staff_id)
        late = sum(1 for a in self.new_alerts if a.type == AlertType.LATE_RISK)
        if late:
            self.message.append(f"LATE_RISK: {late}")
        self.stats["ms"] = int((time.perf_counter() - self.started) * 1000)
        return PlanUpdate(plan=plan, changed_staff_ids=sorted(changed), new_alerts=list(self.new_alerts),
                          message="; ".join(self.message) or None, stats=self.stats)


def _sig(route: Route) -> list[tuple]:
    return [(s.type, s.order_id, s.location_id, s.arrival, s.start, s.end) for s in route.stops]


def set_statuses(plan: Plan, ctx: DayContext, t: int) -> None:
    for r in plan.routes:
        for s in r.stops:
            st, en = ctx.to_sec(s.start), ctx.to_sec(s.end)
            if st < t:
                s.status = StopStatus.DONE if en <= t else StopStatus.IN_PROGRESS
            else:
                s.status = StopStatus.PLANNED


def apply_event(ref: ReferenceState, matrix: MatrixState, day: DayState, event: Event,
                traffic: bool | None = None, joint: bool | None = None) -> PlanUpdate:
    """traffic — учёт пробок по часам; None — из TRAFFIC_FACTORS. joint — JOINT_ORDERS."""
    return EventPlanner(ref, matrix, day, event, traffic=traffic, joint=joint).run()
