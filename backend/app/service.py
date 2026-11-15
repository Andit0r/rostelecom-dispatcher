"""Логика API: справочники, дни, планы и события.

Все публичные методы принимают сырое тело запроса (bytes) и возвращают pydantic-модель.
Ошибки поднимаются как ApiError; при ошибке состояние не меняется: изменения
применяются к клону дня и коммитятся только после успешной обработки.
"""

from __future__ import annotations

import time
import uuid
from datetime import date

from pydantic import TypeAdapter, ValidationError

from .errors import ApiError
from .journal import journal
from .models import (
    Building,
    DayInput,
    Event,
    EventResult,
    LoadResult,
    Order,
    Plan,
    PlanRequest,
    ReferenceData,
    TravelMatrix,
)
from .planner import Planner
from .state import DayState, MatrixState, ReferenceState
from .storage import state_repo
from .store import Store

MAX_BODY_BYTES = 100 * 1024 * 1024
EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


# ---------------------------------------------------------------- parsing

def _path_of(loc: tuple) -> str:
    path = ""
    for part in loc:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path = f"{path}.{part}" if path else str(part)
    return path


def _details(exc: ValidationError) -> list[dict]:
    result = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False):
        result.append({"field": _path_of(err["loc"]) or None, "message": err["msg"]})
    return result


def _parse(parse, raw: bytes):
    if len(raw) > MAX_BODY_BYTES:
        raise ApiError(400, "BAD_REQUEST", "Тело запроса больше 100 МБ")
    try:
        return parse(raw)
    except ValidationError as exc:
        details = _details(exc)
        first = details[0] if details else {"field": None, "message": "некорректное тело"}
        where = first["field"] or "тело запроса"
        raise ApiError(400, "BAD_REQUEST", f"Поле {where}: {first['message']}", details)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ApiError(
            400,
            "BAD_REQUEST",
            f"Некорректная дата '{value}', ожидается YYYY-MM-DD",
            [{"field": "date", "message": "ожидается YYYY-MM-DD"}],
        ) from None


def _unprocessable(code: str, message: str, field: str | None = None) -> ApiError:
    details = [{"field": field, "message": message}] if field else []
    return ApiError(422, code, message, details)


def _conflict(code: str, message: str) -> ApiError:
    return ApiError(409, code, message)


# ---------------------------------------------------------------- reference

def _util(ref: ReferenceState, plan: Plan) -> dict | None:
    """Разбивка загрузки по профилям для журнала (вне контракта); без журнала не считается."""
    if not journal.enabled:
        return None
    from .planner.assemble import staff_profiles, utilization_breakdown

    return utilization_breakdown(plan, staff_profiles(ref))


def _build_reference(data: ReferenceData) -> ReferenceState:
    def unique(kind: str, ids: list[str]) -> None:
        if len(set(ids)) != len(ids):
            raise _unprocessable("DUPLICATE_ID", f"Повторяющиеся идентификаторы в {kind}")

    unique("service_centers", [sc.service_center_id for sc in data.service_centers])
    unique("staff", [s.staff_id for s in data.staff])
    unique("access_nodes", [n.access_node_id for n in data.access_nodes])
    unique("work_types", [w.code for w in data.work_types])

    sc_ids = {sc.service_center_id for sc in data.service_centers}
    for s in data.staff:
        if s.service_center_id not in sc_ids:
            raise _unprocessable(
                "UNKNOWN_SERVICE_CENTER",
                f"СЦ {s.service_center_id} сотрудника {s.staff_id} не найден",
                "staff.service_center_id",
            )
        for field_name in ("ONT", "ROUTER", "STB"):
            if getattr(s.equipment_capacity, field_name) < getattr(s.equipment_start, field_name):
                raise _unprocessable(
                    "INVALID_EQUIPMENT",
                    f"equipment_capacity меньше equipment_start у {s.staff_id}",
                    "staff.equipment_capacity",
                )

    buildings: dict[str, Building] = {}
    building_nodes: dict[str, set[str]] = {}
    for node in data.access_nodes:
        for b in node.buildings:
            if b.zone_service_center_id and b.zone_service_center_id not in sc_ids:
                raise _unprocessable(
                    "UNKNOWN_SERVICE_CENTER",
                    f"zone_service_center_id {b.zone_service_center_id} дома {b.building_id} не найден",
                )
            known = buildings.get(b.building_id)
            if known is None:
                buildings[b.building_id] = b
            elif known.model_dump() != b.model_dump():
                raise _unprocessable(
                    "BUILDING_CONFLICT",
                    f"Дом {b.building_id} указан в разных узлах с разными атрибутами",
                )
            building_nodes.setdefault(b.building_id, set()).add(node.access_node_id)

    return ReferenceState(
        service_centers={sc.service_center_id: sc for sc in data.service_centers},
        staff={s.staff_id: s for s in data.staff},
        access_nodes={n.access_node_id: n for n in data.access_nodes},
        buildings=buildings,
        work_types={w.code: w for w in data.work_types},
        building_nodes=building_nodes,
    )


# ---------------------------------------------------------------- service

class DispatchService:
    def __init__(self, store: Store, planner: Planner) -> None:
        self.store = store
        self.planner = planner

    # ------------------------------------------------ reference

    def put_reference(self, raw: bytes) -> LoadResult:
        ref = self.load_reference(raw)
        with self.store.lock:
            self.store.reference = ref
            # Загрузка справочников удаляет все дни, планы и события.
            self.store.days.clear()
            self.store.ref_gen = uuid.uuid4().hex
            state_repo.save_reference(raw, self.store.ref_gen)
        self.store.notify_reference(ref)  # /ui: справочник сотрудников и аккаунты (контракт не меняется)
        return LoadResult(
            status="loaded",
            counts={
                "service_centers": len(ref.service_centers),
                "staff": len(ref.staff),
                "access_nodes": len(ref.access_nodes),
                "buildings": len(ref.buildings),
                "work_types": len(ref.work_types),
            },
        )

    @staticmethod
    def load_reference(raw: bytes) -> ReferenceState:
        """Разбор и проверка справочников без изменения состояния (и для восстановления из БД)."""
        return _build_reference(_parse(ReferenceData.model_validate_json, raw))

    def put_travel_matrix(self, raw: bytes) -> LoadResult:
        state = self.load_matrix(raw)
        with self.store.lock:
            self.store.matrix = state
            state_repo.save_matrix(raw)
        return LoadResult(status="loaded", counts={"points": len(state.matrix.points)})

    @staticmethod
    def load_matrix(raw: bytes) -> MatrixState:
        matrix = _parse(TravelMatrix.model_validate_json, raw)
        n = len(matrix.points)
        if len(matrix.durations_sec) != n or any(len(r) != n for r in matrix.durations_sec):
            raise _unprocessable("MATRIX_SHAPE_MISMATCH", f"durations_sec должна быть {n}x{n}", "durations_sec")
        if len(matrix.distances_m) != n or any(len(r) != n for r in matrix.distances_m):
            raise _unprocessable("MATRIX_SHAPE_MISMATCH", f"distances_m должна быть {n}x{n}", "distances_m")
        return MatrixState(matrix=matrix, index={p: i for i, p in enumerate(matrix.points)})

    def _persist_day(self, day: DayState) -> None:
        """Снимок дня в БД (write-behind, под lock вызывающего); без БД ничего не делает."""
        modes = getattr(self.planner, "_day_modes", {}).get(day.date)
        state_repo.save_day(day, self.store.ref_gen, modes)

    # ------------------------------------------------ days

    def put_day(self, date_str: str, raw: bytes) -> LoadResult:
        day_date = _parse_date(date_str)
        body = _parse(DayInput.model_validate_json, raw)
        if body.date != day_date:
            raise ApiError(
                400,
                "BAD_REQUEST",
                f"Дата в теле {body.date} не совпадает с датой пути {day_date}",
                [{"field": "date", "message": "не совпадает с путём"}],
            )

        with self.store.lock:
            ref = self._reference()
            orders: dict[str, Order] = {}
            for i, order in enumerate(body.orders):
                if order.order_id in orders:
                    raise _unprocessable("DUPLICATE_ORDER", f"Повтор наряда {order.order_id}", f"orders[{i}].order_id")
                self._check_order_refs(ref, order, f"orders[{i}]")
                orders[order.order_id] = order
            for staff_id in body.absent_staff_ids:
                if staff_id not in ref.staff:
                    raise _unprocessable("UNKNOWN_STAFF", f"Неизвестный сотрудник {staff_id}", "absent_staff_ids")

            day = DayState(
                date=day_date,
                now=body.now,
                orders=orders,
                absent_staff_ids=set(body.absent_staff_ids),
                arrived_at={oid: body.now for oid in orders},
                load_id=uuid.uuid4().hex,
            )
            self.store.days[day_date] = day
            journal.record_day(day_date, body.now, len(orders))
            self._persist_day(day)
        return LoadResult(status="loaded", counts={"orders": len(orders)})

    def build_plan(self, date_str: str, raw: bytes | None) -> Plan:
        day_date = _parse_date(date_str)
        request = PlanRequest()
        if raw and raw.strip():
            request = _parse(PlanRequest.model_validate_json, raw)  # проверка time_limit_sec
        started = time.perf_counter()
        with self.store.lock:
            ref, matrix = self._reference(), self._matrix()
            self._check_matrix_coverage(ref, matrix)
            day = self._day(day_date)
            if day.events:
                raise _conflict("PLAN_ALREADY_ADJUSTED", "После первого события план нельзя построить заново")
            plan = self.planner.build_plan(ref, matrix, day, time_limit_sec=request.time_limit_sec)
            plan.plan_version = 1
            plan.based_on_event_id = None
            plan.as_of = day.now
            plan.computation_ms = int((time.perf_counter() - started) * 1000)
            day.plan = plan
            day.plan_version = 1
            day.model_time = day.now
            util = _util(ref, plan)
            journal.record_plan(plan, util)
            self._persist_day(day)
        return plan

    def get_plan(self, date_str: str) -> Plan:
        day_date = _parse_date(date_str)
        with self.store.lock:
            day = self._day(day_date)
            if day.plan is None:
                raise ApiError(404, "PLAN_NOT_FOUND", f"План на {day_date} ещё не построен")
            return day.plan.model_copy(deep=True)

    def post_event(self, date_str: str, raw: bytes) -> EventResult:
        day_date = _parse_date(date_str)
        event = _parse(EVENT_ADAPTER.validate_json, raw)
        started = time.perf_counter()

        with self.store.lock:
            ref, matrix = self._reference(), self._matrix()
            day = self._day(day_date)
            if day.plan is None:
                raise _conflict("PLAN_NOT_BUILT", "Сначала нужно построить план на день")

            if event.event_id in day.events:
                return EventResult(
                    event_id=event.event_id,
                    accepted=True,
                    duplicate=True,
                    plan_version=day.plan_version,
                    as_of=day.model_time,
                    processing_ms=0,
                    message="Событие уже принято ранее, состояние не изменено",
                )

            if event.timestamp < day.model_time:
                raise _conflict(
                    "EVENT_OUT_OF_ORDER",
                    f"timestamp {event.timestamp.isoformat()} раньше последнего события "
                    f"{day.model_time.isoformat()}",
                )

            candidate = day.clone()
            self._check_event(ref, candidate, event)
            update = self.planner.apply_event(ref, matrix, candidate, event)

            candidate.plan_version += 1
            new_plan = update.plan
            new_plan.plan_version = candidate.plan_version
            new_plan.based_on_event_id = event.event_id
            new_plan.as_of = event.timestamp
            new_plan.computation_ms = int((time.perf_counter() - started) * 1000)
            candidate.plan = new_plan
            candidate.model_time = event.timestamp
            candidate.events[event.event_id] = candidate.plan_version
            self.store.days[day_date] = candidate
            util = _util(ref, new_plan)
            processing_ms = int((time.perf_counter() - started) * 1000)
            # журнал и снимок дня ставятся в одну очередь под lock: порядок версий в БД = порядок в памяти
            journal.record_event(day_date, event, new_plan, processing_ms, util)
            self._persist_day(candidate)

        return EventResult(
            event_id=event.event_id,
            accepted=True,
            duplicate=False,
            plan_version=candidate.plan_version,
            as_of=candidate.model_time,
            processing_ms=processing_ms,
            changed_staff_ids=update.changed_staff_ids,
            new_alerts=update.new_alerts,
            message=getattr(update, "message", None),
        )

    # ------------------------------------------------ checks

    def _reference(self) -> ReferenceState:
        if self.store.reference is None:
            raise _conflict("REFERENCE_NOT_LOADED", "Сначала загрузите справочники (PUT /api/v1/reference)")
        return self.store.reference

    def _matrix(self) -> MatrixState:
        if self.store.matrix is None:
            raise _conflict("REFERENCE_NOT_LOADED", "Сначала загрузите матрицу времени в пути")
        return self.store.matrix

    def _day(self, day_date: date) -> DayState:
        day = self.store.days.get(day_date)
        if day is None:
            raise ApiError(404, "DAY_NOT_FOUND", f"День {day_date} не загружен")
        return day

    @staticmethod
    def _check_matrix_coverage(ref: ReferenceState, matrix: MatrixState) -> None:
        points = set(matrix.index)
        required = set(ref.service_centers) | set(ref.buildings)
        missing = sorted(required - points)
        if missing:
            raise _conflict(
                "REFERENCE_NOT_LOADED",
                f"Матрица не покрывает точки, например: {', '.join(missing[:5])}",
            )

    @staticmethod
    def _check_order_refs(ref: ReferenceState, order: Order, field: str) -> None:
        if order.building_id not in ref.buildings:
            raise _unprocessable("UNKNOWN_BUILDING", f"Дом {order.building_id} не найден", f"{field}.building_id")
        if order.access_node_id not in ref.access_nodes:
            raise _unprocessable(
                "UNKNOWN_ACCESS_NODE", f"Узел доступа {order.access_node_id} не найден", f"{field}.access_node_id"
            )
        if order.access_node_id not in ref.building_nodes.get(order.building_id, set()):
            raise _unprocessable(
                "BUILDING_NOT_ON_NODE",
                f"Дом {order.building_id} не обслуживается узлом {order.access_node_id}",
                f"{field}.access_node_id",
            )
        if order.work_type_code not in ref.work_types:
            raise _unprocessable(
                "UNKNOWN_WORK_TYPE", f"Тип работ {order.work_type_code} не найден", f"{field}.work_type_code"
            )

    def _check_event(self, ref: ReferenceState, day: DayState, event: Event) -> None:
        if event.type == "NEW_ORDER":
            if event.order.order_id in day.orders:
                raise _unprocessable("ORDER_ALREADY_EXISTS", f"Наряд {event.order.order_id} уже есть в дне", "order.order_id")
            self._check_order_refs(ref, event.order, "order")
            return
        if event.type == "TIME_TICK":
            return
        if event.type in ("VISIT_DELAYED", "ORDER_TRANSFERRED", "STAFF_UNAVAILABLE"):
            if event.staff_id not in ref.staff:
                raise _unprocessable("UNKNOWN_STAFF", f"Неизвестный сотрудник {event.staff_id}", "staff_id")
        if event.type == "STAFF_UNAVAILABLE":
            if event.until is not None and event.until <= event.timestamp:
                raise _unprocessable("INVALID_PERIOD", "until должен быть позже timestamp", "until")
            return
        if event.type in ("ORDER_CANCELLED", "VISIT_DELAYED", "ORDER_TRANSFERRED"):
            if event.order_id not in day.orders:
                raise _unprocessable("UNKNOWN_ORDER", f"Наряд {event.order_id} не найден в дне {day.date}", "order_id")
        if event.type == "ORDER_CANCELLED" and event.order_id in day.cancelled:
            raise _unprocessable("ORDER_ALREADY_CANCELLED", f"Наряд {event.order_id} уже отменён", "order_id")
