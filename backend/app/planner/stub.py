"""Заглушка планировщика.

Для каждого сотрудника на смене — маршрут DEPOT_START → DEPOT_END в его СЦ
(в границах смены), без визитов. Все наряды дня — в `unassigned` с OTHER.
Отменённые наряды — в `cancelled`. Реальный планировщик здесь не реализован.
"""

from __future__ import annotations

from datetime import datetime, time

from ..errors import ApiError
from ..models import (
    CancelledOrder,
    Event,
    OrderCancelledEvent,
    Plan,
    PlanSummary,
    ReasonCode,
    Route,
    RouteStatus,
    RouteTotals,
    Stop,
    StopType,
    UnassignedAction,
    UnassignedOrder,
)
from ..state import DayState, MatrixState, ReferenceState
from .base import PlanUpdate

STUB_REASON = "Заглушка планировщика: наряд ещё не распределён"


def _local_dt(day: DayState, hhmm: str) -> datetime:
    return datetime.combine(day.date, time.fromisoformat(hhmm), tzinfo=day.now.tzinfo)


def _depot_stop(seq: int, sc_id: str, at: datetime, kind: StopType) -> Stop:
    return Stop(
        seq=seq,
        type=kind,
        location_id=sc_id,
        order_id=None,
        arrival=at,
        start=at,
        end=at,
        travel_sec=0,
        distance_m=0,
        status="PLANNED",
    )


def _renumber(stops: list[Stop]) -> list[Stop]:
    for i, stop in enumerate(stops, start=1):
        stop.seq = i
    return stops


class StubPlanner:
    def build_plan(
        self, ref: ReferenceState, matrix: MatrixState, day: DayState, time_limit_sec: int | None = None
    ) -> Plan:
        routes: list[Route] = []
        for staff in ref.staff.values():
            if staff.staff_id in day.absent_staff_ids:
                continue
            start = _local_dt(day, staff.shift.start)
            end = _local_dt(day, staff.shift.end)
            routes.append(
                Route(
                    staff_id=staff.staff_id,
                    service_center_id=staff.service_center_id,
                    status=RouteStatus.ACTIVE,
                    stops=[
                        _depot_stop(1, staff.service_center_id, start, StopType.DEPOT_START),
                        _depot_stop(2, staff.service_center_id, end, StopType.DEPOT_END),
                    ],
                    totals=RouteTotals(),
                )
            )
        plan = Plan(
            date=day.date,
            plan_version=1,
            as_of=day.model_time,
            routes=routes,
            unassigned=self._unassigned(day),
            cancelled=list(day.cancelled.values()),
        )
        plan.summary = self._summary(day, plan)
        return plan

    def apply_event(
        self,
        ref: ReferenceState,
        matrix: MatrixState,
        day: DayState,
        event: Event,
    ) -> PlanUpdate:
        assert day.plan is not None, "apply_event вызван до build_plan"
        plan = day.plan.model_copy(deep=True)
        changed: list[str] = []

        if event.type == "NEW_ORDER":
            day.orders[event.order.order_id] = event.order
            day.arrived_at[event.order.order_id] = event.timestamp
        elif event.type == "ORDER_CANCELLED":
            day.cancelled[event.order_id] = _cancelled(event)
        elif event.type == "STAFF_UNAVAILABLE":
            changed.append(event.staff_id)
            self._mark_unavailable(plan, event.staff_id, event.timestamp, event.until)
        elif event.type in ("VISIT_DELAYED", "ORDER_TRANSFERRED"):
            # В заглушке визитов нет, поэтому ни один наряд не находится в маршруте.
            raise ApiError(
                422,
                "ORDER_NOT_IN_ROUTE",
                f"Наряд {event.order_id} не назначен сотруднику {event.staff_id}",
                [{"field": "order_id", "message": "наряд не в маршруте"}],
            )
        # TIME_TICK: только сдвиг модельного времени, он делается в сервисе.

        plan.unassigned = self._unassigned(day)
        plan.cancelled = list(day.cancelled.values())
        plan.summary = self._summary(day, plan)
        return PlanUpdate(plan=plan, changed_staff_ids=changed, new_alerts=[])

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _unassigned(day: DayState) -> list[UnassignedOrder]:
        return [
            UnassignedOrder(
                order_id=order.order_id,
                reason_code=ReasonCode.OTHER,
                reason_text=STUB_REASON,
                suggested_action=UnassignedAction.ESCALATE,
                since=day.arrived_at.get(order.order_id, day.now),
            )
            for order in day.pending_orders()
        ]

    @staticmethod
    def _summary(day: DayState, plan: Plan) -> PlanSummary:
        return PlanSummary(
            orders_total=len(day.orders),
            visits_planned=0,
            unassigned=len(plan.unassigned),
            pgp_orders=0,
            cancelled=len(day.cancelled),
            travel_sec_total=0,
            utilization_cv=0.0,
        )

    @staticmethod
    def _mark_unavailable(plan: Plan, staff_id: str, since: datetime, until: datetime | None) -> None:
        for route in plan.routes:
            if route.staff_id != staff_id:
                continue
            route.status = RouteStatus.UNAVAILABLE
            route.unavailable_from = since
            route.unavailable_until = until
            # Остановки депо, начинающиеся в период недоступности, убираем:
            # это разрешено спецификацией (DEPOT_END не обязателен у недоступного).
            route.stops = _renumber(
                [
                    s
                    for s in route.stops
                    if not (
                        s.type in (StopType.DEPOT_START, StopType.DEPOT_END)
                        and s.start >= since
                        and (until is None or s.start < until)
                    )
                ]
            )


def _cancelled(event: OrderCancelledEvent) -> CancelledOrder:
    return CancelledOrder(
        order_id=event.order_id,
        cancelled_at=event.timestamp,
        event_id=event.event_id,
    )
