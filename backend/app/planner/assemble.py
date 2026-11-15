"""Сборка модели Plan из расписаний декодера; причины unassigned; предупреждения."""

from __future__ import annotations

import datetime as dt
import statistics

from ..models import (
    Alert,
    AlertAction,
    AlertStatus,
    AlertType,
    Equipment,
    PgpAction,
    PgpGroup,
    Plan,
    PlanSummary,
    ReasonCode,
    Route,
    RouteStatus,
    RouteTotals,
    Stop,
    StopStatus,
    StopType,
    TimeWindow,
    UnassignedAction,
    UnassignedOrder,
)
from .context import TZ, DayContext, OrderCtx
from .decoder import VISIT, RouteSchedule, best_insertion, decode
from .joint import is_joint, main_of, partner_id, unassigned_text
from .pgp import PgpCluster


def _eq(inv: tuple[int, int, int]) -> Equipment:
    return Equipment(ONT=inv[0], ROUTER=inv[1], STB=inv[2])


def _fmt(ctx: DayContext, sec: int) -> str:
    return ctx.to_dt(sec).strftime("%H:%M")


def explanation(ctx: DayContext, sched: RouteSchedule, stop_idx: int) -> str:
    stop = sched.stops[stop_idx]
    o = stop.order
    assert o is not None
    k = sched.staff
    parts = [
        f"{k.staff_id} ({'/'.join(k.model.profiles)}, {o.model.technology}"
        + (", допуск к высоте" if o.model.requires_height else "")
        + ")",
        f"в пути {round(stop.travel / 60)} мин",
        f"запас до крайнего начала {max(0, (o.b - stop.st) // 60)} мин",
        f"допустимых исполнителей на смене: {len(o.eligible)}",
    ]
    if stop_idx >= 2 and sched.stops[stop_idx - 1].kind == "RESTOCK":
        parts.append("АО пополнено перед визитом")
    if ctx.joint_staff:
        parts.extend(joint_note(ctx.joint_staff, o.order_id))
    return "; ".join(parts)


def joint_note(pairs: dict[str, tuple[str, str]], order_id: str) -> list[str]:
    """Пояснение к визиту совместного наряда (основной или напарник)."""
    main = main_of(order_id)
    if main is not None and main in pairs:
        return [f"совместный наряд: напарник по {main}, основной исполнитель {pairs[main][0]}, без списания АО"]
    if order_id in pairs:
        return [f"совместный наряд: напарник {pairs[order_id][1]} ({partner_id(order_id)}) с тем же началом"]
    return []


def route_model(ctx: DayContext, sched: RouteSchedule) -> Route:
    k = sched.staff
    stops: list[Stop] = []
    prev_point: int | None = None
    for i, s in enumerate(sched.stops):
        dist = 0 if prev_point is None else ctx.dist[prev_point][s.point]
        if s.kind == "BREAK":
            dist = 0
        stops.append(
            Stop(
                seq=i + 1,
                type=StopType(s.kind),
                location_id=ctx.point_ids[s.point],
                order_id=s.order.order_id if s.order is not None else None,
                arrival=ctx.to_dt(s.arr),
                start=ctx.to_dt(s.st),
                end=ctx.to_dt(s.en),
                travel_sec=s.travel,
                distance_m=dist,
                status=StopStatus.PLANNED,
                equipment_after=_eq(s.inv),
                explanation=explanation(ctx, sched, i) if s.kind == VISIT else None,
            )
        )
        prev_point = s.point
    totals = RouteTotals(
        visits=len(sched.orders),
        travel_sec=sched.travel if sched.orders else 0,
        distance_m=sum(st.distance_m or 0 for st in stops),
        work_min=sched.work // 60,
        utilization=round(sched.utilization, 4),
    )
    return Route(
        staff_id=k.staff_id,
        service_center_id=k.sc.sc_id,
        status=RouteStatus.ACTIVE,
        stops=stops,
        totals=totals,
    )


# ---------------------------------------------------------------- unassigned

def next_working_day(day: dt.date) -> dt.date:
    nxt = day + dt.timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += dt.timedelta(days=1)
    return nxt


def proposed_slot(ctx: DayContext, o: OrderCtx) -> TimeWindow | None:
    """Те же часы следующего рабочего дня, если начало + 10 мин + норматив ≤ deadline."""
    slot = o.model.slot
    shift = dt.datetime.combine(next_working_day(ctx.date), dt.time(0), tzinfo=TZ) - ctx.midnight
    start, end = slot.start + shift, slot.end + shift
    if start + dt.timedelta(minutes=10) + dt.timedelta(seconds=o.p) > o.model.deadline:
        return None
    return TimeWindow(start=start.astimezone(TZ), end=end.astimezone(TZ))


def classify_unassigned(
    ctx: DayContext,
    o: OrderCtx,
    schedules: dict[int, RouteSchedule],
    budget_exhausted: bool = False,
) -> UnassignedOrder:
    """Каскад причин docs/formalization.md, раздел 7."""
    model = o.model
    since = ctx.to_dt(ctx.now)
    if not o.eligible:
        need = f"профилем {'/'.join(o.work_type.profiles)}, технологией {model.technology}"
        if model.requires_height:
            need += " и допуском к работам на высоте"
        return UnassignedOrder(
            order_id=o.order_id,
            reason_code=ReasonCode.NO_QUALIFIED_STAFF,
            reason_text=f"На смене нет сотрудника с {need}",
            suggested_action=UnassignedAction.REASSIGN,
            proposed_slot=None,
            since=since,
        )

    if ctx.joint and is_joint(o) and len(o.eligible) < 2:
        # совместный наряд (JOINT_ORDERS=on), второго допустимого нет — существующий код причины
        return UnassignedOrder(
            order_id=o.order_id, reason_code=ReasonCode.NO_QUALIFIED_STAFF,
            reason_text=unassigned_text(o, len(o.eligible))[1],
            suggested_action=UnassignedAction.REASSIGN, proposed_slot=None, since=since,
        )

    staff = [ctx.staff[i] for i in o.eligible]
    alone = any(decode(ctx, k, [o]) is not None for k in staff)
    alone_no_eq = alone or any(decode(ctx, k, [o], ignore_eq=True) is not None for k in staff)
    n = len(staff)
    window = f"{_fmt(ctx, o.a)}–{_fmt(ctx, o.b)}"
    if not alone_no_eq:
        code = ReasonCode.SLOT_UNREACHABLE
        text = f"Ни один из {n} допустимых сотрудников не успевает начать визит в окне {window} в пределах смены"
    elif not alone:
        code = ReasonCode.NO_EQUIPMENT
        text = (
            f"Нужно АО ONT {o.dem[0]}, ROUTER {o.dem[1]}, STB {o.dem[2]}: заезд в СЦ на пополнение "
            f"не помещается в маршрут ни одного из {n} допустимых сотрудников"
        )
    else:
        free_staff = [k for k in staff if not schedules.get(k.idx) or not schedules[k.idx].orders]
        if ctx.joint and is_joint(o) and not budget_exhausted:
            # совместный наряд: одиночного свободного сотрудника мало, пара не нашлась
            code = ReasonCode.NO_SHIFT_TIME
            text = ctx.joint_fail.get(o.order_id) or unassigned_text(o, n)[1]
        elif budget_exhausted or any(decode(ctx, k, [o]) is not None for k in free_staff):
            code = ReasonCode.OTHER
            text = "Исчерпан бюджет расчёта до вставки наряда"
        elif any(
            best_insertion(ctx, schedules[k.idx], o, ignore_eq=True) is not None
            for k in staff
            if k.idx in schedules
        ):
            code = ReasonCode.NO_EQUIPMENT
            text = (
                f"Без учёта АО вставка возможна, но для АО ONT {o.dem[0]}, ROUTER {o.dem[1]}, STB {o.dem[2]} "
                f"заезд в СЦ не помещается в маршруты {n} допустимых сотрудников"
            )
        else:
            code = ReasonCode.NO_SHIFT_TIME
            text = (
                f"У {n} допустимых сотрудников не хватает времени смены: визит в окне {window} "
                f"не встаёт без срыва уже назначенных визитов"
            )
    if code == ReasonCode.OTHER:
        return UnassignedOrder(
            order_id=o.order_id, reason_code=code, reason_text=text,
            suggested_action=UnassignedAction.ESCALATE, proposed_slot=None, since=since,
        )
    slot = proposed_slot(ctx, o)
    return UnassignedOrder(
        order_id=o.order_id,
        reason_code=code,
        reason_text=text,
        suggested_action=UnassignedAction.RESCHEDULE if slot is not None else UnassignedAction.ESCALATE,
        proposed_slot=slot,
        since=since,
    )


# ---------------------------------------------------------------- alerts / pgp

class AlertIds:
    def __init__(self, day: dt.date, start: int = 0) -> None:
        self.day = day
        self.n = start

    def next(self) -> str:
        self.n += 1
        return f"ALR-{self.day:%Y%m%d}-{self.n:04d}"


def pgp_groups(ctx_date: dt.date, clusters: list[PgpCluster], now: dt.datetime, node_names: dict[str, str]) -> list[PgpGroup]:
    groups = []
    for i, c in enumerate(clusters, start=1):
        name = node_names.get(c.access_node_id, c.access_node_id)
        groups.append(
            PgpGroup(
                group_id=f"PGP-{ctx_date:%Y%m%d}-{i:02d}",
                access_node_id=c.access_node_id,
                order_ids=[o.order_id for o in c.orders],
                detected_at=now,
                action=PgpAction.TRANSFER_TO_STATION,
                comment=f"{len(c.orders)} обращения с {name} за {c.span_min} мин",
            )
        )
    return groups


def pgp_alert(ids: AlertIds, group: PgpGroup, now: dt.datetime) -> Alert:
    return Alert(
        alert_id=ids.next(),
        created_at=now,
        type=AlertType.PGP_DETECTED,
        status=AlertStatus.ACTIVE,
        group_id=group.group_id,
        suggested_action=AlertAction.TRANSFER_TO_STATION,
        applied=True,
        message=f"ПГП на {group.access_node_id}: {len(group.order_ids)} наряда переданы в станционное подразделение",
    )


def unassigned_alert(ids: AlertIds, u: UnassignedOrder, now: dt.datetime) -> Alert:
    action = {
        UnassignedAction.RESCHEDULE: AlertAction.RESCHEDULE,
        UnassignedAction.REASSIGN: AlertAction.REASSIGN,
    }.get(u.suggested_action, AlertAction.NONE)
    return Alert(
        alert_id=ids.next(),
        created_at=now,
        type=AlertType.UNASSIGNED,
        status=AlertStatus.ACTIVE,
        order_id=u.order_id,
        suggested_action=action,
        proposed_slot=u.proposed_slot if action == AlertAction.RESCHEDULE else None,
        applied=False,
        message=f"{u.order_id} не распределён ({u.reason_code.value}): {u.reason_text}",
    )


def _working_utils(plan: Plan) -> list[tuple[str, float]]:
    """(staff_id, ρ) по всем работающим сотрудникам смены: пустые маршруты входят с 0,
    UNAVAILABLE не считаются (absent в план не попадают вовсе)."""
    return [
        (r.staff_id, r.totals.utilization if r.totals else 0.0)
        for r in plan.routes
        if r.status == RouteStatus.ACTIVE
    ]


def _cv(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    return statistics.pstdev(values) / mean if mean > 0 else 0.0


def util_group(values: list[float]) -> dict:
    """cv/mean/min/max и хвосты загрузки группы сотрудников (docs/planner-notes.md, «Баланс загрузки»)."""
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "cv": round(_cv(values), 4),
        "mean": round(statistics.fmean(values), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "empty": sum(1 for u in values if u <= 0),
        "lt30": sum(1 for u in values if u < 0.3),
        "gt90": sum(1 for u in values if u > 0.9),
    }


def utilization_breakdown(plan: Plan, staff_profiles: dict[str, str]) -> dict:
    """Загрузка по всем работающим и отдельно по профилям (INSTALL, LTP3, ...).

    staff_profiles: staff_id -> ключ профиля ("/".join(profiles)). Вне контракта:
    кладётся в stats планировщика и в журнал истории, не в Plan."""
    utils = _working_utils(plan)
    groups: dict[str, list[float]] = {}
    for sid, u in utils:
        groups.setdefault(staff_profiles.get(sid, "?"), []).append(u)
    out = {"ALL": util_group([u for _, u in utils])}
    out.update({p: util_group(v) for p, v in sorted(groups.items())})
    return out


def staff_profiles(ref) -> dict[str, str]:
    return {sid: "/".join(s.profiles) for sid, s in ref.staff.items()}


def summary(plan: Plan, orders_total: int) -> PlanSummary:
    # CV по всем работающим сотрудникам смены, включая пустые маршруты (раньше — только по
    # задействованным, что скрывало простаивающих)
    cv = _cv([u for _, u in _working_utils(plan)])
    return PlanSummary(
        orders_total=orders_total,
        # визит напарника совместного наряда (ORD-…-B) не считается: пара — один размещённый наряд
        visits_planned=sum(
            1 for r in plan.routes for s in r.stops if s.type == StopType.VISIT and main_of(s.order_id) is None
        ),
        unassigned=len(plan.unassigned),
        pgp_orders=sum(len(g.order_ids) for g in plan.pgp_groups),
        cancelled=len(plan.cancelled),
        travel_sec_total=sum((r.totals.travel_sec if r.totals else 0) for r in plan.routes),
        utilization_cv=round(cv, 4),
    )
