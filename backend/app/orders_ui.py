"""Наряды дня из интерфейса диспетчера (/ui/orders/*), вне контракта /api/v1.

* GET  /ui/orders/catalog       — типы работ, поиск домов (подстрока адреса, ≤ 20), сотрудники;
* POST /ui/orders/create        — сервер собирает полный Order по правилам контракта (как
  tools/synth/generate.py) и отправляет его событием NEW_ORDER в текущий план демо-дня;
* POST /ui/orders/{id}/cancel   — событие ORDER_CANCELLED (reason = CUSTOMER_REQUEST).

Время события — модельное время дня (day.model_time, оно же plan.as_of).

Пожелание по исполнителю (preferred_staff_id) — мягкое. Ядро перепланирования не меняется:
событие NEW_ORDER обрабатывает подкласс EventPlanner, у которого переопределён только
выбор исполнителя для нового наряда (place): сначала обычная оценка автоназначения, затем
пробная вставка в хвост маршрута желаемого сотрудника теми же insertion/decode_tail
(все окна, перерыв, конец смены, рюкзак АО — жёстко). Удалась — наряд у него, иначе
автоназначение и причина. Итоговый план, как и всегда, проходит validate_plan +
validate_transition внутри EventPlanner.check; фактический результат сверяется по плану.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from .errors import ApiError
from .models import AlertType, StopType
from .planner.context import qualifies
from .planner.events import EventPlanner
from .ui_api import TZ, _iso, _service, _staff_name, active_day, demo_for, require_active_day

router = APIRouter(prefix="/ui/orders", tags=["ui"])

SLOT_HOURS = (9, 11, 13, 15, 17, 19)  # стандартные двухчасовые окна, как в генераторе
SLOT_LEN = dt.timedelta(hours=2)
NEAREST_LEAD = dt.timedelta(minutes=30)  # генератор: slot.start − registered_at ≥ 30 мин
MIN_LEAD = dt.timedelta(minutes=15)  # ручное окно: до latest_start должно оставаться ≥ 15 мин
SEARCH_LIMIT = 20


# ---------------------------------------------------------------- правила генератора

def _compatible(code: str, b, techs: set[str]) -> bool:
    """Какие дома подходят типу работ (tools/synth/generate.py, gen_orders.pairs_by_type)."""
    if code == "INST_PON_MKD":
        return b.building_type == "MKD" and "PON" in techs
    if code == "INST_FTTB_MKD":
        return b.building_type == "MKD" and "FTTB" in techs
    if code == "INST_PRIVATE_VLS":
        return b.building_type == "PRIVATE" and b.overhead_line
    if code == "SURVEY_PRIVATE":
        return b.building_type == "PRIVATE"
    return True


def _required_tech(code: str) -> str | None:
    return {"INST_PON_MKD": "PON", "INST_FTTB_MKD": "FTTB"}.get(code)


def _equipment(code: str, tech: str) -> dict:
    """Детерминированный вариант Gen.equipment_for (генератор добавляет STB/роутер случайно)."""
    eq = {"ONT": 0, "ROUTER": 0, "STB": 0}
    if code == "INST_PON_MKD":
        eq.update(ONT=1, ROUTER=1)
    elif code == "INST_FTTB_MKD":
        eq.update(ROUTER=1)
    elif code == "INST_PRIVATE_VLS":
        eq.update(ONT=1 if tech == "PON" else 0, ROUTER=1)
    elif code == "ADD_SERVICE":
        eq.update(ROUTER=1)
    elif code in ("REPAIR", "DEGRADATION"):
        eq.update(ONT=1 if tech == "PON" else 0)
    return eq


def _add_working_days(moment: dt.datetime, n: int) -> dt.datetime:
    result, left = moment, n
    while left > 0:
        result += dt.timedelta(days=1)
        if result.weekday() < 5:
            left -= 1
    return result


def _deadline(wt, registered: dt.datetime, latest: dt.datetime, is_repeat: bool) -> dt.datetime:
    """Контрольный срок (compute_deadline генератора, data/api_spec.yaml Order.deadline)."""
    kind = getattr(wt.deadline_kind, "value", wt.deadline_kind)
    if kind == "SLOT":
        return latest + dt.timedelta(minutes=wt.duration_min)
    value = wt.repeat_deadline_value if (is_repeat and wt.repeat_deadline_value) else wt.deadline_value
    if kind == "HOURS":
        return registered + dt.timedelta(hours=value)
    return _add_working_days(registered, value)


def _latest_start(start: dt.datetime, end: dt.datetime, max_delay: int | None) -> dt.datetime:
    return end if max_delay is None else min(end, start + dt.timedelta(minutes=max_delay))


# ---------------------------------------------------------------- состояние демо-дня

def _require_day(request: Request):
    """(svc, demo, date) активного дня (как /ui/plan/status) или 409, если дня с планом нет.
    demo — состояние проигрывателя, только если он ведёт этот же день (иначе None)."""
    svc = _service(request)
    if svc.store.reference is None:
        raise ApiError(409, "PLAN_NOT_BUILT", "День не загружен: создайте день или загрузите его (PUT /api/v1/days)")
    date = require_active_day(request)
    day = svc.store.days.get(date)
    if day is None or day.plan is None:
        raise ApiError(409, "PLAN_NOT_BUILT", "План дня ещё не построен: загрузите день заново")
    return svc, demo_for(request, date), date.isoformat()


def _node_techs(ref, building_id: str) -> dict[str, str]:
    """access_node_id -> технология для узлов, обслуживающих дом."""
    return {nid: ref.access_nodes[nid].technology for nid in sorted(ref.building_nodes.get(building_id, ()))}


def _next_order_id(day, demo: dict | None) -> str:
    prefix = f"ORD-{day.date:%Y%m%d}-"
    n = 0
    for oid in list(day.orders) + list((demo or {}).get("order_info", {})):
        if oid.startswith(prefix):
            try:
                n = max(n, int(oid[len(prefix):]))
            except ValueError:
                pass
    if n >= 9999:
        raise ApiError(422, "ORDER_ID_EXHAUSTED", "Номера нарядов на этот день закончились")
    return f"{prefix}{n + 1:04d}"


def _next_event_id(day) -> str:
    prefix = f"EV-UI-{day.date:%Y%m%d}-"
    n = sum(1 for e in day.events if e.startswith(prefix))
    while f"{prefix}{n + 1:04d}" in day.events:
        n += 1
    return f"{prefix}{n + 1:04d}"


def _event_item(res, body: dict) -> dict:
    """Элемент в формате /ui/demo/step (sent[]) — для строки итога в шапке."""
    return {"event_id": res.event_id, "type": body["type"], "timestamp": body["timestamp"], "accepted": res.accepted,
            "plan_version": res.plan_version, "processing_ms": res.processing_ms, "message": res.message,
            "changed_staff_ids": res.changed_staff_ids}


def _warnings(res, skip_order: str | None = None) -> list[str]:
    out = []
    for a in res.new_alerts:
        if skip_order and a.order_id == skip_order and a.type == AlertType.UNASSIGNED:
            continue  # причина нового наряда и так в карточке
        if a.message:
            out.append(a.message)
    return out[:8]


# ---------------------------------------------------------------- пожелание по исполнителю

class _PreferredEventPlanner(EventPlanner):
    """EventPlanner, у которого для нового наряда сначала пробуется желаемый сотрудник."""

    def __init__(self, *args, preferred: str, order_id: str, report: dict, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.pref_id, self.pref_order, self.report = preferred, order_id, report

    def _pref_reason(self, o) -> str | None:
        """Почему желаемый сотрудник не может взять наряд в принципе (до пробной вставки)."""
        ref, sid = self.ref, self.pref_id
        staff = ref.staff[sid]
        name = _staff_name(sid, staff.display_name) or sid
        wt, order = o.work_type, o.model
        if sid in self.day.absent_staff_ids or sid not in self.anchors:
            return f"{name} сегодня не на смене"
        A = self.anchors[sid]
        if A.blocked:
            return f"{name} недоступен до конца смены"
        if A.closed or self.t >= A.k.end:
            return f"{name} уже закончил смену"
        if not qualifies(staff, order, wt):
            if not set(staff.profiles) & set(wt.profiles):
                return f"{name} не выполняет «{wt.name}» (нужен профиль {'/'.join(wt.profiles)})"
            if order.technology not in staff.technologies:
                return f"{name} не работает с технологией {order.technology}"
            return f"{name} не имеет допуска к работам на высоте"
        cap, dem = A.k.cap, o.dem
        if any(c < d for c, d in zip(cap, dem)):
            return f"У {name} нет нужного оборудования: в рюкзак не помещается требуемое АО"
        return None

    def place(self, o, exclude=None):
        if o.order_id != self.pref_order or self.report.get("tried"):
            return super().place(o, exclude)
        self.report["tried"] = True
        auto = self.best_staff(o, exclude)
        if auto is not None and auto[1].k.staff_id == self.pref_id:
            self.report["auto_same"] = True
            return super().place(o, exclude)
        reason = self._pref_reason(o)
        if reason is None:
            A = self.anchors[self.pref_id]
            res = self.insertion(A, o, max_tries=12)
            if res is None:
                name = _staff_name(self.pref_id, A.k.model.display_name) or self.pref_id
                win = f"{self.ctx.to_dt(o.a):%H:%M}–{self.ctx.to_dt(o.b):%H:%M}"
                reason = (f"{name} не успевает начать визит в окне {win} без срыва своих визитов, "
                          f"перерыва или конца смены")
            else:
                cost, sched, seq = res
                A.seq, A.sched, A.sched_ok, A.touched = seq, sched, True, True
                self.report["placed"] = True
                if auto is not None:
                    self.report["extra_travel_min"] = max(0, round((cost - auto[0]) / 60))
                return self.pref_id
        self.report["reason"] = reason
        if auto is None:
            return None
        _, A, sched, seq = auto
        A.seq, A.sched, A.sched_ok, A.touched = seq, sched, True, True
        return A.k.staff_id


class _PlannerProxy:
    """Подменяет apply_event только для одного NEW_ORDER; остальное — исходный планировщик."""

    def __init__(self, base, preferred: str, order_id: str, report: dict) -> None:
        self.base, self.preferred, self.order_id, self.report = base, preferred, order_id, report

    def __getattr__(self, name):
        return getattr(self.base, name)

    def apply_event(self, ref, matrix, day, event):
        if event.type != "NEW_ORDER" or not hasattr(self.base, "traffic"):
            return self.base.apply_event(ref, matrix, day, event)  # заглушка: пожелание не применяется
        traffic = self.base.traffic
        if traffic is None:  # режим плана дня, зафиксированный при его построении
            pinned = getattr(self.base, "_day_modes", {}).get(day.date)
            traffic = pinned[0] if pinned else None
        update = _PreferredEventPlanner(ref, matrix, day, event, traffic=traffic,
                                        preferred=self.preferred, order_id=self.order_id, report=self.report).run()
        self.base.last_event_stats = update.stats
        return update


# ---------------------------------------------------------------- запросы

class SlotIn(BaseModel):
    start: str = Field(description="HH:MM (местное время дня)")
    end: str = Field(description="HH:MM")


class CreateOrderRequest(BaseModel):
    building_id: str
    work_type: str
    slot: Optional[SlotIn] = None  # None — ближайшее стандартное окно
    is_repeat: bool = False
    preferred_staff_id: Optional[str] = None


def _hhmm(day: dt.date, value: str, field: str) -> dt.datetime:
    m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value.strip())
    if not m:
        raise ApiError(400, "BAD_REQUEST", f"Поле slot.{field}: ожидается ЧЧ:ММ",
                       [{"field": f"slot.{field}", "message": "ожидается ЧЧ:ММ"}])
    return dt.datetime.combine(day, dt.time(int(m.group(1)), int(m.group(2))), tzinfo=TZ)


def _pick_slot(day: dt.date, now: dt.datetime, slot: SlotIn | None, wt) -> tuple[dt.datetime, dt.datetime]:
    if slot is None:
        for h in SLOT_HOURS:
            start = dt.datetime.combine(day, dt.time(h), tzinfo=TZ)
            if start - now >= NEAREST_LEAD:
                return start, start + SLOT_LEN
        raise ApiError(422, "NO_SLOT_LEFT",
                       f"На сегодня стандартных окон не осталось (модельное время {now:%H:%M}); задайте окно вручную",
                       [{"field": "slot", "message": "нет ближайшего окна"}])
    start, end = _hhmm(day, slot.start, "start"), _hhmm(day, slot.end, "end")
    if end <= start:
        raise ApiError(422, "INVALID_SLOT", "Конец окна должен быть позже начала",
                       [{"field": "slot.end", "message": "раньше начала"}])
    latest = _latest_start(start, end, wt.max_start_delay_min)
    if latest - now < MIN_LEAD:
        raise ApiError(422, "SLOT_IN_PAST",
                       f"Окно {start:%H:%M}–{end:%H:%M} уже не успеть: крайнее начало {latest:%H:%M}, "
                       f"модельное время {now:%H:%M}",
                       [{"field": "slot.start", "message": "окно уже прошло"}])
    return start, end


# ---------------------------------------------------------------- routes

@router.get("/catalog")
def catalog(
    request: Request,
    q: str = Query("", max_length=100),
    work_type: str | None = None,
    limit: int = Query(SEARCH_LIMIT, ge=1, le=SEARCH_LIMIT),
) -> dict:
    """Справочники для формы нового наряда: типы работ, дома по подстроке адреса, сотрудники — только из
    справочника в Store (PUT reference / «Создать день»); каталог data/synth здесь не читается."""
    svc = _service(request)
    ref = svc.store.reference
    if ref is None:
        raise ApiError(409, "REFERENCE_NOT_LOADED",
                       "Справочник не загружен: пришлите PUT /api/v1/reference или нажмите «Создать день»")
    active = active_day(request)
    absent: set[str] = set()
    if active is not None:
        day = svc.store.days.get(active)
        absent = set(day.absent_staff_ids) if day is not None else set()

    if work_type is not None and work_type not in ref.work_types:
        raise ApiError(422, "UNKNOWN_WORK_TYPE", f"Тип работ {work_type} не найден",
                       [{"field": "work_type", "message": "нет в справочнике"}])
    needle = " ".join(q.lower().split())
    found, total = [], 0
    for b in sorted(ref.buildings.values(), key=lambda x: x.address):
        if needle and needle not in " ".join(b.address.lower().split()) and needle != b.building_id.lower():
            continue
        techs = set(_node_techs(ref, b.building_id).values())
        compatible = [c for c in ref.work_types if _compatible(c, b, techs)]
        if work_type is not None and work_type not in compatible:
            continue
        total += 1
        if len(found) < limit:
            found.append({"building_id": b.building_id, "address": b.address, "lat": b.lat, "lon": b.lon,
                          "building_type": b.building_type, "overhead_line": b.overhead_line,
                          "technologies": sorted(techs), "work_types": compatible})
    staff = [{"staff_id": s.staff_id, "display_name": _staff_name(s.staff_id, s.display_name),
              "profiles": list(s.profiles), "technologies": list(s.technologies),
              "height_permit": s.height_permit, "on_shift": s.staff_id not in absent}
             for s in ref.staff.values()]
    work_types = [{"code": w.code, "name": w.name, "profiles": list(w.profiles), "duration_min": w.duration_min,
                   "pgp_relevant": w.pgp_relevant, "repeat_allowed": w.repeat_deadline_value is not None}
                  for w in ref.work_types.values()]
    return {"work_types": work_types, "buildings": found, "buildings_total": total,
            "buildings_count": len(ref.buildings), "staff": staff}


@router.post("/create")
def create_order(body: CreateOrderRequest, request: Request) -> dict:
    svc, demo, date = _require_day(request)
    report: dict = {}
    with svc.store.lock:
        ref = svc.store.reference
        day = svc._day(dt.date.fromisoformat(date))
        now = day.model_time
        b = ref.buildings.get(body.building_id)
        if b is None:
            raise ApiError(422, "UNKNOWN_BUILDING", f"Дом {body.building_id} не найден",
                           [{"field": "building_id", "message": "нет в справочнике"}])
        wt = ref.work_types.get(body.work_type)
        if wt is None:
            raise ApiError(422, "UNKNOWN_WORK_TYPE", f"Тип работ {body.work_type} не найден",
                           [{"field": "work_type", "message": "нет в справочнике"}])
        nodes = _node_techs(ref, b.building_id)
        if not nodes:
            raise ApiError(422, "BUILDING_NOT_ON_NODE", f"Дом {b.address} не подключён ни к одному узлу доступа")
        if not _compatible(wt.code, b, set(nodes.values())):
            raise ApiError(422, "WORK_TYPE_NOT_APPLICABLE",
                           f"«{wt.name}» не выполняется по адресу {b.address} "
                           f"({'МКД' if b.building_type == 'MKD' else 'частный дом'}, {'/'.join(sorted(set(nodes.values())))})",
                           [{"field": "work_type", "message": "не подходит дому"}])
        if body.preferred_staff_id is not None and body.preferred_staff_id not in ref.staff:
            raise ApiError(422, "UNKNOWN_STAFF", f"Неизвестный сотрудник {body.preferred_staff_id}",
                           [{"field": "preferred_staff_id", "message": "нет в справочнике"}])
        need = _required_tech(wt.code)
        node_id = next((n for n, t in nodes.items() if need is None or t == need), next(iter(nodes)))
        tech = nodes[node_id]
        start, end = _pick_slot(day.date, now, body.slot, wt)
        is_repeat = body.is_repeat and wt.repeat_deadline_value is not None
        latest = _latest_start(start, end, wt.max_start_delay_min)
        deadline = _deadline(wt, now, latest, is_repeat)
        if latest + dt.timedelta(minutes=wt.duration_min) > deadline:
            raise ApiError(422, "DEADLINE_TOO_EARLY", "Контрольный срок наступает раньше конца визита в этом окне")
        order_id = _next_order_id(day, demo)
        order = {
            "order_id": order_id, "work_type_code": wt.code, "technology": tech, "building_id": b.building_id,
            "address": b.address, "lat": b.lat, "lon": b.lon, "building_type": b.building_type,
            "requires_height": b.building_type == "PRIVATE" and b.overhead_line, "access_node_id": node_id,
            "registered_at": now.isoformat(), "slot": {"start": start.isoformat(), "end": end.isoformat()},
            "latest_start": latest.isoformat(), "deadline": deadline.isoformat(),
            "required_equipment": _equipment(wt.code, tech), "is_repeat": is_repeat, "previous_order_id": None,
        }
        event = {"event_id": _next_event_id(day), "type": "NEW_ORDER", "timestamp": now.isoformat(), "order": order}
        raw = json.dumps(event, ensure_ascii=False).encode("utf-8")
        if body.preferred_staff_id:
            base = svc.planner
            svc.planner = _PlannerProxy(base, body.preferred_staff_id, order_id, report)
            try:
                res = svc.post_event(date, raw)
            finally:
                svc.planner = base
        else:
            res = svc.post_event(date, raw)
        if demo is not None:  # день проигрывателя: наряд нужен его триггерам/восстановлению
            demo.setdefault("order_info", {})[order_id] = order
        plan = svc.get_plan(date)

    route = next(((r, s) for r in plan.routes for s in r.stops if s.type == StopType.VISIT and s.order_id == order_id),
                 None)
    unassigned = next((u for u in plan.unassigned if u.order_id == order_id), None)
    group = next((g for g in plan.pgp_groups if order_id in g.order_ids), None)
    out: dict = {
        "accepted": res.accepted, "order_id": order_id, "order": order, "work_type_name": wt.name,
        "plan_version": res.plan_version, "as_of": _iso(res.as_of), "event": _event_item(res, event),
        "warnings": _warnings(res, order_id), "assignment": None, "unassigned": None, "pgp": None, "preferred": None,
    }
    if route is not None:
        r, s = route
        staff = ref.staff.get(r.staff_id)
        out["outcome"] = "ASSIGNED"
        out["assignment"] = {"staff_id": r.staff_id, "staff_name": _staff_name(r.staff_id, staff.display_name if staff else None),
                             "eta": _iso(s.arrival), "planned_start": _iso(s.start), "planned_end": _iso(s.end)}
    elif group is not None:
        out["outcome"] = "PGP"
        out["pgp"] = {"group_id": group.group_id, "access_node_id": group.access_node_id, "comment": group.comment,
                      "order_ids": list(group.order_ids),
                      "message": "Похоже на групповое повреждение (ПГП): наряд передан в станционное подразделение, "
                                 "выезд бригады не планируется"}
    else:
        out["outcome"] = "UNASSIGNED"
        if unassigned is not None:
            out["unassigned"] = unassigned.model_dump(mode="json")
    if body.preferred_staff_id:
        sid = body.preferred_staff_id
        st = ref.staff[sid]
        name = _staff_name(sid, st.display_name) or sid
        honored = out["assignment"] is not None and out["assignment"]["staff_id"] == sid
        if honored:
            extra = report.get("extra_travel_min")
            msg = f"Пожелание учтено: исполнитель — {name}" + (f" (в пути на {extra} мин больше, чем при автоназначении)"
                                                         if extra else "")
        elif group is not None:
            msg = "Пожелание не применяется: наряд ушёл в группу ПГП"
        elif report.get("reason"):
            msg = f"Пожелание не исполнено: {report['reason']}" + (
                "; назначен автоматически" if out["assignment"] else "")
        elif report.get("placed"):
            msg = "Пожелание не исполнено: вставка отклонена проверкой плана"
        else:
            msg = "Пожелание не проверено: не хватило бюджета расчёта события"
        out["preferred"] = {"staff_id": sid, "staff_name": name, "honored": honored, "message": msg}
    return out


@router.post("/{order_id}/cancel")
def cancel_order(order_id: str, request: Request) -> dict:
    svc, _demo_state, date = _require_day(request)
    with svc.store.lock:
        day = svc._day(dt.date.fromisoformat(date))
        if order_id not in day.orders:
            raise ApiError(404, "UNKNOWN_ORDER", f"Наряд {order_id} не найден в дне {date}")
        if order_id in day.cancelled:
            raise ApiError(409, "ORDER_ALREADY_CANCELLED", f"Наряд {order_id} уже отменён")
        now = day.model_time
        before = None
        for r in day.plan.routes:
            for s in r.stops:
                if s.type == StopType.VISIT and s.order_id == order_id:
                    before = (r.staff_id, s)
        if before is not None and before[1].start < now:
            sid, s = before
            ref = svc.store.reference
            name = _staff_name(sid, ref.staff[sid].display_name if sid in ref.staff else None) or sid
            done = s.end <= now
            raise ApiError(409, "VISIT_STARTED",
                           f"Визит уже {'выполнен' if done else 'начат'} ({name}, начало {s.start:%H:%M}): "
                           "по контракту отменить можно только наряд, визит по которому ещё не начат")
        event = {"event_id": _next_event_id(day), "type": "ORDER_CANCELLED", "timestamp": now.isoformat(),
                 "order_id": order_id, "reason": "CUSTOMER_REQUEST"}
        res = svc.post_event(date, json.dumps(event, ensure_ascii=False).encode("utf-8"))
        plan = svc.get_plan(date)
        ref = svc.store.reference
    cancelled = any(c.order_id == order_id for c in plan.cancelled)
    freed = None
    if before is not None:
        sid = before[0]
        freed = {"staff_id": sid, "staff_name": _staff_name(sid, ref.staff[sid].display_name if sid in ref.staff else None)}
    summary = f"{order_id} отменён"
    if freed:
        summary += f", маршрут {freed['staff_name'] or freed['staff_id']} пересчитан"
    if len(res.changed_staff_ids) > 1:
        summary += f"; изменено маршрутов: {len(res.changed_staff_ids)}"
    return {"accepted": res.accepted, "cancelled": cancelled, "order_id": order_id, "summary": summary,
            "freed": freed, "plan_version": res.plan_version, "as_of": _iso(res.as_of),
            "changed_staff_ids": res.changed_staff_ids, "message": res.message,
            "warnings": _warnings(res), "event": _event_item(res, event)}
