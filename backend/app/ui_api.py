"""Вспомогательный API для интерфейса диспетчера (/ui), вне контракта /api/v1.

Отдаёт наряды со статусами и справочники без матрицы, создаёт пустой день («Создать день»),
пересобирает активный день («Пересобрать») и очищает его. Служебные `/ui/demo/*` (загрузка дня
из каталога data/synth и проигрыватель сценария) нужны тестам и скриптам, интерфейс их не использует.
Состояние — тот же in-memory Store сервиса; вызовы идут через DispatchService.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import catalog
from .errors import ApiError

ROOT = Path(__file__).resolve().parents[2]
# Каталог файлов хранилища без БД (staff_directory.json, auth_accounts.json) и служебного каталога данных.
DATA = Path(os.environ.get("UI_DATA_DIR", ROOT / "data" / "synth"))
TZ = dt.timezone(dt.timedelta(hours=5))
SCENARIOS = catalog.SCENARIOS

router = APIRouter(prefix="/ui", tags=["ui"])


def _principal(request: Request):
    from . import auth
    return auth.current_principal(request)


# ---------------------------------------------------------------- данные

def _effective_reference_bytes() -> bytes | None:
    """Справочники из Store (исходные, как пришли в PUT reference или из каталога) с рюкзаками АО из
    справочника сотрудников (переопределения диспетчера поверх). Справочников нет — None."""
    from . import reference_sync
    from .staff_directory import directory  # staff_directory импортирует этот модуль
    ref = reference_sync.base_reference()
    if ref is None:
        return None

    def dump(items):
        return [x.model_dump(mode="json", by_alias=True) for x in items]

    payload = {"service_centers": dump(ref.service_centers.values()), "staff": dump(ref.staff.values()),
               "access_nodes": dump(ref.access_nodes.values()), "work_types": dump(ref.work_types.values())}
    try:
        payload["staff"] = directory().apply_equipment(payload["staff"])
    except Exception:  # справочник сотрудников не должен ломать загрузку/пересборку дня
        pass
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _orders_train() -> list[dict]:
    return catalog.orders_train()


def _scenario(name: str) -> dict:
    return catalog.scenario(name)


def _ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def _plan_time_limit() -> int:
    """time_limit_sec для POST /plan так, чтобы внутренний бюджет был ≈ UI_PLAN_BUDGET_SEC
    (планировщик берёт min(PLANNER_DAY_BUDGET_SEC, 0.8·limit − 3))."""
    from . import runtime_settings
    budget = float(runtime_settings.get("UI_PLAN_BUDGET_SEC"))
    return max(1, min(300, math.ceil((budget + 3.0) / 0.8)))


# ---------------------------------------------------------------- helpers

def _staff_name(staff_id: str | None, fallback: str | None) -> str | None:
    from .staff_directory import display_name  # staff_directory импортирует этот модуль
    return display_name(staff_id, fallback)


def _service(request: Request):
    return request.app.state.service


def _demo(request: Request) -> dict | None:
    return getattr(request.app.state, "ui_demo", None)


def _visits(plan) -> dict[str, tuple[str, object]]:
    return {s.order_id: (r.staff_id, s) for r in plan.routes for s in r.stops if s.type == "VISIT"}


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _next_body(svc, demo: dict) -> dict | None:
    """Следующее сообщение сценария: сработавший триггер (если он не позже ближайшего
    события) или само событие; когда события кончились — оставшиеся триггеры."""
    plan = svc.get_plan(demo["date"])
    vis = _visits(plan)
    cands = []
    for tr in demo["triggers"]:
        if tr["order_id"] in vis:
            sid, stop = vis[tr["order_id"]]
            cands.append((stop.start + dt.timedelta(minutes=tr["fire_after_start_min"]), tr, sid, stop))
    next_event = demo["events"][0] if demo["events"] else None
    if cands:
        at, tr, sid, stop = min(cands, key=lambda c: c[0])
        if next_event is None or at <= _ts(next_event["timestamp"]):
            at = max(at, demo["last_ts"])
            body = {"event_id": tr["trigger_id"], "type": tr["type"], "timestamp": at.isoformat(),
                    "staff_id": sid, "order_id": tr["order_id"]}
            if tr["type"] == "VISIT_DELAYED":
                ref = svc.store.reference
                wt = ref.work_types[demo["order_info"][tr["order_id"]]["work_type_code"]]
                end = stop.start + dt.timedelta(minutes=wt.duration_min + (tr.get("extra_duration_min") or 0))
                body["expected_end"] = end.isoformat()
            else:
                body["actual_end"] = at.isoformat()
                body["transferred_to"] = tr.get("transferred_to") or "STATION_EQUIPMENT"
            demo["triggers"].remove(tr)
            demo["last_ts"] = at
            return body
    if next_event is not None:
        demo["events"].pop(0)
        demo["last_ts"] = max(demo["last_ts"], _ts(next_event["timestamp"]))
        return next_event
    if demo["triggers"]:  # триггеры по нарядам, которых нет в маршрутах, пропускаются
        demo["skipped"] += len(demo["triggers"])
        demo["triggers"].clear()
    return None


def _demo_status(svc, demo: dict | None) -> dict:
    if demo is None:
        return {"loaded": False}
    try:
        plan = svc.get_plan(demo["date"])
    except ApiError:  # день проигрывателя стёрт (PUT reference/days внешним клиентом)
        return {"loaded": False}
    return {
        "loaded": True,
        "date": demo["date"],
        "scenario": demo["scenario"],
        "now": demo["now"],
        "as_of": _iso(plan.as_of),
        "plan_version": plan.plan_version,
        "remaining_events": len(demo["events"]),
        "remaining_triggers": len(demo["triggers"]),
        "skipped_triggers": demo["skipped"],
    }


def _persist_demo(svc, demo: dict) -> None:
    """Состояние проигрывателя в БД (write-behind; без БД ничего не делает)."""
    from .storage import state_repo
    with svc.store.lock:
        day = svc.store.days.get(dt.date.fromisoformat(demo["date"]))
        if day is not None:
            state_repo.save_demo(day.date, day.load_id, demo)


def restore_demo(app, saved: dict) -> None:
    """Проигрыватель /ui/demo/* после рестарта (storage/restore.py): order_info строится заново —
    из нарядов дня в памяти и, если доступны, файлов демо (как в demo_load)."""
    svc = app.state.service
    day_date = dt.date.fromisoformat(saved["date"])
    with svc.store.lock:
        day = svc.store.days.get(day_date)
        if day is None or day.plan is None:
            return
        order_info = {oid: o.model_dump(mode="json", by_alias=True) for oid, o in day.orders.items()}
    try:
        for o in _orders_train():
            if o["slot"]["start"][:10] == saved["date"]:
                order_info.setdefault(o["order_id"], o)
        if saved.get("scenario") in SCENARIOS:
            for e in _scenario(saved["scenario"])["events"]:
                if e["type"] == "NEW_ORDER":
                    order_info.setdefault(e["order"]["order_id"], e["order"])
    except (OSError, ValueError, KeyError):
        pass  # нет файлов демо: хватит нарядов дня
    app.state.ui_demo = {
        "date": saved["date"],
        "scenario": saved.get("scenario"),
        "now": saved["now"],
        "events": list(saved.get("events") or []),
        "triggers": list(saved.get("triggers") or []),
        "order_info": order_info,
        "last_ts": _ts(saved["last_ts"]) if saved.get("last_ts") else day.now,
        "skipped": int(saved.get("skipped") or 0),
    }


# ---------------------------------------------------------------- routes

def _apply_overrides(svc) -> None:
    """Под store.lock: справочник в Store с рюкзаками АО из справочника сотрудников (если отличаются).
    Исходный справочник (reference_sync.base_reference) остаётся прежним."""
    from .storage import state_repo
    raw = _effective_reference_bytes()
    if raw is None:
        return
    ref = svc.load_reference(raw)
    cur = svc.store.reference
    if cur is not None and all(s.model_dump() == cur.staff[sid].model_dump() for sid, s in ref.staff.items()
                               if sid in cur.staff):
        return
    svc.store.reference = ref
    svc.store.ref_gen = uuid.uuid4().hex
    state_repo.save_reference(raw, svc.store.ref_gen)
    svc.store.notify_reference(ref, derived=True)


def _load_catalog_reference(request: Request) -> None:
    """Служебный путь: справочники и матрица из каталога (catalog.py, без нарядов и событий) — как PUT reference
    и PUT travel-matrix; новые записи справочника сотрудников получают ФИО-заготовку staff_names.json."""
    from . import reference_sync
    svc = _service(request)
    with reference_sync.names_hint(catalog.staff_names()):
        svc.put_reference(catalog.reference_bytes())
    svc.put_travel_matrix(catalog.matrix_bytes())
    with svc.store.lock:
        _apply_overrides(svc)  # переопределения рюкзаков диспетчера (дней ещё нет)
    request.app.state.ui_ref_catalog_gen = svc.store.ref_gen


@router.get("/demo/load", summary="Служебный: загрузить день из каталога данных (не используется интерфейсом)",
            description="Служебный, не используется интерфейсом: день из data/synth + план; нужен тестам и скриптам.")
def demo_load(
    request: Request,
    date: str | None = None,
    scenario: str = Query("dense", pattern="^(dense|example)$"),
) -> dict:
    svc = _service(request)
    sc = _scenario(scenario)
    day = date or sc["date"]
    try:
        dt.date.fromisoformat(day)
    except ValueError:
        raise ApiError(400, "BAD_REQUEST", f"Некорректная дата '{day}', ожидается YYYY-MM-DD") from None
    with_events = day == sc["date"]
    now = _ts(sc["now"]) if with_events else dt.datetime.combine(
        dt.date.fromisoformat(day), dt.time(8, 30), tzinfo=TZ)

    day_orders = [o for o in _orders_train() if o["slot"]["start"][:10] == day]
    loaded = [o for o in day_orders if _ts(o["registered_at"]) <= now]
    if not day_orders:
        raise ApiError(404, "DAY_NOT_FOUND", f"В orders_train.json нет нарядов на {day}")

    _load_catalog_reference(request)
    svc.put_day(day, json.dumps({"date": day, "now": now.isoformat(), "orders": loaded},
                                ensure_ascii=False).encode("utf-8"))
    plan = svc.build_plan(day, json.dumps({"time_limit_sec": _plan_time_limit()}).encode())

    order_info = {o["order_id"]: o for o in day_orders}
    events = sorted(sc["events"], key=lambda e: _ts(e["timestamp"])) if with_events else []
    for e in events:
        if e["type"] == "NEW_ORDER":
            order_info[e["order"]["order_id"]] = e["order"]
    request.app.state.ui_demo = {
        "date": day,
        "scenario": scenario if with_events else None,
        "now": now.isoformat(),
        "events": events,
        "triggers": list(sc["triggers"]) if with_events else [],
        "order_info": order_info,
        "last_ts": now,
        "skipped": 0,
    }
    _persist_demo(svc, request.app.state.ui_demo)
    return {
        "date": day,
        "now": now.isoformat(),
        "status": "loaded",
        "plan_version": plan.plan_version,
        "orders": len(loaded),
        "computation_ms": plan.computation_ms,
        "demo": _demo_status(svc, request.app.state.ui_demo),
    }


@router.get("/settings")
def settings_get() -> dict:
    from . import runtime_settings
    return {"settings": runtime_settings.describe()}


@router.put("/settings")
def settings_put(body: dict = Body(...), principal=Depends(_principal)) -> dict:
    from . import runtime_settings
    if not body:
        raise ApiError(422, "VALIDATION_ERROR", "Пустой запрос: укажите хотя бы одну настройку")
    try:
        changed = runtime_settings.update(body, who=principal.login)
    except ValueError as exc:
        raise ApiError(422, "VALIDATION_ERROR", str(exc)) from None
    return {"settings": runtime_settings.describe(), "changed": changed,
            "plan_outdated": runtime_settings.plan_outdated(changed)}


@router.post("/settings/reset")
def settings_reset(principal=Depends(_principal)) -> dict:
    from . import runtime_settings
    changed = runtime_settings.reset(who=principal.login)
    return {"settings": runtime_settings.describe(), "changed": changed,
            "plan_outdated": runtime_settings.plan_outdated(changed)}


@router.get("/demo/status")
def demo_status(request: Request) -> dict:
    return _demo_status(_service(request), _demo(request))


# Сигнатуры дней, увиденные опросом /ui/plan/status: date -> (load_id, plan_version), и «активный» день.
_STATUS_LOCK = threading.Lock()
_STATUS_SEEN: dict = {"sig": {}, "active": None}


def _active_state(request: Request) -> tuple[dt.date | None, dict]:
    """(активный день, {date: (load_id, plan_version, as_of, orders_total)} дней с планом).

    Активный день — тот, что изменился последним (PUT days / POST plan / события от внешнего
    клиента); при первом обращении — день проигрывателя /ui/demo, иначе самый поздний с планом.
    """
    svc = _service(request)
    demo = _demo(request)
    with svc.store.lock:
        days = {} if svc.store.reference is None else {
            d: (s.load_id, s.plan.plan_version, s.plan.as_of, len(s.orders))
            for d, s in svc.store.days.items() if s.plan is not None}
    with _STATUS_LOCK:
        seen = _STATUS_SEEN
        first = not seen["sig"]
        changed = [d for d, v in days.items() if seen["sig"].get(d) != v[:2]]
        seen["sig"] = {d: v[:2] for d, v in days.items()}
        demo_day = dt.date.fromisoformat(demo["date"]) if demo else None
        if changed and not first:
            seen["active"] = max(changed)
        elif seen["active"] not in days:
            seen["active"] = demo_day if demo_day in days else (max(days) if days else None)
        active = seen["active"]
    return active, days


def active_day(request: Request) -> dt.date | None:
    """Активный день (как в /ui/plan/status) или None, если дня с планом нет."""
    return _active_state(request)[0]


def require_active_day(request: Request) -> dt.date:
    day = active_day(request)
    if day is None:
        raise ApiError(409, "PLAN_NOT_BUILT",
                       "День не загружен: загрузите его (PUT /api/v1/days + POST plan) или «Создать день»")
    return day


def demo_for(request: Request, day: dt.date | str) -> dict | None:
    """Состояние проигрывателя, если он ведёт именно этот день; иначе None."""
    demo = _demo(request)
    key = day.isoformat() if isinstance(day, dt.date) else day
    return demo if demo is not None and demo.get("date") == key else None


@router.get("/plan/status")
def plan_status(request: Request) -> dict:
    """Дешёвое состояние плана для автообновления интерфейса (ничего не меняет в плане)."""
    active, days = _active_state(request)
    if active is None:
        return {"loaded": False, "date": None, "plan_version": None, "as_of": None,
                "orders_total": None, "load_id": None}
    load_id, version, as_of, total = days[active]
    return {"loaded": True, "date": active.isoformat(), "plan_version": version, "as_of": _iso(as_of),
            "orders_total": total, "load_id": load_id}


@router.post("/day/reset")
def day_reset(request: Request, with_reference: bool = Query(False),
              date: str | None = Query(None, description="YYYY-MM-DD: очистить только этот день"),
              principal=Depends(_principal)) -> dict:
    """«Очистить день».

    Без `date`: из памяти удаляются все дни, планы, события, отметки и проигрыватель /ui/demo. Справочники и
    матрица остаются (with_reference=true — сбрасываются и они). В БД (write-behind) удаляются снимки day_state и
    отметки текущих поколений — после рестарта дни не восстановятся; история journal остаётся. Идемпотентно.

    С `date`: удаляется только этот день (его план, события, отметки, режимы, проигрыватель, если он вёл этот
    день; в БД — снимок day_state этой даты и отметки его load_id). Другие дни и справочники не трогаются; если
    день был активным, активным становится самый поздний из оставшихся (или «нет дня»). Дня нет в памяти —
    404 DAY_NOT_FOUND (повтор тоже 404: удалять нечего). with_reference вместе с date — 400.
    Действие пишется в actions (middleware /ui POST)."""
    from .storage import state_repo
    svc = _service(request)
    if date is not None:
        if with_reference:
            raise ApiError(400, "BAD_REQUEST", "with_reference=true нельзя сочетать с date: справочники общие для всех дней")
        try:
            day_date = dt.date.fromisoformat(date)
        except ValueError:
            raise ApiError(400, "BAD_REQUEST", f"Некорректная дата '{date}', ожидается YYYY-MM-DD") from None
        with svc.store.lock:
            day = svc.store.days.get(day_date)
            if day is None:
                raise ApiError(404, "DAY_NOT_FOUND", f"День {day_date} не загружен: очищать нечего")
            svc.store.days.pop(day_date)
            modes = getattr(svc.planner, "_day_modes", None)
            if isinstance(modes, dict):
                modes.pop(day_date, None)
            if demo_for(request, day_date) is not None:
                request.app.state.ui_demo = None
            box = getattr(request.app.state, "employee_marks", None)
            if isinstance(box, dict) and isinstance(box.get("days"), dict):
                for key in [k for k in box["days"] if k[0] == day_date]:
                    box["days"].pop(key, None)
            state_repo.save_reset([(day_date, day.load_id)], False, getattr(principal, "login", None),
                                  only_date=day_date)
        with _STATUS_LOCK:
            _STATUS_SEEN["sig"].pop(day_date, None)
            if _STATUS_SEEN["active"] == day_date:
                _STATUS_SEEN["active"] = None  # следующий опрос выберет самый поздний из оставшихся
        active = active_day(request)
        return {"status": "reset", "days": [day_date.isoformat()], "with_reference": False,
                "reference_loaded": svc.store.reference is not None,
                "remaining_days": sorted(d.isoformat() for d in svc.store.days),
                "active_date": active.isoformat() if active else None}
    with svc.store.lock:
        dropped = [(d, s.load_id) for d, s in sorted(svc.store.days.items())]
        svc.store.days.clear()
        modes = getattr(svc.planner, "_day_modes", None)
        if isinstance(modes, dict):
            modes.clear()
        if with_reference:
            svc.store.reference = None
            svc.store.matrix = None
            svc.store.ref_gen = None
        request.app.state.ui_demo = None
        request.app.state.employee_marks = None
        request.app.state.employee_mark_reverts = []
        state_repo.save_reset(dropped, with_reference, getattr(principal, "login", None))
    with _STATUS_LOCK:
        _STATUS_SEEN["sig"] = {}
        _STATUS_SEEN["active"] = None
    return {"status": "reset", "days": [d.isoformat() for d, _ in dropped], "with_reference": with_reference,
            "reference_loaded": svc.store.reference is not None, "remaining_days": [], "active_date": None}


def _mark_active(date: dt.date, day) -> None:
    with _STATUS_LOCK:
        _STATUS_SEEN["sig"][date] = (day.load_id, day.plan.plan_version)
        _STATUS_SEEN["active"] = date


class DayCreate(BaseModel):
    date: str | None = Field(None, description="YYYY-MM-DD; по умолчанию сегодня (часы сервера, Екатеринбург)")
    now: str | None = Field(None, description="ISO-время начала дня; по умолчанию см. описание эндпоинта")


def _earliest_shift_start(ref) -> dt.time:
    starts = [s.shift.start for s in ref.staff.values()]
    return dt.time.fromisoformat(min(starts)) if starts else dt.time(8, 0)


@router.post("/day/create")
def day_create(request: Request, body: DayCreate = Body(default_factory=DayCreate)) -> dict:
    """«Создать день»: пустой день на дату (без нарядов) и пустой план (версия 1), день становится активным.

    date — по умолчанию сегодня (часы сервера в часовом поясе Екатеринбурга). now — модельное время:
    для сегодняшней даты текущее время, округлённое вниз до 5 минут и не раньше начала самой ранней смены;
    иначе начало самой ранней смены. Справочники нужны: если их нет, они подгружаются из служебного каталога
    (catalog.py: только справочники и матрица, без нарядов) так же, как PUT reference; `reference_loaded_from` —
    catalog|existing.
    День на эту дату уже есть — 409 DAY_EXISTS. Наряды вносятся формой «Новый наряд» (NEW_ORDER).
    """
    svc = _service(request)
    today = dt.datetime.now(TZ)
    try:
        day_date = dt.date.fromisoformat(body.date) if body.date else today.date()
    except ValueError:
        raise ApiError(400, "BAD_REQUEST", f"Некорректная дата '{body.date}', ожидается YYYY-MM-DD") from None
    with svc.store.lock:
        if day_date in svc.store.days:
            raise ApiError(409, "DAY_EXISTS", "День уже существует; очистите день")
        loaded_from = "existing"
        if svc.store.reference is None or svc.store.matrix is None:
            try:
                if svc.store.reference is None:
                    _load_catalog_reference(request)
                else:
                    svc.put_travel_matrix(catalog.matrix_bytes())
            except OSError:
                raise ApiError(409, "REFERENCE_NOT_LOADED",
                               "Справочники не загружены (PUT /api/v1/reference), и каталога по умолчанию нет") from None
            loaded_from = "catalog"
        floor = dt.datetime.combine(day_date, _earliest_shift_start(svc.store.reference), tzinfo=TZ)
        if body.now:
            try:
                now = dt.datetime.fromisoformat(body.now)
            except ValueError:
                raise ApiError(400, "BAD_REQUEST", f"Некорректное время '{body.now}', ожидается ISO 8601") from None
            if now.tzinfo is None:
                now = now.replace(tzinfo=TZ)
        elif day_date == today.date():
            now = max(today.replace(minute=today.minute // 5 * 5, second=0, microsecond=0), floor)
        else:
            now = floor
        svc.put_day(day_date.isoformat(), json.dumps(
            {"date": day_date.isoformat(), "now": now.isoformat(), "orders": []}).encode("utf-8"))
        try:
            plan = svc.build_plan(day_date.isoformat(), json.dumps({"time_limit_sec": _plan_time_limit()}).encode())
        except Exception:
            svc.store.days.pop(day_date, None)
            raise
        _mark_active(day_date, svc.store.days[day_date])
    return {"status": "created", "date": day_date.isoformat(), "now": now.isoformat(),
            "plan_version": plan.plan_version, "orders": 0, "computation_ms": plan.computation_ms,
            "reference_loaded_from": loaded_from}


@router.post("/day/rebuild")
def day_rebuild(request: Request) -> dict:
    """«Пересобрать»: план активного дня строится с нуля по его собственным входным данным (наряды дня,
    включая добавленные вручную, без отменённых; время начала и отсутствующие сотрудники как при загрузке),
    с текущими режимами и рюкзаками АО из справочника сотрудников. Как PUT days + POST plan: новое
    поколение дня, версия плана 1, прежние события дня забываются. Не удалась — прежний день остаётся."""
    from .storage import state_repo
    svc = _service(request)
    active = require_active_day(request)
    with svc.store.lock:
        old = svc.store.days.get(active)
        if old is None or old.plan is None:
            raise ApiError(409, "PLAN_NOT_BUILT", "День не загружен: создайте день или загрузите его (PUT /api/v1/days)")
        old_ref, old_gen = svc.store.reference, svc.store.ref_gen
        orders = [o.model_dump(mode="json", by_alias=True) for o in old.pending_orders()]
        body = {"date": active.isoformat(), "now": old.now.isoformat(), "orders": orders,
                "absent_staff_ids": sorted(old.absent_staff_ids)}
        # справочники из Store (исходные, как пришли) с переопределениями рюкзаков поверх
        raw_ref = _effective_reference_bytes() or b""
        new_ref = svc.load_reference(raw_ref)
        others = [d for d in svc.store.days.values() if d.date != active]
        try:
            svc.store.reference = new_ref
            svc.store.ref_gen = uuid.uuid4().hex
            state_repo.save_reference(raw_ref, svc.store.ref_gen)
            svc.put_day(active.isoformat(), json.dumps(body, ensure_ascii=False).encode("utf-8"))
            plan = svc.build_plan(active.isoformat(), json.dumps({"time_limit_sec": _plan_time_limit()}).encode())
        except Exception:
            svc.store.reference, svc.store.ref_gen = old_ref, old_gen
            svc.store.days[active] = old
            raise
        svc.store.notify_reference(new_ref, derived=True)
        for d in others:  # прочие дни остаются; снимки перезаписываются под новое поколение справочников
            svc._persist_day(d)
        if demo_for(request, active) is not None:
            request.app.state.ui_demo = None  # проигрыватель сценария начинал с прежнего плана
        _mark_active(active, svc.store.days[active])
    return {"status": "rebuilt", "date": active.isoformat(), "now": body["now"], "plan_version": plan.plan_version,
            "orders": len(orders), "computation_ms": plan.computation_ms}


@router.post("/demo/step")
def demo_step(request: Request, count: int = Query(1, ge=1, le=50)) -> dict:
    svc = _service(request)
    demo = _demo(request)
    if demo is None:
        raise ApiError(409, "PLAN_NOT_BUILT", "Сначала создайте или загрузите день")
    sent = []
    for _ in range(count):
        body = _next_body(svc, demo)
        if body is None:
            break
        item = {"event_id": body["event_id"], "type": body["type"], "timestamp": body["timestamp"]}
        try:
            res = svc.post_event(demo["date"], json.dumps(body, ensure_ascii=False).encode("utf-8"))
            item.update(accepted=True, plan_version=res.plan_version, processing_ms=res.processing_ms,
                        message=res.message, changed_staff_ids=res.changed_staff_ids)
        except ApiError as exc:
            item.update(accepted=False, error=exc.body())
        sent.append(item)
    _persist_demo(svc, demo)
    status = _demo_status(svc, demo)
    return {"sent": sent, "plan_version": status["plan_version"], "as_of": status["as_of"], "demo": status}


@router.get("/day/{date}/orders")
def day_orders(date: str, request: Request) -> dict:
    svc = _service(request)
    plan = svc.get_plan(date)
    with svc.store.lock:
        day = svc._day(dt.date.fromisoformat(date))
        orders = list(day.orders.values())
        ref = svc.store.reference
    vis = _visits(plan)
    cancelled = {c.order_id: c for c in plan.cancelled}
    pgp = {oid: g for g in plan.pgp_groups for oid in g.order_ids}
    unassigned = {u.order_id: u for u in plan.unassigned}
    late = {a.order_id: a for a in plan.alerts if a.type == "LATE_RISK" and a.status == "ACTIVE"}

    rows = []
    for o in orders:
        row = o.model_dump(mode="json", exclude={"fact"})
        wt = ref.work_types.get(o.work_type_code)
        row["work_type_name"] = wt.name if wt else o.work_type_code
        row.update(status="UNKNOWN", staff_id=None, staff_name=None, planned_arrival=None,
                   planned_start=None, planned_end=None, stop_status=None, explanation=None,
                   group_id=None, reason_code=None, reason_text=None, suggested_action=None,
                   cancelled_at=None, late_risk=None, partner_staff_id=None, partner_staff_name=None)
        if o.order_id in cancelled:
            row.update(status="CANCELLED", cancelled_at=_iso(cancelled[o.order_id].cancelled_at))
        elif o.order_id in vis:
            sid, stop = vis[o.order_id]
            staff = ref.staff.get(sid)
            row.update(status="ROUTED", staff_id=sid,
                       staff_name=_staff_name(sid, staff.display_name if staff else None),
                       planned_arrival=_iso(stop.arrival), planned_start=_iso(stop.start),
                       planned_end=_iso(stop.end), stop_status=stop.status, explanation=stop.explanation)
            # совместный наряд (JOINT_ORDERS): визит напарника — ORD-…-B у другого сотрудника
            partner = vis.get(o.order_id + "-B")
            if partner is not None:
                pst = ref.staff.get(partner[0])
                row.update(partner_staff_id=partner[0],
                           partner_staff_name=_staff_name(partner[0], pst.display_name if pst else None))
        elif o.order_id in pgp:
            row.update(status="PGP", group_id=pgp[o.order_id].group_id)
        elif o.order_id in unassigned:
            u = unassigned[o.order_id]
            row.update(status="UNASSIGNED", reason_code=u.reason_code, reason_text=u.reason_text,
                       suggested_action=u.suggested_action)
        if o.order_id in late:
            a = late[o.order_id]
            row["late_risk"] = {"alert_id": a.alert_id, "lateness_min": a.lateness_min,
                                "suggested_action": a.suggested_action}
        rows.append(row)
    return {"date": date, "plan_version": plan.plan_version, "as_of": _iso(plan.as_of), "orders": rows}


@router.get("/reference")
def reference(request: Request) -> dict:
    ref = _service(request).store.reference
    if ref is None:
        raise ApiError(409, "REFERENCE_NOT_LOADED", "Справочники не загружены: PUT /api/v1/reference или «Создать день»")

    def dump(items):
        return [x.model_dump(mode="json", by_alias=True) for x in items]

    nodes = []
    for n in ref.access_nodes.values():
        d = n.model_dump(mode="json", by_alias=True, exclude={"buildings"})
        d["buildings_count"] = len(n.buildings)
        nodes.append(d)
    staff = dump(ref.staff.values())
    for d in staff:  # «Фамилия И. О.» из справочника сотрудников (только /ui, не /api/v1)
        d["display_name"] = _staff_name(d["staff_id"], d.get("display_name"))
    return {
        "service_centers": dump(ref.service_centers.values()),
        "staff": staff,
        "work_types": dump(ref.work_types.values()),
        "access_nodes": nodes,
    }


DEFAULT_CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173", "http://0.0.0.0:5173"]  # dev-фронт


def cors_origins() -> list[str]:
    """Разрешённые origin: env CORS_ORIGINS (через запятую), иначе список по умолчанию.
    "*" разрешает всех, но только если задан явно."""
    raw = os.environ.get("CORS_ORIGINS", "")
    items = [x.strip() for x in raw.split(",") if x.strip()]
    return items or list(DEFAULT_CORS_ORIGINS)


def install_ui(app: FastAPI) -> None:
    """Подключает /ui (с правами из auth.py) и CORS для веб-клиента."""
    from fastapi import Depends
    from . import auth
    from .auth_api import admin_router as accounts_router, router as auth_router
    only_dispatcher = [Depends(auth.require_dispatcher)]
    app.include_router(auth_router)  # /ui/auth/*: открытые (внутри свои проверки)
    app.include_router(accounts_router)  # /ui/directory/accounts/*: диспетчер (зависимость в самом роутере)
    app.include_router(router, dependencies=only_dispatcher)
    from .scenarios import router as scenario_router; app.include_router(scenario_router, dependencies=only_dispatcher)  # noqa: E702
    from .history_api import router as history_router; app.include_router(history_router, dependencies=only_dispatcher)  # noqa: E702
    # /ui/employee/*: сотрудник — только свой staff_id (проверка в зависимости), диспетчер — любой
    from .employee_api import router as employee_router; app.include_router(employee_router, dependencies=[Depends(auth.employee_scope)])  # noqa: E702
    from .staff_directory import router as directory_router; app.include_router(directory_router, dependencies=only_dispatcher)  # noqa: E702
    from .orders_ui import router as orders_router; app.include_router(orders_router, dependencies=only_dispatcher)  # noqa: E702
    from .duration_forecast import router as forecast_router; app.include_router(forecast_router, dependencies=only_dispatcher)  # noqa: E702
    origins = cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        # Cookie и Authorization нужны веб-клиенту на другом origin; при "*" credentials не включаем
        allow_credentials="*" not in origins,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type", "Accept", "X-API-Key"],
    )
