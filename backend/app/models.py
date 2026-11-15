"""Pydantic-модели контракта API (data/api_spec.yaml, OpenAPI 3.0.3).

Имена полей, типы и enum-значения совпадают со спецификацией. Неизвестные входные
поля игнорируются (поведение pydantic по умолчанию). Все моменты времени — с
часовым поясом (AwareDatetime).
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum
from typing import Annotated, Literal, Optional, Union

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field


# ---------------------------------------------------------------- helpers

def _unique(values: list):
    if len(set(values)) != len(values):
        raise ValueError("элементы списка должны быть уникальны")
    return values


LocalTime = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
ServiceCenterId = Annotated[str, Field(pattern=r"^SC-[0-9]{2}$")]
StaffId = Annotated[str, Field(pattern=r"^ST-[0-9]{3}$")]
BuildingId = Annotated[str, Field(pattern=r"^B-[0-9]{5}$")]
AccessNodeId = Annotated[str, Field(pattern=r"^AN-[0-9]{4}$")]
OrderId = Annotated[str, Field(pattern=r"^ORD-[0-9]{8}-[0-9]{4}$")]


# ---------------------------------------------------------------- enums

class Technology(StrEnum):
    PON = "PON"
    FTTB = "FTTB"
    XDSL = "XDSL"
    DOCSIS = "DOCSIS"


class Profile(StrEnum):
    INSTALL = "INSTALL"
    LTP3 = "LTP3"


class WorkTypeCode(StrEnum):
    INST_PON_MKD = "INST_PON_MKD"
    INST_FTTB_MKD = "INST_FTTB_MKD"
    INST_PRIVATE_VLS = "INST_PRIVATE_VLS"
    ADD_SERVICE = "ADD_SERVICE"
    SURVEY_PRIVATE = "SURVEY_PRIVATE"
    REPAIR = "REPAIR"
    DEGRADATION = "DEGRADATION"
    DISMANTLE = "DISMANTLE"


class BuildingType(StrEnum):
    MKD = "MKD"
    PRIVATE = "PRIVATE"


class NodeType(StrEnum):
    OLT = "OLT"
    SWITCH = "SWITCH"
    DSLAM = "DSLAM"
    CMTS = "CMTS"


class DeadlineKind(StrEnum):
    SLOT = "SLOT"
    HOURS = "HOURS"
    WORKING_DAYS = "WORKING_DAYS"


class OrderOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    TRANSFERRED_TO_STATION = "TRANSFERRED_TO_STATION"
    CANCELLED = "CANCELLED"
    RESCHEDULED = "RESCHEDULED"
    PGP_GROUPED = "PGP_GROUPED"


class RouteStatus(StrEnum):
    ACTIVE = "ACTIVE"
    UNAVAILABLE = "UNAVAILABLE"


class StopType(StrEnum):
    DEPOT_START = "DEPOT_START"
    VISIT = "VISIT"
    BREAK = "BREAK"
    RESTOCK = "RESTOCK"
    DEPOT_END = "DEPOT_END"


class StopStatus(StrEnum):
    PLANNED = "PLANNED"
    IN_PROGRESS = "IN_PROGRESS"
    DONE = "DONE"


class ReasonCode(StrEnum):
    NO_QUALIFIED_STAFF = "NO_QUALIFIED_STAFF"
    NO_SHIFT_TIME = "NO_SHIFT_TIME"
    SLOT_UNREACHABLE = "SLOT_UNREACHABLE"
    NO_EQUIPMENT = "NO_EQUIPMENT"
    STAFF_UNAVAILABLE = "STAFF_UNAVAILABLE"
    OTHER = "OTHER"


class UnassignedAction(StrEnum):
    RESCHEDULE = "RESCHEDULE"
    REASSIGN = "REASSIGN"
    ESCALATE = "ESCALATE"


class AlertType(StrEnum):
    LATE_RISK = "LATE_RISK"
    PGP_DETECTED = "PGP_DETECTED"
    UNASSIGNED = "UNASSIGNED"


class AlertStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RESOLVED = "RESOLVED"


class AlertAction(StrEnum):
    REASSIGN = "REASSIGN"
    RESCHEDULE = "RESCHEDULE"
    TRANSFER_TO_STATION = "TRANSFER_TO_STATION"
    NONE = "NONE"


class PgpAction(StrEnum):
    TRANSFER_TO_STATION = "TRANSFER_TO_STATION"


class EventType(StrEnum):
    NEW_ORDER = "NEW_ORDER"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    VISIT_DELAYED = "VISIT_DELAYED"
    ORDER_TRANSFERRED = "ORDER_TRANSFERRED"
    STAFF_UNAVAILABLE = "STAFF_UNAVAILABLE"
    TIME_TICK = "TIME_TICK"


class CancelReason(StrEnum):
    NOT_CONFIRMED = "NOT_CONFIRMED"
    CUSTOMER_REQUEST = "CUSTOMER_REQUEST"


class TransferredTo(StrEnum):
    STATION_EQUIPMENT = "STATION_EQUIPMENT"
    TRUNK_CABLE = "TRUNK_CABLE"


class UnavailableReason(StrEnum):
    VEHICLE_BREAKDOWN = "VEHICLE_BREAKDOWN"
    ILLNESS = "ILLNESS"
    OTHER = "OTHER"


class ScenarioTriggerType(StrEnum):
    VISIT_DELAYED = "VISIT_DELAYED"
    ORDER_TRANSFERRED = "ORDER_TRANSFERRED"


# ---------------------------------------------------------------- basic

class Equipment(BaseModel):
    """Количество абонентского оборудования (АО) по видам."""

    ONT: int = Field(ge=0)
    ROUTER: int = Field(ge=0)
    STB: int = Field(ge=0)


class TimeWindow(BaseModel):
    start: AwareDatetime
    end: AwareDatetime


# ---------------------------------------------------------------- reference

class OpeningHours(BaseModel):
    open: LocalTime
    close: LocalTime


class ServiceCenter(BaseModel):
    service_center_id: ServiceCenterId
    name: str
    address: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    opening_hours: OpeningHours
    restock_duration_min: int = Field(ge=0)


class BreakWindow(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: LocalTime = Field(alias="from")
    to: LocalTime


class Shift(BaseModel):
    start: LocalTime
    end: LocalTime
    break_duration_min: int = Field(ge=0)
    break_window: BreakWindow


class Staff(BaseModel):
    staff_id: StaffId
    display_name: Optional[str] = None
    service_center_id: ServiceCenterId
    profiles: Annotated[list[Profile], Field(min_length=1), AfterValidator(_unique)]
    technologies: Annotated[list[Technology], Field(min_length=1), AfterValidator(_unique)]
    height_permit: bool
    shift: Shift
    equipment_start: Equipment
    equipment_capacity: Equipment
    experience_years: Optional[float] = Field(default=None, ge=0)


class Building(BaseModel):
    building_id: BuildingId
    address: str
    lat: float
    lon: float
    building_type: BuildingType
    overhead_line: bool
    zone_service_center_id: Optional[ServiceCenterId] = None


class AccessNode(BaseModel):
    access_node_id: AccessNodeId
    node_type: NodeType
    technology: Technology
    name: str
    address: Optional[str] = None
    lat: float
    lon: float
    buildings: Annotated[list[Building], Field(min_length=1)]


class WorkType(BaseModel):
    """Строка справочника work_types.csv."""

    code: WorkTypeCode
    name: str
    profiles: Annotated[list[Profile], Field(min_length=1), AfterValidator(_unique)]
    duration_min: int = Field(ge=1)
    max_start_delay_min: Optional[int] = Field(default=None, ge=0)
    deadline_kind: DeadlineKind
    deadline_value: Optional[int] = Field(default=None, ge=1)
    repeat_deadline_value: Optional[int] = Field(default=None, ge=1)
    pgp_relevant: bool


class HourlyFactor(BaseModel):
    hour: int = Field(ge=0, le=23)
    factor: float = Field(ge=0)


class TravelMatrix(BaseModel):
    matrix_id: str
    points: Annotated[list[str], Field(min_length=2), AfterValidator(_unique)]
    durations_sec: list[list[Annotated[int, Field(ge=0)]]]
    distances_m: list[list[Annotated[int, Field(ge=0)]]]
    hourly_factors: list[HourlyFactor] = Field(default_factory=list)


class ReferenceData(BaseModel):
    service_centers: Annotated[list[ServiceCenter], Field(min_length=1)]
    staff: Annotated[list[Staff], Field(min_length=1)]
    access_nodes: Annotated[list[AccessNode], Field(min_length=1)]
    work_types: Annotated[list[WorkType], Field(min_length=1)]


# ---------------------------------------------------------------- orders

class OrderFact(BaseModel):
    """Фактическое исполнение — только в orders_train.json."""

    outcome: Optional[OrderOutcome] = None
    staff_id: Optional[str] = None
    actual_start: Optional[AwareDatetime] = None
    actual_duration_min: Optional[int] = Field(default=None, ge=0)
    pgp_incident_id: Optional[str] = None


class Order(BaseModel):
    order_id: OrderId
    work_type_code: WorkTypeCode
    technology: Technology
    building_id: BuildingId
    address: str
    lat: float
    lon: float
    building_type: BuildingType
    requires_height: bool
    access_node_id: AccessNodeId
    registered_at: AwareDatetime
    slot: TimeWindow
    latest_start: AwareDatetime
    deadline: AwareDatetime
    required_equipment: Equipment
    is_repeat: bool
    previous_order_id: Optional[str] = None
    fact: Optional[OrderFact] = None


class DayInput(BaseModel):
    date: dt.date
    now: AwareDatetime
    orders: list[Order]
    absent_staff_ids: Annotated[list[str], AfterValidator(_unique)] = Field(default_factory=list)


class PlanRequest(BaseModel):
    time_limit_sec: int = Field(default=300, ge=1, le=300)


# ---------------------------------------------------------------- plan

class Stop(BaseModel):
    seq: int = Field(ge=1)
    type: StopType
    location_id: str
    order_id: Optional[str] = None
    arrival: AwareDatetime
    start: AwareDatetime
    end: AwareDatetime
    travel_sec: Optional[int] = Field(default=None, ge=0)
    distance_m: Optional[int] = Field(default=None, ge=0)
    status: Optional[StopStatus] = None
    equipment_after: Optional[Equipment] = None
    explanation: Optional[str] = None


class RouteTotals(BaseModel):
    visits: int = Field(default=0, ge=0)
    travel_sec: int = Field(default=0, ge=0)
    distance_m: int = Field(default=0, ge=0)
    work_min: int = Field(default=0, ge=0)
    utilization: float = Field(default=0, ge=0)


class Route(BaseModel):
    staff_id: str
    service_center_id: str
    status: RouteStatus
    unavailable_from: Optional[AwareDatetime] = None
    unavailable_until: Optional[AwareDatetime] = None
    stops: list[Stop] = Field(default_factory=list)
    totals: Optional[RouteTotals] = None


class UnassignedOrder(BaseModel):
    order_id: str
    reason_code: ReasonCode
    reason_text: str
    suggested_action: UnassignedAction
    proposed_slot: Optional[TimeWindow] = None
    since: Optional[AwareDatetime] = None


class PgpGroup(BaseModel):
    group_id: str
    access_node_id: str
    order_ids: Annotated[list[str], Field(min_length=2), AfterValidator(_unique)]
    detected_at: AwareDatetime
    action: PgpAction
    comment: Optional[str] = None


class CancelledOrder(BaseModel):
    order_id: str
    cancelled_at: AwareDatetime
    event_id: Optional[str] = None


class Alert(BaseModel):
    alert_id: str
    created_at: AwareDatetime
    type: AlertType
    status: AlertStatus
    resolved_at: Optional[AwareDatetime] = None
    order_id: Optional[str] = None
    group_id: Optional[str] = None
    staff_id: Optional[str] = None
    forecast_start: Optional[AwareDatetime] = None
    lateness_min: Optional[int] = None
    suggested_action: AlertAction
    suggested_staff_id: Optional[str] = None
    proposed_slot: Optional[TimeWindow] = None
    applied: bool = False
    message: str


class PlanSummary(BaseModel):
    orders_total: Optional[int] = None
    visits_planned: Optional[int] = None
    unassigned: Optional[int] = None
    pgp_orders: Optional[int] = None
    cancelled: Optional[int] = None
    travel_sec_total: Optional[int] = None
    utilization_cv: Optional[float] = None


class Plan(BaseModel):
    date: dt.date
    plan_version: int = Field(ge=1)
    based_on_event_id: Optional[str] = None
    as_of: AwareDatetime
    computation_ms: Optional[int] = Field(default=None, ge=0)
    routes: list[Route] = Field(default_factory=list)
    unassigned: list[UnassignedOrder] = Field(default_factory=list)
    pgp_groups: list[PgpGroup] = Field(default_factory=list)
    cancelled: list[CancelledOrder] = Field(default_factory=list)
    alerts: list[Alert] = Field(default_factory=list)
    summary: Optional[PlanSummary] = None


# ---------------------------------------------------------------- events

class EventBase(BaseModel):
    event_id: str
    timestamp: AwareDatetime


class NewOrderEvent(EventBase):
    type: Literal["NEW_ORDER"]
    order: Order


class OrderCancelledEvent(EventBase):
    type: Literal["ORDER_CANCELLED"]
    order_id: str
    reason: CancelReason


class VisitDelayedEvent(EventBase):
    type: Literal["VISIT_DELAYED"]
    staff_id: str
    order_id: str
    expected_end: AwareDatetime


class OrderTransferredEvent(EventBase):
    type: Literal["ORDER_TRANSFERRED"]
    staff_id: str
    order_id: str
    actual_end: AwareDatetime
    transferred_to: TransferredTo


class StaffUnavailableEvent(EventBase):
    type: Literal["STAFF_UNAVAILABLE"]
    staff_id: str
    until: Optional[AwareDatetime] = None
    reason: UnavailableReason


class TimeTickEvent(EventBase):
    type: Literal["TIME_TICK"]


Event = Annotated[
    Union[
        NewOrderEvent,
        OrderCancelledEvent,
        VisitDelayedEvent,
        OrderTransferredEvent,
        StaffUnavailableEvent,
        TimeTickEvent,
    ],
    Field(discriminator="type"),
]


class EventResult(BaseModel):
    event_id: str
    accepted: bool
    duplicate: bool
    plan_version: int = Field(ge=1)
    as_of: AwareDatetime
    processing_ms: Optional[int] = Field(default=None, ge=0)
    changed_staff_ids: list[str] = Field(default_factory=list)
    new_alerts: list[Alert] = Field(default_factory=list)
    message: Optional[str] = None


class ScenarioTrigger(BaseModel):
    trigger_id: str
    type: ScenarioTriggerType
    order_id: str
    fire_after_start_min: int = Field(ge=0)
    extra_duration_min: Optional[int] = Field(default=None, ge=1)
    transferred_to: Optional[TransferredTo] = None


class EventScenarioFile(BaseModel):
    """Сценарий дня (events_example.json). Файл, не API."""

    date: dt.date
    now: AwareDatetime
    events: list[Event]
    triggers: list[ScenarioTrigger]


# ---------------------------------------------------------------- service

class Health(BaseModel):
    status: Literal["ok"]
    team: Optional[str] = None
    version: Optional[str] = None


class LoadResult(BaseModel):
    status: Literal["loaded"]
    counts: dict[str, Annotated[int, Field(ge=0)]]


class ErrorDetail(BaseModel):
    field: Optional[str] = None
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[ErrorDetail] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: ErrorBody
