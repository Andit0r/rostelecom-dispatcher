"""Независимая самопроверка плана по жёстким ограничениям H1–H10 (data/api_spec.yaml, п. 3–4).

Не использует декодер и контекст планировщика: время берётся из Stop как есть,
дорога — из единой матрицы по location_id. validate_plan — H1–H9 одной версии,
validate_transition — H10 между последовательными версиями. Допуск сравнения
времени — 1 с, как у валидатора организаторов. Это самопроверка, не часть API.

H-J — наша дополнительная проверка совместных нарядов (JOINT_ORDERS, planner/joint.py):
визит напарника `ORD-…-B` начинается одновременно с основным визитом, у другого
сотрудника, который сам проходит H1 по основному наряду; длительность — норматив
(или момент из события по основному наряду); АО у напарника не списывается. Валидатор
организаторов про такие визиты не знает (см. docs/planner-notes.md, «Совместные наряды»).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..models import Plan, StopType
from .joint import is_joint_code, joint_enabled, main_of
from ..state import DayState, MatrixState, ReferenceState

TOL = 1  # с
TZ = dt.timezone(dt.timedelta(hours=5))


@dataclass(frozen=True)
class Violation:
    code: str  # H1..H9
    message: str
    order_id: str | None = None
    staff_id: str | None = None

    def __str__(self) -> str:
        who = " ".join(x for x in (self.staff_id, self.order_id) if x)
        return f"{self.code} [{who}] {self.message}"


def _sec(day: dt.date, moment: dt.datetime) -> float:
    return (moment - dt.datetime.combine(day, dt.time(0), tzinfo=TZ)).total_seconds()


def _hhmm(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 3600 + int(m) * 60


def validate_plan(
    plan: Plan,
    ref: ReferenceState,
    matrix: MatrixState,
    day: DayState,
    joint: bool | None = None,
) -> list[Violation]:
    """joint — режим совместных нарядов (None — из JOINT_ORDERS): при включённом каждый
    основной визит INST_PRIVATE_VLS должен иметь напарника (H-J)."""
    if joint is None:
        joint = joint_enabled()
    out: list[Violation] = []
    add = out.append
    idx = matrix.index
    main_visits: dict[str, list[tuple[str, object]]] = {}
    partner_visits: dict[str, list[tuple[str, object]]] = {}
    dur = matrix.matrix.durations_sec

    def tau(a: str, b: str) -> int | None:
        if a not in idx or b not in idx:
            return None
        return dur[idx[a]][idx[b]]

    on_shift = [s for s in ref.staff if s not in day.absent_staff_ids]
    seen_routes: dict[str, int] = {}
    visit_count: dict[str, int] = {}

    for route in plan.routes:
        sid = route.staff_id
        seen_routes[sid] = seen_routes.get(sid, 0) + 1
        staff = ref.staff.get(sid)
        if staff is None or sid in day.absent_staff_ids:
            add(Violation("H2", "маршрут сотрудника не на смене", staff_id=sid))
            continue
        stops = route.stops
        if not stops:
            continue
        S, F = _hhmm(staff.shift.start), _hhmm(staff.shift.end)
        bf, bt = _hhmm(staff.shift.break_window.from_), _hhmm(staff.shift.break_window.to)
        beta = staff.shift.break_duration_min * 60
        cap = staff.equipment_capacity
        inv = [staff.equipment_start.ONT, staff.equipment_start.ROUTER, staff.equipment_start.STB]

        for i, s in enumerate(stops, start=1):
            if s.seq != i:
                add(Violation("H6", f"seq {s.seq} вместо {i}", s.order_id, sid))
                break

        # --- структура и H8 для депо
        if stops[0].type != StopType.DEPOT_START:
            add(Violation("H8", "первая остановка не DEPOT_START", staff_id=sid))
        if stops[-1].type != StopType.DEPOT_END and route.status == "ACTIVE":
            add(Violation("H8", "последняя остановка не DEPOT_END", staff_id=sid))
        for s in stops:
            if s.type in (StopType.DEPOT_START, StopType.DEPOT_END):
                if s.location_id != staff.service_center_id:
                    add(Violation("H8", f"{s.type.value} не в своём СЦ ({s.location_id})", staff_id=sid))
                if not (s.arrival == s.start == s.end):
                    add(Violation("H6", f"{s.type.value}: arrival, start, end должны совпадать", staff_id=sid))
            if s.type == StopType.DEPOT_START and s is not stops[0]:
                add(Violation("H8", "DEPOT_START не первой остановкой", staff_id=sid))
            if s.type == StopType.DEPOT_END and s is not stops[-1]:
                add(Violation("H8", "DEPOT_END не последней остановкой", staff_id=sid))

        # --- H2
        first, last = stops[0], stops[-1]
        if first.type == StopType.DEPOT_START and _sec(day.date, first.start) < S - TOL:
            add(Violation("H2", "выезд раньше начала смены", staff_id=sid))
        if last.type == StopType.DEPOT_END and _sec(day.date, last.arrival) > F + TOL:
            add(Violation("H2", "возвращение позже конца смены", staff_id=sid))
        for s in stops:
            a, st, en = _sec(day.date, s.arrival), _sec(day.date, s.start), _sec(day.date, s.end)
            if a < S - TOL or en > F + TOL:
                add(Violation("H2", f"{s.type.value} вне смены", s.order_id, sid))

        # --- H6 (дорога, порядок времён)
        for j, s in enumerate(stops):
            a, st, en = _sec(day.date, s.arrival), _sec(day.date, s.start), _sec(day.date, s.end)
            if st < a - TOL or en < st - TOL:
                add(Violation("H6", f"{s.type.value}: нарушен порядок arrival ≤ start ≤ end", s.order_id, sid))
            if j == 0:
                continue
            prev = stops[j - 1]
            t = tau(prev.location_id, s.location_id)
            if t is None:
                add(Violation("H6", f"точка {s.location_id} или {prev.location_id} не в матрице", s.order_id, sid))
                continue
            if s.type == StopType.BREAK:
                if s.location_id != prev.location_id:
                    add(Violation("H3", "BREAK не на месте предыдущей остановки", staff_id=sid))
            if a < _sec(day.date, prev.end) + t - TOL:
                add(Violation("H6", f"{s.type.value}: прибытие раньше, чем позволяет матрица", s.order_id, sid))

        # --- H3
        visits = [s for s in stops if s.type == StopType.VISIT]
        breaks = [s for s in stops if s.type == StopType.BREAK]
        if visits:
            # Допущение этапа B: у выбывшего перерыв не обязателен, если остаток окна перерыва
            # после выбытия целиком попал в недоступность (until = null или until > to).
            exempt = (
                route.status == "UNAVAILABLE"
                and (route.unavailable_until is None or _sec(day.date, route.unavailable_until) > bt)
            )
            if len(breaks) > 1 or (len(breaks) == 0 and not exempt):
                add(Violation("H3", f"перерывов {len(breaks)} вместо 1", staff_id=sid))
            for b in breaks:
                st, en = _sec(day.date, b.start), _sec(day.date, b.end)
                if en - st < beta - TOL:
                    add(Violation("H3", "перерыв короче нормы", staff_id=sid))
                if st < bf - TOL or st > bt + TOL:
                    add(Violation("H3", "начало перерыва вне break_window", staff_id=sid))

        # --- H1, H4, H5, H7, H8 (RESTOCK)
        for s in stops:
            if s.type == StopType.VISIT:
                oid = s.order_id
                order = day.orders.get(oid) if oid else None
                main = main_of(oid, day.orders) if order is None else None
                if main is not None:
                    # визит напарника: проверяется в H-J, АО не списывается
                    partner_visits.setdefault(main, []).append((sid, s))
                    continue
                if order is not None and is_joint_code(order.work_type_code):
                    main_visits.setdefault(oid, []).append((sid, s))
                if order is None:
                    add(Violation("H9", "визит по неизвестному наряду", oid, sid))
                    continue
                visit_count[oid] = visit_count.get(oid, 0) + 1
                wt = ref.work_types[order.work_type_code]
                if s.location_id != order.building_id:
                    add(Violation("H6", "location_id визита не совпадает с building_id", oid, sid))
                if not set(staff.profiles) & set(wt.profiles):
                    add(Violation("H1", "нет допустимого профиля", oid, sid))
                if order.technology not in staff.technologies:
                    add(Violation("H1", f"нет технологии {order.technology}", oid, sid))
                if order.requires_height and not staff.height_permit:
                    add(Violation("H1", "нет допуска к высоте", oid, sid))
                st = _sec(day.date, s.start)
                if st < _sec(day.date, order.slot.start) - TOL or st > _sec(day.date, order.latest_start) + TOL:
                    add(Violation("H4", "начало визита вне [slot.start, latest_start]", oid, sid))
                override = getattr(day, "visit_end_overrides", {}).get(oid)
                if override is not None:
                    if abs((s.end - override).total_seconds()) > TOL:
                        add(Violation("H5", "конец визита не равен моменту из события", oid, sid))
                elif abs((s.end - s.start).total_seconds() - wt.duration_min * 60) > TOL:
                    add(Violation("H5", "длительность визита не равна нормативу", oid, sid))
                req = order.required_equipment
                inv = [inv[0] - req.ONT, inv[1] - req.ROUTER, inv[2] - req.STB]
                if min(inv) < 0:
                    add(Violation("H7", f"остаток АО отрицательный: {inv}", oid, sid))
            elif s.type == StopType.RESTOCK:
                sc = ref.service_centers.get(s.location_id)
                if sc is None:
                    add(Violation("H8", f"RESTOCK не в СЦ ({s.location_id})", staff_id=sid))
                    continue
                st, en = _sec(day.date, s.start), _sec(day.date, s.end)
                if st < _hhmm(sc.opening_hours.open) - TOL or en > _hhmm(sc.opening_hours.close) + TOL:
                    add(Violation("H8", f"RESTOCK в {sc.service_center_id} вне режима работы", staff_id=sid))
                if en - st < sc.restock_duration_min * 60 - TOL:
                    add(Violation("H8", "RESTOCK короче restock_duration_min", staff_id=sid))
                inv = [cap.ONT, cap.ROUTER, cap.STB]
            elif s.order_id:
                add(Violation("H6", f"order_id у остановки {s.type.value}", s.order_id, sid))

    _check_joint(main_visits, partner_visits, ref, day, joint, add)

    for sid in on_shift:
        if seen_routes.get(sid, 0) != 1:
            add(Violation("H2", f"маршрутов у сотрудника на смене: {seen_routes.get(sid, 0)}", staff_id=sid))

    # --- H9: каждый наряд ровно в одном месте
    places: dict[str, list[str]] = {}
    for oid, n in visit_count.items():
        places.setdefault(oid, []).extend(["route"] * n)
    for u in plan.unassigned:
        places.setdefault(u.order_id, []).append("unassigned")
    for g in plan.pgp_groups:
        for oid in g.order_ids:
            places.setdefault(oid, []).append("pgp")
    for c in plan.cancelled:
        places.setdefault(c.order_id, []).append("cancelled")
    for oid in day.orders:
        where = places.get(oid, [])
        if len(where) != 1:
            add(Violation("H9", f"наряд учтён {len(where)} раз: {where}", oid))
    for oid in places:
        if oid not in day.orders:
            add(Violation("H9", "в плане неизвестный наряд", oid))
    return out


def _check_joint(main_visits, partner_visits, ref: ReferenceState, day: DayState, joint: bool, add) -> None:
    """H-J: пары «основной визит — визит напарника»."""
    overrides = getattr(day, "visit_end_overrides", {})
    for main, partners in partner_visits.items():
        order = day.orders[main]
        wt = ref.work_types[order.work_type_code]
        mains = main_visits.get(main, [])
        if not is_joint_code(order.work_type_code):
            add(Violation("H-J", "напарник у наряда, который не выполняется совместно", main))
        if len(partners) != 1:
            add(Violation("H-J", f"визитов напарника {len(partners)} вместо 1", main))
        if len(mains) != 1:
            add(Violation("H-J", "визит напарника без основного визита", main, partners[0][0]))
            continue
        msid, ms = mains[0]
        for psid, ps in partners:
            if psid == msid:
                add(Violation("H-J", "напарник — тот же сотрудник", main, psid))
            staff = ref.staff.get(psid)
            if staff is None:
                continue
            if not set(staff.profiles) & set(wt.profiles) or order.technology not in staff.technologies:
                add(Violation("H-J", "напарник не проходит H1 по профилю/технологии", main, psid))
            if order.requires_height and not staff.height_permit:
                add(Violation("H-J", "у напарника нет допуска к высоте", main, psid))
            if ps.location_id != order.building_id:
                add(Violation("H-J", "напарник не по адресу наряда", main, psid))
            if abs((ps.start - ms.start).total_seconds()) > TOL:
                add(Violation("H-J", "начало визита напарника не совпадает с основным", main, psid))
            pid = ps.order_id
            override = overrides.get(pid)
            if override is not None:
                if abs((ps.end - override).total_seconds()) > TOL:
                    add(Violation("H-J", "конец визита напарника не равен моменту из события", main, psid))
            elif abs((ps.end - ps.start).total_seconds() - wt.duration_min * 60) > TOL:
                add(Violation("H-J", "длительность визита напарника не равна нормативу", main, psid))
    if joint:
        for main, mains in main_visits.items():
            if main not in partner_visits:
                add(Violation("H-J", "совместный наряд без напарника", main, mains[0][0]))


# ---------------------------------------------------------------- H10

def _key(stop) -> tuple:
    if stop.type == StopType.VISIT:
        return ("VISIT", stop.order_id)
    if stop.type == StopType.RESTOCK:
        return ("RESTOCK", stop.location_id)
    return (stop.type.value,)


def validate_transition(
    prev: Plan,
    curr: Plan,
    ref: ReferenceState,
    matrix: MatrixState,
    day: DayState,
    t: dt.datetime,
    event=None,
) -> list[Violation]:
    """H10 между версиями prev -> curr в модельный момент t (раздел 3 спецификации).

    * остановка prev со start < t — та же в curr (сотрудник, позиция, тип, наряд, location_id,
      arrival, start); end меняется только событием VISIT_DELAYED / ORDER_TRANSFERRED по ней;
    * незамороженная остановка curr начинается не раньше t;
    * новая остановка или остановка с изменившимся предшественником:
      arrival ≥ max(end предшественника, t) + время в пути;
    * визит по наряду из NEW_ORDER начинается не раньше момента его поступления;
    * отменённый наряд не посещается; у выбывшего нет остановок, начинающихся в [from, until).
    """
    out: list[Violation] = []
    add = out.append
    idx = matrix.index
    dur = matrix.matrix.durations_sec
    D = day.date
    tt = _sec(D, t)
    end_events: dict[str, dt.datetime] = {}
    if event is not None and getattr(event, "type", None) in ("VISIT_DELAYED", "ORDER_TRANSFERRED"):
        end_events[event.order_id] = event.expected_end if event.type == "VISIT_DELAYED" else event.actual_end
        # визит напарника совместного наряда заканчивается вместе с основным (planner/joint.py)
        end_events[event.order_id + "-B"] = end_events[event.order_id]

    prev_routes = {r.staff_id: r for r in prev.routes}
    curr_routes = {r.staff_id: r for r in curr.routes}
    for sid, pr in prev_routes.items():
        frozen = [s for s in pr.stops if _sec(D, s.start) < tt - TOL]
        if not frozen:
            continue
        cr = curr_routes.get(sid)
        if cr is None or len(cr.stops) < len(frozen):
            add(Violation("H10", "начатые остановки пропали из маршрута", staff_id=sid))
            continue
        for a, b in zip(frozen, cr.stops):
            same = (a.type == b.type and a.order_id == b.order_id and a.location_id == b.location_id
                    and abs((a.arrival - b.arrival).total_seconds()) <= TOL
                    and abs((a.start - b.start).total_seconds()) <= TOL)
            if not same:
                add(Violation("H10", f"изменена начатая остановка {a.type.value}", a.order_id, sid))
                continue
            if abs((a.end - b.end).total_seconds()) > TOL:
                moment = end_events.get(a.order_id) if a.type == StopType.VISIT else None
                if moment is None or abs((b.end - moment).total_seconds()) > TOL:
                    add(Violation("H10", "end начатой остановки изменён без события", a.order_id, sid))

    for sid, cr in curr_routes.items():
        pr = prev_routes.get(sid)
        prev_pred: dict[tuple, tuple | None] = {}
        n_frozen = 0
        if pr is not None:
            for j, s in enumerate(pr.stops):
                prev_pred[_key(s)] = _key(pr.stops[j - 1]) if j else None
            n_frozen = sum(1 for s in pr.stops if _sec(D, s.start) < tt - TOL)
        for j, s in enumerate(cr.stops):
            if j < n_frozen:
                continue
            st = _sec(D, s.start)
            if st < tt - TOL:
                add(Violation("H10", f"{s.type.value} начинается раньше модельного времени", s.order_id, sid))
            k = _key(s)
            pred = cr.stops[j - 1] if j else None
            pk = _key(pred) if pred is not None else None
            if k in prev_pred and prev_pred[k] == pk:
                continue  # прежняя остановка с прежним предшественником
            arr = _sec(D, s.arrival)
            if pred is None:
                if arr < tt - TOL:
                    add(Violation("H10", f"новая {s.type.value}: прибытие раньше t", s.order_id, sid))
                continue
            if pred.location_id in idx and s.location_id in idx:
                tr = dur[idx[pred.location_id]][idx[s.location_id]]
                if arr < max(_sec(D, pred.end), tt) + tr - TOL:
                    add(Violation("H10", f"{s.type.value}: прибытие раньше max(end предшественника, t) + дорога",
                                  s.order_id, sid))

    cancelled = {c.order_id for c in curr.cancelled}
    for cr in curr.routes:
        for s in cr.stops:
            if s.type != StopType.VISIT:
                continue
            if s.order_id in cancelled:
                add(Violation("H10", "визит по отменённому наряду", s.order_id, cr.staff_id))
            came = day.arrived_at.get(s.order_id)
            if came is not None and came > day.now and s.start < came - dt.timedelta(seconds=TOL):
                add(Violation("H10", "визит по NEW_ORDER раньше его поступления", s.order_id, cr.staff_id))
        if cr.status == "UNAVAILABLE" and cr.unavailable_from is not None:
            lo = _sec(D, cr.unavailable_from)
            hi = _sec(D, cr.unavailable_until) if cr.unavailable_until is not None else float("inf")
            for s in cr.stops:
                st = _sec(D, s.start)
                if lo <= st < hi - TOL:
                    add(Violation("H10", f"{s.type.value} в период недоступности", s.order_id, cr.staff_id))
    if event is not None and getattr(event, "type", None) == "STAFF_UNAVAILABLE":
        r = curr_routes.get(event.staff_id)
        if r is not None and r.status != "UNAVAILABLE":
            add(Violation("H10", "выбывший сотрудник не помечен UNAVAILABLE", staff_id=event.staff_id))
    return out
