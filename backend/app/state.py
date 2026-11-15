"""Состояние сервиса в памяти: справочники, матрица и дни."""

from __future__ import annotations

from dataclasses import dataclass, field
import datetime as dt

from .models import (
    AccessNode,
    Building,
    CancelledOrder,
    Order,
    Plan,
    ServiceCenter,
    Staff,
    TravelMatrix,
    WorkType,
)


@dataclass
class ReferenceState:
    service_centers: dict[str, ServiceCenter]
    staff: dict[str, Staff]
    access_nodes: dict[str, AccessNode]
    buildings: dict[str, Building]
    work_types: dict[str, WorkType]
    # building_id -> набор access_node_id, к которым он привязан
    building_nodes: dict[str, set[str]] = field(default_factory=dict)


@dataclass
class MatrixState:
    matrix: TravelMatrix
    index: dict[str, int]


@dataclass
class DayState:
    """Состояние одного рабочего дня.

    Планировщик получает клон и может его менять; сервис коммитит клон только
    после успешной обработки, поэтому ошибка не меняет состояние.
    """

    date: dt.date
    now: dt.datetime
    orders: dict[str, Order]
    absent_staff_ids: set[str]
    # order_id -> момент, с которого наряд известен диспетчеру
    arrived_at: dict[str, dt.datetime]
    cancelled: dict[str, CancelledOrder] = field(default_factory=dict)
    plan: Plan | None = None
    plan_version: int = 0
    model_time: dt.datetime | None = None
    # принятые события: event_id -> plan_version, который они породили
    events: dict[str, int] = field(default_factory=dict)
    # order_id -> конец визита из VISIT_DELAYED / ORDER_TRANSFERRED (H5, H10)
    visit_end_overrides: dict[str, dt.datetime] = field(default_factory=dict)
    # поколение дня: новое при каждой загрузке PUT /days/{date} (ключ снимка и отметок в БД)
    load_id: str = ""

    def __post_init__(self) -> None:
        if self.model_time is None:
            self.model_time = self.now

    def clone(self) -> "DayState":
        return DayState(
            date=self.date,
            now=self.now,
            orders=dict(self.orders),
            absent_staff_ids=set(self.absent_staff_ids),
            arrived_at=dict(self.arrived_at),
            cancelled=dict(self.cancelled),
            plan=self.plan.model_copy(deep=True) if self.plan is not None else None,
            plan_version=self.plan_version,
            model_time=self.model_time,
            events=dict(self.events),
            visit_end_overrides=dict(self.visit_end_overrides),
            load_id=self.load_id,
        )

    def pending_orders(self) -> list[Order]:
        """Наряды дня, не отменённые на текущий момент (в порядке загрузки)."""
        return [o for oid, o in self.orders.items() if oid not in self.cancelled]
