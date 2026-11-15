"""Мобильный экран и API сотрудника: /ui/employee (вне контракта /api/v1, наше расширение).

Маршрут сотрудника на день из текущей версии плана и отметки по визитам. Отметки хранятся
в памяти процесса под той же блокировкой, что и остальное состояние (принадлежат загрузке дня:
новая загрузка справочников или дня начинает их с нуля). При DATABASE_URL память — быстрый кэш,
каждая отметка и откат дописываются в таблицу marks фоном (storage/state_repo.py), а при старте
сервиса отметки восстанавливаются вместе с днём (storage/restore.py). Часть отметок порождает события контракта через
`svc.post_event` (единственный путь изменения плана):

| отметка            | событие контракта                                                    |
|--------------------|----------------------------------------------------------------------|
| CALLED             | нет (только запись)                                                  |
| ARRIVED            | VISIT_DELAYED, если прибыл позже планового начала на >= 5 мин        |
| DONE (COMPLETED)   | VISIT_DELAYED, если завершил позже планового конца; раньше — без события |
| TRANSFER           | ORDER_TRANSFERRED (actual_end = время отметки)                       |
| UNAVAILABLE        | STAFF_UNAVAILABLE                                                    |

Время отметки берётся из тела (`timestamp`, модельное время), иначе — текущее модельное время дня.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Literal, Optional

from fastapi import APIRouter, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from . import auth
from .errors import ApiError
from .service import _parse
from .storage import state_repo
from .staff_directory import display_name

router = APIRouter(prefix="/ui/employee", tags=["ui-employee"])

KINDS = ("CALLED", "ARRIVED", "DONE")
ALL_KINDS = KINDS + ("TRANSFER", "UNAVAILABLE")
# Правило ТЗ: позвонить абоненту за 30–60 минут до начала слота.
CALL_BEFORE_MAX_MIN = 60
CALL_BEFORE_MIN_MIN = 30
# Прибытие позже планового начала на столько минут и больше → VISIT_DELAYED.
LATE_ARRIVAL_THRESHOLD_MIN = 5
# Отметка с timestamp дальше этого от модельного времени дня отклоняется (защита от неверных часов телефона).
MAX_MARK_SKEW = dt.timedelta(hours=6)
DANGEROUS_KINDS = ("TRANSFER", "UNAVAILABLE")  # при AUTH_ENABLED=on требуют confirmed=true
STAFF_MARK_PREFIX = "@staff:"  # ключ отметок сотрудника (не наряда) в хранилище отметок

_ERRORS = {
    400: {"description": "Некорректное тело или дата (BAD_REQUEST)"},
    404: {"description": "Сотрудник не найден (STAFF_NOT_FOUND) или день не загружен (DAY_NOT_FOUND)"},
    409: {"description": "Справочники или план не готовы (REFERENCE_NOT_LOADED, PLAN_NOT_BUILT)"},
    401: {"description": "Нет или недействителен токен (UNAUTHORIZED)"},
    403: {"description": "Чужой маршрут или нужна роль диспетчера (FORBIDDEN)"},
    428: {"description": "Для TRANSFER/UNAVAILABLE нужно confirmed=true (CONFIRMATION_REQUIRED)"},
    422: {"description": "Отметка невозможна: ORDER_NOT_IN_ROUTE, MARK_OUT_OF_ORDER, VISIT_NOT_STARTED, "
                         "либо событие контракта отклонено (EVENT_CONFLICTS_PLAN, EVENT_OUT_OF_ORDER, "
                         "INVALID_PERIOD, STAFF_NOT_ON_SHIFT, ...) — код события сохраняется"},
}


_EX_DAY = "2026-11-16"  # дата только для примеров в OpenAPI (каталог данных здесь не читается)


class MarkIn(BaseModel):
    """Отметка сотрудника. `kind`: CALLED | ARRIVED | DONE (синоним COMPLETED) | TRANSFER | UNAVAILABLE."""

    model_config = ConfigDict(json_schema_extra={"examples": [
        {"order_id": "ORD-1", "kind": "CALLED"},
        {"order_id": "ORD-1", "kind": "ARRIVED", "timestamp": f"{_EX_DAY}T10:12:00+05:00"},
        {"order_id": "ORD-1", "kind": "COMPLETED", "timestamp": f"{_EX_DAY}T11:20:00+05:00"},
        {"order_id": "ORD-1", "kind": "TRANSFER", "reason": "TRUNK_CABLE", "timestamp": f"{_EX_DAY}T10:40:00+05:00"},
        {"kind": "UNAVAILABLE", "reason": "VEHICLE_BREAKDOWN", "timestamp": f"{_EX_DAY}T12:00:00+05:00",
         "until": f"{_EX_DAY}T14:00:00+05:00"},
    ]})

    kind: Literal["CALLED", "ARRIVED", "DONE", "COMPLETED", "TRANSFER", "UNAVAILABLE"]
    order_id: Optional[str] = Field(None, description="Наряд; обязателен для всех видов, кроме UNAVAILABLE")
    timestamp: Optional[AwareDatetime] = Field(
        None, description="Время отметки (ISO 8601 с поясом, модельное время). По умолчанию — текущее модельное время дня")
    at: Optional[AwareDatetime] = Field(None, description="Устаревший синоним `timestamp`")
    reason: Optional[str] = Field(
        None, description="TRANSFER: STATION_EQUIPMENT | TRUNK_CABLE; UNAVAILABLE: VEHICLE_BREAKDOWN | ILLNESS | OTHER")
    until: Optional[AwareDatetime] = Field(
        None, description="UNAVAILABLE: до какого времени недоступен; не задано — до конца смены")
    confirmed: Optional[bool] = Field(
        None, description="Подтверждение опасного действия (TRANSFER, UNAVAILABLE): при включённой авторизации "
                          "обязательно true, иначе 428 CONFIRMATION_REQUIRED")


class RevertIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    order_id: Optional[str] = Field(None, description="Наряд (для UNAVAILABLE не нужен)")
    kind: Literal["CALLED", "ARRIVED", "DONE", "COMPLETED", "TRANSFER", "UNAVAILABLE"]


class MarkEventInfo(BaseModel):
    event_id: str
    type: str
    accepted: bool
    duplicate: bool
    message: Optional[str] = None


class MarkOut(BaseModel):
    """Результат отметки. `event_sent` — было ли отправлено событие контракта в план."""

    accepted: bool = Field(description="Отметка принята (иначе вернётся ошибка 4xx)")
    duplicate: bool = Field(description="Такая отметка уже была: состояние и план не изменены")
    kind: str
    order_id: Optional[str] = None
    staff_id: str
    at: str
    recorded_at: str
    event_sent: bool
    event: Optional[MarkEventInfo] = Field(None, description="Событие контракта (или ранее отправленное, при duplicate)")
    event_note: Optional[str] = Field(None, description="Почему событие не отправлялось")
    plan_version: int
    route_changed: bool = Field(description="Маршрут сотрудника изменился: перечитайте GET /ui/employee/{id}/route")
    added_order_ids: list[str] = []
    removed_order_ids: list[str] = []
    changed_staff_ids: list[str] = Field([], description="Сотрудники, чьи маршруты пересчитаны событием")
    marks: dict[str, Optional[str]]
    next_mark: Optional[str] = None
    allowed_actions: list[str] = []


# ---------------------------------------------------------------- helpers

def _svc(request: Request):
    return request.app.state.service


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _resolve_date(request: Request, date: str | None) -> dt.date:
    """Дата из параметра, иначе активный день (как /ui/plan/status: изменившийся последним,
    иначе день демо, иначе последний день с планом)."""
    if date:
        try:
            return dt.date.fromisoformat(date)
        except ValueError:
            raise ApiError(400, "BAD_REQUEST", f"Некорректная дата '{date}', ожидается YYYY-MM-DD",
                           [{"field": "date", "message": "ожидается YYYY-MM-DD"}]) from None
    from .ui_api import active_day
    day = active_day(request)
    if day is None:
        raise ApiError(409, "PLAN_NOT_BUILT", "План дня ещё не построен: создайте день («Создать день») или загрузите его (PUT /api/v1/days + POST plan)")
    return day


def _day_marks(request: Request, svc, day) -> dict[str, dict[str, dict]]:
    """Отметки дня: order_id -> kind -> {at, recorded_at, staff_id}. Вызывать под lock."""
    box = getattr(request.app.state, "employee_marks", None)
    ref = svc.store.reference
    if box is None or box["ref"] is not ref:
        box = {"ref": ref, "days": {}}
        request.app.state.employee_marks = box
    key = (day.date, day.load_id or day.now)  # load_id — поколение дня (новая загрузка — новые отметки)
    return box["days"].setdefault(key, {})


def seed_marks(app, svc, day, marks: dict[str, dict[str, dict]]) -> None:
    """Отметки дня из БД при восстановлении (под lock вызывающего)."""
    ref = svc.store.reference
    box = getattr(app.state, "employee_marks", None)
    if box is None or box["ref"] is not ref:
        box = {"ref": ref, "days": {}}
        app.state.employee_marks = box
    box["days"][(day.date, day.load_id or day.now)] = {k: dict(v) for k, v in marks.items()}


def _snapshot(request: Request, date: dt.date):
    """Согласованный срез под lock: справочники, день (копия плана), отметки дня."""
    svc = _svc(request)
    with svc.store.lock:
        ref = svc.store.reference
        if ref is None:
            raise ApiError(409, "REFERENCE_NOT_LOADED", "Справочники не загружены: PUT /api/v1/reference или «Создать день»")
        day = svc._day(date)
        if day.plan is None:
            raise ApiError(409, "PLAN_NOT_BUILT", f"План на {date} ещё не построен")
        plan = day.plan.model_copy(deep=True)
        orders = dict(day.orders)
        marks = {oid: dict(v) for oid, v in _day_marks(request, svc, day).items()}
    return ref, day, plan, orders, marks


def _route(plan, staff_id: str):
    return next((r for r in plan.routes if r.staff_id == staff_id), None)


def _staff_or_404(ref, staff_id: str):
    staff = ref.staff.get(staff_id)
    if staff is None:
        raise ApiError(404, "STAFF_NOT_FOUND", f"Сотрудник {staff_id} не найден")
    return staff


def _sc_brief(ref, sc_id: str) -> dict | None:
    sc = ref.service_centers.get(sc_id)
    if sc is None:
        return None
    return {"service_center_id": sc.service_center_id, "name": sc.name, "address": sc.address,
            "lat": sc.lat, "lon": sc.lon}


def _location(ref, location_id: str, order) -> dict | None:
    if order is not None:
        return {"address": order.address, "lat": order.lat, "lon": order.lon}
    sc = ref.service_centers.get(location_id)
    if sc is not None:
        return {"address": sc.address, "lat": sc.lat, "lon": sc.lon, "name": sc.name}
    b = ref.buildings.get(location_id)
    if b is not None:
        return {"address": b.address, "lat": b.lat, "lon": b.lon}
    return None


def _stop_state(stop, order_marks: dict, as_of: dt.datetime) -> str:
    """DONE | IN_PROGRESS | PLANNED: отметки сотрудника важнее модельного статуса плана."""
    if stop.type == "VISIT":
        if "DONE" in order_marks or "TRANSFER" in order_marks:
            return "DONE"
        if "ARRIVED" in order_marks:
            return "IN_PROGRESS"
    # Статус плана PLANNED не доверяем слепо: он не обновляется, когда модельное время уже ушло вперёд.
    if stop.status is not None and str(stop.status) != "PLANNED":
        return str(stop.status)
    if stop.end <= as_of:
        return "DONE"
    if stop.start <= as_of:
        return "IN_PROGRESS"
    return "PLANNED"


def _order_dump(ref, order) -> dict:
    wt = ref.work_types.get(order.work_type_code)
    return {
        "order_id": order.order_id,
        "work_type_code": order.work_type_code,
        "work_type_name": wt.name if wt else order.work_type_code,
        "technology": order.technology,
        "address": order.address,
        "lat": order.lat,
        "lon": order.lon,
        "building_type": order.building_type,
        "requires_height": order.requires_height,
        "slot": order.slot.model_dump(mode="json"),
        "latest_start": _iso(order.latest_start),
        "required_equipment": order.required_equipment.model_dump(),
        "is_repeat": order.is_repeat,
        # В данных организаторов телефона абонента нет; поле зарезервировано для клиента.
        "customer_phone": getattr(order, "customer_phone", None),
    }


def _call_window(order) -> dict:
    start = order.slot.start - dt.timedelta(minutes=CALL_BEFORE_MAX_MIN)
    end = order.slot.start - dt.timedelta(minutes=CALL_BEFORE_MIN_MIN)
    return {"start": _iso(start), "end": _iso(end)}


def _marks_view(order_marks: dict) -> dict:
    return {k: (order_marks[k]["at"] if k in order_marks else None) for k in KINDS + ("TRANSFER",)}


def _next_kind(order_marks: dict) -> str | None:
    if "TRANSFER" in order_marks:
        return None
    return next((k for k in KINDS if k not in order_marks), None)


def _allowed_visit_actions(order_marks: dict) -> list[str]:
    """Действия по визиту сейчас: следующая отметка по порядку и, после прибытия, TRANSFER."""
    nxt = _next_kind(order_marks)
    if nxt is None:
        return []
    out = [nxt]
    if "ARRIVED" in order_marks and "DONE" not in order_marks:
        out.append("TRANSFER")
    return out


def _route_actions(route, staff_id: str, day, marks: dict) -> list[str]:
    if route is None or staff_id in day.absent_staff_ids or route.status == "UNAVAILABLE":
        return []
    return [] if STAFF_MARK_PREFIX + staff_id in marks else ["UNAVAILABLE"]


def _route_sig(route) -> list[tuple]:
    return [(str(s.type), s.order_id, s.start, s.end) for s in (route.stops if route else [])]


def _norm_kind(kind: str) -> str:
    return "DONE" if kind == "COMPLETED" else kind


# ---------------------------------------------------------------- routes

@router.get("/staff", summary="Список сотрудников смены",
            description="Сотрудники с маршрутом в текущем плане дня (без отсутствующих). "
                        "Параметр `date` (YYYY-MM-DD) необязателен: по умолчанию день демо или последний день с планом.",
            responses={k: _ERRORS[k] for k in (400, 409)})
def employee_staff(request: Request, date: str | None = None) -> dict:
    principal = auth.current_principal(request)
    only = principal.staff_id if principal.role == auth.EMPLOYEE else None  # сотрудник видит только себя
    day_date = _resolve_date(request, date)
    ref, day, plan, _orders, marks = _snapshot(request, day_date)
    items = []
    for route in plan.routes:
        if route.staff_id in day.absent_staff_ids or (only is not None and route.staff_id != only):
            continue
        s = ref.staff.get(route.staff_id)
        if s is None:
            continue
        visit_ids = [st.order_id for st in route.stops if st.type == "VISIT"]
        done = sum(1 for st in route.stops if st.type == "VISIT"
                   and _stop_state(st, marks.get(st.order_id, {}), plan.as_of) == "DONE")
        sc = ref.service_centers.get(s.service_center_id)
        items.append({
            "staff_id": s.staff_id,
            "display_name": display_name(s.staff_id, s.display_name),
            "profiles": list(s.profiles),
            "service_center_id": s.service_center_id,
            "service_center_name": sc.name if sc else None,
            "shift": {"start": s.shift.start, "end": s.shift.end},
            "route_status": route.status,
            "visits": len(visit_ids),
            "visits_done": done,
        })
    items.sort(key=lambda x: x["staff_id"])
    return {"date": day_date.isoformat(), "plan_version": plan.plan_version, "as_of": _iso(plan.as_of),
            "staff": items}


@router.get("/status", summary="Загружен ли день (для мобильного клиента без доступа к /ui/demo/*)",
            description="Ничего не загружает: `loaded=true`, если есть справочники и построенный план.")
def employee_status(request: Request) -> dict:
    from .ui_api import active_day
    svc = _svc(request)
    day_date = active_day(request)
    with svc.store.lock:
        state = svc.store.days.get(day_date) if svc.store.reference is not None and day_date else None
        plan = state.plan if state is not None else None
    return {"loaded": plan is not None, "date": day_date.isoformat() if plan is not None else None,
            "plan_version": plan.plan_version if plan else None, "as_of": _iso(plan.as_of) if plan else None}


@router.get("/{staff_id}/route", summary="Маршрут сотрудника на день",
            description="Остановки текущей версии плана с состоянием (`state`), отметками (`marks`), "
                        "окном звонка (`call_window`), координатами, телефоном абонента (`customer_phone`, "
                        "в данных организаторов его нет, поэтому null) и допустимыми действиями (`allowed_actions` "
                        "у визита и у маршрута). `next` — ближайший невыполненный визит.",
            responses={k: _ERRORS[k] for k in (400, 404, 409)})
def employee_route(staff_id: str, request: Request, date: str | None = None) -> dict:
    day_date = _resolve_date(request, date)
    ref, day, plan, orders, marks = _snapshot(request, day_date)
    staff = _staff_or_404(ref, staff_id)
    route = _route(plan, staff_id)
    as_of = plan.as_of

    stops = []
    for st in (route.stops if route else []):
        order = orders.get(st.order_id) if st.order_id else None
        om = marks.get(st.order_id, {}) if st.order_id else {}
        is_visit = st.type == "VISIT"
        item = {
            "seq": st.seq,
            "type": st.type,
            "location_id": st.location_id,
            "location": _location(ref, st.location_id, order),
            "order_id": st.order_id,
            "order": _order_dump(ref, order) if order is not None else None,
            "arrival": _iso(st.arrival),
            "start": _iso(st.start),
            "end": _iso(st.end),
            "travel_sec": st.travel_sec,
            "distance_m": st.distance_m,
            "plan_status": st.status,
            "state": _stop_state(st, om, as_of),
            "equipment_after": st.equipment_after.model_dump() if st.equipment_after else None,
            "explanation": st.explanation,
            "marks": _marks_view(om) if is_visit else None,
            "next_mark": _next_kind(om) if is_visit else None,
            "allowed_actions": _allowed_visit_actions(om) if is_visit else [],
            "call_window": _call_window(order) if order is not None and is_visit else None,
        }
        stops.append(item)

    # Монотонность: всё, что идёт в маршруте раньше уже начатого/выполненного визита (выезд из СЦ, перерыв,
    # пополнение), пройдено — иначе в списке остаются «Впереди» метки, которые давно позади.
    last_active = max(
        (i for i, s in enumerate(stops) if s["type"] == "VISIT" and s["state"] in ("DONE", "IN_PROGRESS")),
        default=-1,
    )
    for i in range(last_active):
        if stops[i]["type"] != "VISIT" and stops[i]["state"] == "PLANNED":
            stops[i]["state"] = "DONE"

    visits = [s for s in stops if s["type"] == "VISIT"]
    nxt = next((s for s in visits if s["state"] != "DONE"), None)
    next_info = None
    if nxt is not None:
        cw = nxt["call_window"]
        o = nxt["order"] or {}
        loc = nxt["location"] or {}
        next_info = {
            "seq": nxt["seq"],
            "order_id": nxt["order_id"],
            "address": o.get("address") or loc.get("address"),
            "lat": o.get("lat", loc.get("lat")),
            "lon": o.get("lon", loc.get("lon")),
            "customer_phone": o.get("customer_phone"),
            "slot": o.get("slot"),
            "arrival": nxt["arrival"],
            "start": nxt["start"],
            "end": nxt["end"],
            "state": nxt["state"],
            "call_window": cw,
            "call_hint": f"Позвонить абоненту до {cw['end'][11:16]}" if cw else None,
            "next_mark": nxt["next_mark"],
            "allowed_actions": nxt["allowed_actions"],
        }

    route_ids = {s["order_id"] for s in visits}
    mark_list = sorted(
        ({"order_id": oid, "kind": k, **v} for oid, kinds in marks.items() if oid in route_ids
         for k, v in kinds.items()),
        key=lambda m: (m["recorded_at"], ALL_KINDS.index(m["kind"])),
    )
    sc = _sc_brief(ref, staff.service_center_id)
    return {
        "date": day_date.isoformat(),
        "plan_version": plan.plan_version,
        "as_of": _iso(as_of),
        "staff": {
            "staff_id": staff.staff_id,
            "display_name": display_name(staff.staff_id, staff.display_name),
            "profiles": list(staff.profiles),
            "technologies": list(staff.technologies),
            "service_center": sc,
            "shift": staff.shift.model_dump(mode="json", by_alias=True),
        },
        "on_shift": route is not None and staff_id not in day.absent_staff_ids,
        "route_status": route.status if route else None,
        "unavailable_from": _iso(route.unavailable_from) if route else None,
        "unavailable_until": _iso(route.unavailable_until) if route else None,
        "allowed_actions": _route_actions(route, staff_id, day, marks),
        "progress": {"visits_total": len(visits), "visits_done": sum(1 for s in visits if s["state"] == "DONE")},
        "next": next_info,
        "stops": stops,
        "marks": mark_list,
    }


# ---------------------------------------------------------------- marks

def _event_error(exc: ApiError, ev_type: str) -> ApiError:
    """Любой отказ события контракта — 422 с исходным кодом и понятным текстом."""
    return ApiError(422, exc.code, f"Событие {ev_type} отклонено планом ({exc.code}): {exc.message}", exc.details)


def _mark_response(*, staff_id, kind, order_id, mark, order_marks, day, duplicate, event_sent, event,
                   event_note, route_changed=False, added=(), removed=(), changed=()) -> dict:
    return {
        "accepted": True,
        "duplicate": duplicate,
        "kind": kind,
        "order_id": order_id,
        "staff_id": staff_id,
        "at": mark["at"],
        "recorded_at": mark["recorded_at"],
        "event_sent": event_sent,
        "event": event,
        "event_note": event_note,
        "plan_version": day.plan_version,
        "route_changed": route_changed,
        "added_order_ids": list(added),
        "removed_order_ids": list(removed),
        "changed_staff_ids": list(changed),
        "marks": _marks_view(order_marks),
        "next_mark": _next_kind(order_marks),
        "allowed_actions": _allowed_visit_actions(order_marks) if order_id else [],
    }


def _post(svc, day_date: dt.date, payload: dict):
    try:
        return svc.post_event(day_date.isoformat(), json.dumps(payload).encode("utf-8"))
    except ApiError as exc:
        raise _event_error(exc, payload["type"]) from None


def _do_mark(request: Request, staff_id: str, body: MarkIn, day_date: dt.date) -> dict:
    svc = _svc(request)
    kind = _norm_kind(body.kind)
    if kind in DANGEROUS_KINDS and auth.auth_enabled() and body.confirmed is not True:
        raise ApiError(428, "CONFIRMATION_REQUIRED",
                       f"Требуется подтверждение: повторите запрос с confirmed=true ({kind} меняет план)",
                       [{"field": "confirmed", "message": "ожидается true"}])
    with svc.store.lock:
        ref = svc.store.reference
        if ref is None:
            raise ApiError(409, "REFERENCE_NOT_LOADED", "Справочники не загружены: PUT /api/v1/reference или «Создать день»")
        _staff_or_404(ref, staff_id)
        day = svc._day(day_date)
        if day.plan is None:
            raise ApiError(409, "PLAN_NOT_BUILT", f"План на {day_date} ещё не построен")
        route = _route(day.plan, staff_id)
        before = _route_sig(route)
        before_orders = {s.order_id for s in (route.stops if route else []) if s.type == "VISIT"}
        at = body.timestamp or body.at or day.model_time
        if abs(at - day.model_time) > MAX_MARK_SKEW:
            raise ApiError(422, "TIMESTAMP_OUT_OF_RANGE",
                           f"Время отметки {at.isoformat()} отличается от модельного времени дня "
                           f"{day.model_time.isoformat()} больше чем на {int(MAX_MARK_SKEW.total_seconds() // 3600)} ч",
                           [{"field": "timestamp", "message": "слишком далеко от модельного времени"}])
        marks_day = _day_marks(request, svc, day)

        # ---- недоступность сотрудника (не привязана к наряду)
        if kind == "UNAVAILABLE":
            if body.reason not in ("VEHICLE_BREAKDOWN", "ILLNESS", "OTHER"):
                raise ApiError(422, "REASON_REQUIRED",
                               "Для UNAVAILABLE нужна причина: VEHICLE_BREAKDOWN | ILLNESS | OTHER",
                               [{"field": "reason", "message": "VEHICLE_BREAKDOWN | ILLNESS | OTHER"}])
            om = marks_day.get(STAFF_MARK_PREFIX + staff_id, {})
            if "UNAVAILABLE" in om:
                return _mark_response(staff_id=staff_id, kind=kind, order_id=None, mark=om["UNAVAILABLE"],
                                      order_marks={}, day=day, duplicate=True, event_sent=False,
                                      event=om["UNAVAILABLE"].get("event"),
                                      event_note="Такая отметка уже была, событие не повторяется")
            payload = {"event_id": f"EV-MOB-{day_date:%Y%m%d}-UNAV-{staff_id}", "type": "STAFF_UNAVAILABLE",
                       "timestamp": at.isoformat(), "staff_id": staff_id, "reason": body.reason}
            if body.until is not None:
                payload["until"] = body.until.isoformat()
            return _finish(svc, request, staff_id, kind, None, day_date, payload, at, before, before_orders)

        # ---- отметки по наряду
        if not body.order_id:
            raise ApiError(422, "ORDER_ID_REQUIRED", f"Для {body.kind} нужен order_id",
                           [{"field": "order_id", "message": "обязательное поле"}])
        stop = next((s for s in (route.stops if route else [])
                     if s.type == "VISIT" and s.order_id == body.order_id), None)
        if stop is None:
            raise ApiError(422, "ORDER_NOT_IN_ROUTE",
                           f"Наряда {body.order_id} нет в маршруте сотрудника {staff_id}",
                           [{"field": "order_id", "message": "наряд не в маршруте сотрудника"}])
        order_marks = marks_day.get(body.order_id, {})
        if kind in order_marks:  # идемпотентно по (order_id, kind): событие не повторяется
            ex = order_marks[kind]
            return _mark_response(staff_id=staff_id, kind=kind, order_id=body.order_id, mark=ex,
                                  order_marks=order_marks, day=day, duplicate=True, event_sent=False,
                                  event=ex.get("event"), event_note="Такая отметка уже была, событие не повторяется")

        if kind == "TRANSFER":
            if body.reason not in ("STATION_EQUIPMENT", "TRUNK_CABLE"):
                raise ApiError(422, "REASON_REQUIRED",
                               "Для TRANSFER нужна причина: STATION_EQUIPMENT | TRUNK_CABLE",
                               [{"field": "reason", "message": "STATION_EQUIPMENT | TRUNK_CABLE"}])
            if "DONE" in order_marks:
                raise ApiError(422, "MARK_OUT_OF_ORDER", "Визит уже завершён, передать наряд нельзя",
                               [{"field": "kind", "message": "после DONE"}])
            if "ARRIVED" not in order_marks:
                raise ApiError(422, "MARK_OUT_OF_ORDER",
                               "Передать наряд можно только после отметки ARRIVED (визит должен быть начат)",
                               [{"field": "kind", "message": "сначала ARRIVED"}])
        else:
            if "TRANSFER" in order_marks:
                raise ApiError(422, "MARK_OUT_OF_ORDER", f"Наряд уже передан (TRANSFER), отметка {kind} невозможна",
                               [{"field": "kind", "message": "после TRANSFER"}])
            expected = _next_kind(order_marks)
            if kind != expected:
                raise ApiError(422, "MARK_OUT_OF_ORDER",
                               f"Отметка {kind} раньше {expected}: порядок CALLED → ARRIVED → DONE",
                               [{"field": "kind", "message": f"ожидается {expected}"}])
        prev = {"ARRIVED": "CALLED", "DONE": "ARRIVED", "TRANSFER": "ARRIVED"}.get(kind)
        if prev and at < dt.datetime.fromisoformat(order_marks[prev]["at"]):
            raise ApiError(422, "MARK_OUT_OF_ORDER", f"Время отметки {kind} раньше отметки {prev}",
                           [{"field": "timestamp", "message": "раньше предыдущей отметки"}])

        payload = None
        note = None
        eid = f"EV-MOB-{day_date:%Y%m%d}-{body.order_id}-{kind}"
        if kind == "CALLED":
            note = "Звонок: только запись отметки, событий контракта для него нет"
        elif kind == "ARRIVED":
            late = at - stop.start
            if late >= dt.timedelta(minutes=LATE_ARRIVAL_THRESHOLD_MIN):
                payload = {"event_id": eid, "type": "VISIT_DELAYED", "timestamp": at.isoformat(),
                           "staff_id": staff_id, "order_id": body.order_id,
                           "expected_end": (stop.end + late).isoformat()}
            else:
                note = (f"Прибытие позже планового начала менее чем на {LATE_ARRIVAL_THRESHOLD_MIN} мин "
                        "или вовремя: только отметка")
        elif kind == "DONE":
            if at > stop.end:
                payload = {"event_id": eid, "type": "VISIT_DELAYED", "timestamp": at.isoformat(),
                           "staff_id": staff_id, "order_id": body.order_id, "expected_end": at.isoformat()}
            else:
                note = ("Завершено не позже планового конца: только отметка (досрочное завершение "
                        "событием контракта не выражается)")
        else:  # TRANSFER
            if at <= stop.start:
                raise ApiError(422, "VISIT_NOT_STARTED",
                               f"Визит по {body.order_id} начинается по плану в {stop.start.isoformat()}: "
                               "ORDER_TRANSFERRED допустим только для начатого визита (timestamp позже начала)",
                               [{"field": "timestamp", "message": "не позже планового начала визита"}])
            payload = {"event_id": eid, "type": "ORDER_TRANSFERRED", "timestamp": at.isoformat(),
                       "staff_id": staff_id, "order_id": body.order_id, "actual_end": at.isoformat(),
                       "transferred_to": body.reason}
        return _finish(svc, request, staff_id, kind, body.order_id, day_date, payload, at, before, before_orders,
                       note)


def _finish(svc, request, staff_id, kind, order_id, day_date, payload, at, before, before_orders,
            note=None) -> dict:
    """Шлёт событие (если есть), записывает отметку, считает изменения маршрута. Под lock вызывающего."""
    event = None
    changed: list[str] = []
    if payload is not None:
        res = _post(svc, day_date, payload)  # при отказе поднимает 422: отметка не записывается
        event = {"event_id": res.event_id, "type": payload["type"], "accepted": res.accepted,
                 "duplicate": res.duplicate, "message": res.message}
        changed = list(res.changed_staff_ids)
    day = svc._day(day_date)
    marks_day = _day_marks(request, svc, day)
    key = order_id if order_id is not None else STAFF_MARK_PREFIX + staff_id
    om = marks_day.setdefault(key, {})
    mark = {"at": _iso(at), "recorded_at": dt.datetime.now(dt.timezone.utc).isoformat(), "staff_id": staff_id}
    principal = getattr(request.state, "principal", None)
    if principal is not None:  # журнал: кто отметил (время сервера — recorded_at)
        mark["author"] = principal.author()
    if event is not None:
        mark["event"] = event
    om[kind] = mark
    state_repo.save_mark(day.load_id, day.date, key, kind, mark)  # write-behind; без БД ничего
    route = _route(day.plan, staff_id)
    after_orders = {s.order_id for s in (route.stops if route else []) if s.type == "VISIT"}
    return _mark_response(
        staff_id=staff_id, kind=kind, order_id=order_id, mark=mark,
        order_marks=om if order_id else {}, day=day, duplicate=False, event_sent=event is not None,
        event=event, event_note=note, route_changed=_route_sig(route) != before,
        added=sorted(after_orders - before_orders), removed=sorted(before_orders - after_orders),
        changed=changed)


@router.post(
    "/{staff_id}/marks", summary="Отметка сотрудника (звонок, прибытие, завершение, передача, недоступность)",
    description="Записывает отметку и, если контракт это допускает, отправляет событие в план "
                "(VISIT_DELAYED / ORDER_TRANSFERRED / STAFF_UNAVAILABLE тем же путём, что POST /api/v1/days/{date}/events). "
                "Повтор отметки того же вида на тот же наряд: `duplicate: true`, без нового события. "
                "Если план событие отклонил, ответ 422 с кодом события, а отметка не записывается.",
    response_model=MarkOut,
    responses={k: _ERRORS[k] for k in (400, 404, 409, 422)},
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
        "schema": MarkIn.model_json_schema(), "examples": {
            "called": {"summary": "Звонок абоненту", "value": {"order_id": "ORD-1", "kind": "CALLED"}},
            "arrived": {"summary": "Прибытие (с фактическим временем)",
                        "value": {"order_id": "ORD-1", "kind": "ARRIVED", "timestamp": f"{_EX_DAY}T10:12:00+05:00"}},
            "transfer": {"summary": "Передача наряда",
                         "value": {"order_id": "ORD-1", "kind": "TRANSFER", "reason": "TRUNK_CABLE",
                                   "timestamp": f"{_EX_DAY}T10:40:00+05:00"}},
            "unavailable": {"summary": "Недоступен", "value": {
                "kind": "UNAVAILABLE", "reason": "ILLNESS", "timestamp": f"{_EX_DAY}T12:00:00+05:00"}},
        }}}}},
)
async def employee_mark(staff_id: str, request: Request, date: str | None = Query(
        None, description="Дата дня YYYY-MM-DD; по умолчанию день демо или последний день с планом")) -> dict:
    body: MarkIn = _parse(MarkIn.model_validate_json, await request.body())
    day_date = _resolve_date(request, date)
    return await run_in_threadpool(_do_mark, request, staff_id, body, day_date)


@router.post(
    "/{staff_id}/marks/revert", summary="Исправить отметку (только диспетчер)",
    description="Удаляет отметку, если по ней не отправлено событие в план и нет более поздних отметок по наряду. "
                "Иначе 409: `EVENT_ALREADY_APPLIED` («событие уже применено») или `MARK_HAS_DEPENDENT`.",
    responses={403: _ERRORS[403], 404: {"description": "Отметки нет (MARK_NOT_FOUND)"},
               409: {"description": "Событие уже применено (EVENT_ALREADY_APPLIED) или есть более поздняя отметка"}},
)
async def employee_mark_revert(staff_id: str, request: Request, date: str | None = Query(None)) -> dict:
    auth.require_dispatcher(request)
    body: RevertIn = _parse(RevertIn.model_validate_json, await request.body())
    day_date = _resolve_date(request, date)
    return await run_in_threadpool(_do_revert, request, staff_id, body, day_date)


def _do_revert(request: Request, staff_id: str, body: RevertIn, day_date: dt.date) -> dict:
    svc = _svc(request)
    kind = _norm_kind(body.kind)
    with svc.store.lock:
        ref = svc.store.reference
        if ref is None:
            raise ApiError(409, "REFERENCE_NOT_LOADED", "Справочники не загружены: PUT /api/v1/reference или «Создать день»")
        _staff_or_404(ref, staff_id)
        day = svc._day(day_date)
        marks_day = _day_marks(request, svc, day)
        if kind == "UNAVAILABLE":
            key = STAFF_MARK_PREFIX + staff_id
        elif body.order_id:
            key = body.order_id
        else:
            raise ApiError(422, "ORDER_ID_REQUIRED", f"Для {body.kind} нужен order_id",
                           [{"field": "order_id", "message": "обязательное поле"}])
        om = marks_day.get(key, {})
        mark = om.get(kind)
        if mark is None or mark.get("staff_id") != staff_id:
            raise ApiError(404, "MARK_NOT_FOUND", f"Отметки {kind} по {key} у сотрудника {staff_id} нет")
        if mark.get("event") is not None:
            raise ApiError(409, "EVENT_ALREADY_APPLIED",
                           f"Событие уже применено: отметка {kind} породила {mark['event']['type']} в плане, "
                           "откатить её нельзя")
        order = ("CALLED", "ARRIVED", "DONE", "TRANSFER")
        later = [k for k in om if k != kind and k in order and
                 (order.index(k) > order.index(kind) if kind in order else False)]
        if later:
            raise ApiError(409, "MARK_HAS_DEPENDENT", f"Сначала откатите более позднюю отметку: {', '.join(later)}")
        del om[kind]
        if not om:
            marks_day.pop(key, None)
        state_repo.delete_mark(day.load_id, key, kind)
        principal = getattr(request.state, "principal", None)
        log = getattr(request.app.state, "employee_mark_reverts", None)
        if log is None:
            log = request.app.state.employee_mark_reverts = []
        log.append({"staff_id": staff_id, "key": key, "kind": kind, "mark": mark,
                    "by": principal.author() if principal else None,
                    "at": dt.datetime.now(dt.timezone.utc).isoformat()})
        left = marks_day.get(key, {})
        return {"reverted": True, "kind": kind, "order_id": body.order_id, "staff_id": staff_id,
                "marks": _marks_view(left) if body.order_id else {},
                "next_mark": _next_kind(left) if body.order_id else None}
