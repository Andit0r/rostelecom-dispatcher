"""Сценарий contract: контрактные проверки API (коды HTTP, формат ошибок, идемпотентность, порядок вызовов)."""

from __future__ import annotations

import copy
import datetime as dt
import json

from simlib import schema
from simlib.client import Resp
from simlib.data import iso, parse_ts
from simlib.report import ScenarioResult
from simlib.session import Ctx, DaySession, at, visits_of

NAME = "contract"
UNKNOWN_DAY = "2027-01-15"


def _expect(res: ScenarioResult, r: Resp, ok: tuple[int, ...], tag: str, code: str | None = None,
            alt: tuple[int, ...] = ()) -> bool:
    """Проверка статуса: ok - ожидается по контракту; alt - допустимо, но с замечанием (контракт молчит)."""
    if r.status in ok:
        good = True
    elif r.status in alt:
        res.add("note", f"{tag}: код {r.status} вместо {'/'.join(map(str, ok))} (спецификация допускает не явно)",
                str(r.data)[:200], tag)
        good = True
    else:
        res.check(False, f"{tag}: ожидался HTTP {'/'.join(map(str, ok))}", f"получен {r.status}: {r.text or str(r.data)[:200]}",
                  "contract", tag)
        return False
    res.checks += 1
    if r.status >= 400:
        errs = schema.validate(r.data, schema.ERROR)
        res.schema_check("ErrorResponse", errs, tag)
        if code is not None and r.error_code() != code:
            res.add("note", f"{tag}: код ошибки {r.error_code()!r}, ожидался {code!r} (в спецификации - пример)", "", tag)
    return good


def _same(a, b) -> bool:
    def norm(p):
        p = dict(p)
        p.pop("computation_ms", None)
        return json.dumps(p, sort_keys=True, ensure_ascii=False)
    return norm(a) == norm(b)


def run(ctx: Ctx) -> ScenarioResult:
    res = ScenarioResult(NAME, ctx.seed)
    rng = ctx.rng(NAME)
    data, c = ctx.data, ctx.client
    errors_before = len(c.server_errors)

    # ---- 1. health
    r = c.health()
    if res.check(r.ok, "GET /health не 200", f"{r.status}", "contract", "health"):
        errs = schema.validate(r.data, schema.HEALTH)
        res.check(not errs, "GET /health: ответ не соответствует Health", "; ".join(errs), "contract", "health")
        res.check("json" in r.content_type, "GET /health: Content-Type не application/json", r.content_type,
                  "contract", "health")

    # ---- 2. reference и матрица: положительный путь, повторная загрузка
    r = c.put_reference(data.reference)
    if res.check(r.ok, "PUT /reference не 200", f"{r.status} {r.text or r.data}", "contract", "reference"):
        errs = schema.validate(r.data, schema.LOAD_RESULT)
        res.check(not errs, "PUT /reference: ответ не LoadResult", "; ".join(errs), "contract", "reference")
        cnt = (r.data or {}).get("counts", {})
        res.check(cnt.get("staff") == len(data.reference["staff"]) and
                  cnt.get("service_centers") == len(data.reference["service_centers"]),
                  "PUT /reference: counts не соответствуют загруженному", str(cnt), "contract", "reference")
        res.check({"service_centers", "staff", "access_nodes", "buildings", "work_types"} <= set(cnt),
                  "PUT /reference: counts без ключей из примера спецификации", str(cnt), "note", "reference")
    r = c.put_matrix(data.matrix_raw)
    if res.check(r.ok, "PUT /reference/travel-matrix не 200", f"{r.status} {r.text or r.data}", "contract", "matrix"):
        errs = schema.validate(r.data, schema.LOAD_RESULT)
        res.check(not errs, "PUT travel-matrix: ответ не LoadResult", "; ".join(errs), "contract", "matrix")
        res.check((r.data or {}).get("counts", {}).get("points", -1) > 0, "PUT travel-matrix: counts.points", str(r.data),
                  "note", "matrix")
    ctx.reference_loaded = True

    # ---- 3. день D1 с планом
    date = ctx.options.get("contract_date") or ctx.data.train_dates()[8]
    idc = date.replace("-", "")  # часть order_id вида ORD-YYYYMMDD-NNNN
    now = at(date, "08:30")
    base = data.day_orders(date, now, min(ctx.scale, 0.1), rng)
    tl = min(ctx.time_limit, 8)
    s = DaySession(ctx, res, date, now, base, [], time_limit=tl)
    if not s.setup():
        return res
    plan0 = copy.deepcopy(s.plan_json)

    def unchanged(tag: str) -> None:
        g = c.get_plan(date)
        res.check(g.ok and _same(g.data, s.plan_json), f"{tag}: состояние изменилось после отклонённого запроса",
                  f"GET {g.status}", "contract", tag)

    # ---- 4. ошибки в PUT reference / matrix не ломают состояние
    r = c.call("PUT reference (bad)", "PUT", "/reference", content=b"{not json")
    _expect(res, r, (400,), "reference: невалидный JSON", "BAD_REQUEST")
    bad = copy.deepcopy(data.reference)
    del bad["staff"][0]["staff_id"]
    r = c.call("PUT reference (bad)", "PUT", "/reference", json_body=bad)
    _expect(res, r, (400,), "reference: нарушение схемы", "BAD_REQUEST")
    bad = copy.deepcopy(data.reference)
    bad["staff"][0]["service_center_id"] = "SC-99"
    r = c.call("PUT reference (bad)", "PUT", "/reference", json_body=bad)
    _expect(res, r, (422,), "reference: неизвестный СЦ у сотрудника", None, alt=(400,))
    bad = copy.deepcopy(data.reference)
    bad["staff"][0]["equipment_capacity"] = {"ONT": 0, "ROUTER": 0, "STB": 0}
    bad["staff"][0]["equipment_start"] = {"ONT": 5, "ROUTER": 5, "STB": 5}
    r = c.call("PUT reference (bad)", "PUT", "/reference", json_body=bad)
    _expect(res, r, (422,), "reference: capacity меньше start", None, alt=(400,))
    r = c.call("PUT matrix (bad)", "PUT", "/reference/travel-matrix", content=b"[1,2")
    _expect(res, r, (400,), "matrix: невалидный JSON", "BAD_REQUEST")
    r = c.call("PUT matrix (bad)", "PUT", "/reference/travel-matrix",
               json_body={"matrix_id": "x", "points": ["SC-01", "SC-02"], "durations_sec": [[0, 1], [1, 0]]})
    _expect(res, r, (400,), "matrix: нет distances_m", "BAD_REQUEST")
    r = c.call("PUT matrix (bad)", "PUT", "/reference/travel-matrix",
               json_body={"matrix_id": "x", "points": ["SC-01", "SC-02"], "durations_sec": [[0, 1]],
                          "distances_m": [[0, 1], [1, 0]]})
    _expect(res, r, (422,), "matrix: durations_sec не N x N", None, alt=(400,))
    unchanged("после неудачных PUT reference/matrix")

    # ---- 5. PUT days: ошибки
    good_day = {"date": date, "now": iso(now), "orders": base}
    for bad_date in ("2026-13-45", "not-a-date", "2026-10-9"):
        r = c.call("PUT day (bad)", "PUT", f"/days/{bad_date}", json_body={**good_day, "date": bad_date})
        _expect(res, r, (400,), f"PUT days/{bad_date}: неверная дата", None, alt=(404, 422))
    r = c.call("PUT day (bad)", "PUT", f"/days/{date}", json_body={**good_day, "date": ctx.data.train_dates()[9]})
    _expect(res, r, (400,), "PUT days: дата в теле не совпадает с путём", None, alt=(422,))
    r = c.call("PUT day (bad)", "PUT", f"/days/{date}", content=b"{")
    _expect(res, r, (400,), "PUT days: невалидный JSON", "BAD_REQUEST")
    r = c.call("PUT day (bad)", "PUT", f"/days/{date}", json_body={"date": date, "now": iso(now)})
    _expect(res, r, (400,), "PUT days: нет orders", "BAD_REQUEST")
    if base:
        o = copy.deepcopy(base[0])
        o["slot"]["end"] = "завтра"
        r = c.call("PUT day (bad)", "PUT", f"/days/{date}", json_body={**good_day, "orders": [o]})
        _expect(res, r, (400,), "PUT days: slot.end не date-time", "BAD_REQUEST")
        o = copy.deepcopy(base[0])
        o["building_id"] = "B-99999"
        r = c.call("PUT day (bad)", "PUT", f"/days/{date}", json_body={**good_day, "orders": [o]})
        _expect(res, r, (422,), "PUT days: неизвестный дом", "UNKNOWN_BUILDING")
        o = copy.deepcopy(base[0])
        o["access_node_id"] = "AN-9999"
        r = c.call("PUT day (bad)", "PUT", f"/days/{date}", json_body={**good_day, "orders": [o]})
        _expect(res, r, (422,), "PUT days: неизвестный узел доступа", None)
        r = c.call("PUT day (bad)", "PUT", f"/days/{date}", json_body={**good_day, "orders": [base[0], base[0]]})
        _expect(res, r, (422,), "PUT days: повтор order_id", None, alt=(400,))
    r = c.call("PUT day (bad)", "PUT", f"/days/{date}", json_body={**good_day, "absent_staff_ids": ["ST-999"]})
    _expect(res, r, (422,), "PUT days: неизвестный absent_staff_id", "UNKNOWN_STAFF")
    unchanged("после неудачных PUT days")

    # ---- 6. plan: 404 / 400
    r = c.call("GET plan (404)", "GET", f"/days/{UNKNOWN_DAY}/plan")
    _expect(res, r, (404,), "GET plan: день не загружен", None)
    r = c.call("POST plan (404)", "POST", f"/days/{UNKNOWN_DAY}/plan", json_body={"time_limit_sec": 5})
    _expect(res, r, (404,), "POST plan: день не загружен", None)
    r = c.call("GET plan (bad)", "GET", "/days/2026-02-30/plan")
    _expect(res, r, (404, 400), "GET plan: недопустимая дата", None, alt=(422,))
    for tag, body in (("time_limit_sec=0", {"time_limit_sec": 0}), ("time_limit_sec=301", {"time_limit_sec": 301}),
                      ("time_limit_sec строкой", {"time_limit_sec": "abc"})):
        r = c.call("POST plan (bad)", "POST", f"/days/{date}/plan", json_body=body)
        _expect(res, r, (400,), f"POST plan: {tag}", "BAD_REQUEST", alt=(422,))
    r = c.call("POST plan (bad)", "POST", f"/days/{date}/plan", content=b"{oops")
    _expect(res, r, (400,), "POST plan: невалидный JSON", "BAD_REQUEST")
    unchanged("после неудачных POST plan")

    # ---- 7. событие по невалидной схеме / несуществующему дню
    ver = s.model.version
    ts1 = at(date, "09:00")
    tick = s.tick(ts1)
    r = c.call("POST event (bad)", "POST", f"/days/{UNKNOWN_DAY}/events", json_body=tick)
    _expect(res, r, (404,), "events: день не загружен", None)
    r = c.post_raw_event(date, "{bad")
    _expect(res, r, (400,), "events: невалидный JSON", "BAD_REQUEST")
    for tag, body in (
        ("неизвестный type", {**tick, "type": "BOOM"}),
        ("нет event_id", {k: v for k, v in tick.items() if k != "event_id"}),
        ("timestamp не date-time", {**tick, "timestamp": "9 утра"}),
        ("NEW_ORDER без order", {"event_id": "E-X1", "type": "NEW_ORDER", "timestamp": iso(ts1)}),
        ("ORDER_CANCELLED: reason вне enum", {"event_id": "E-X2", "type": "ORDER_CANCELLED", "timestamp": iso(ts1),
                                              "order_id": next(iter(s.model.orders), f"ORD-{idc}-0001"), "reason": "WHIM"}),
        ("STAFF_UNAVAILABLE: reason вне enum", {"event_id": "E-X3", "type": "STAFF_UNAVAILABLE", "timestamp": iso(ts1),
                                                "staff_id": "ST-001", "reason": "LAZY"}),
    ):
        r = c.post_event(date, body, "POST event (bad)")
        _expect(res, r, (400,), f"events: {tag}", "BAD_REQUEST")
    unchanged("после невалидных событий (400)")

    # ---- 8. бизнес-ошибки 422 (состояние не меняется)
    vis = visits_of(s.plan)
    tstart = at(date, "09:05")
    unstarted = [(oid, sid) for oid, (sid, st) in vis.items() if st.start > tstart]
    cases: list[tuple[str, dict]] = [
        ("ORDER_CANCELLED: неизвестный наряд", {"event_id": "E-U1", "type": "ORDER_CANCELLED", "timestamp": iso(tstart),
                                                "order_id": f"ORD-{idc}-7777", "reason": "NOT_CONFIRMED"}),
        ("VISIT_DELAYED: неизвестный наряд", {"event_id": "E-U2", "type": "VISIT_DELAYED", "timestamp": iso(tstart),
                                              "staff_id": "ST-001", "order_id": f"ORD-{idc}-7777",
                                              "expected_end": iso(tstart + dt.timedelta(hours=1))}),
        ("VISIT_DELAYED: неизвестный сотрудник", {"event_id": "E-U3", "type": "VISIT_DELAYED", "timestamp": iso(tstart),
                                                  "staff_id": "ST-999", "order_id": next(iter(s.model.orders), f"ORD-{idc}-0001"),
                                                  "expected_end": iso(tstart + dt.timedelta(hours=1))}),
        ("STAFF_UNAVAILABLE: неизвестный сотрудник", {"event_id": "E-U4", "type": "STAFF_UNAVAILABLE",
                                                      "timestamp": iso(tstart), "staff_id": "ST-999", "reason": "ILLNESS"}),
        ("STAFF_UNAVAILABLE: until раньше timestamp", {"event_id": "E-U5", "type": "STAFF_UNAVAILABLE",
                                                       "timestamp": iso(tstart), "staff_id": "ST-001",
                                                       "until": iso(tstart - dt.timedelta(minutes=10)), "reason": "OTHER"}),
    ]
    if base:
        cases.append(("NEW_ORDER: наряд уже существует", {"event_id": "E-U6", "type": "NEW_ORDER", "timestamp": iso(tstart),
                                                          "order": base[0]}))
        o = copy.deepcopy(base[0])
        o.update(order_id=f"ORD-{idc}-8001", building_id="B-99999")
        cases.append(("NEW_ORDER: неизвестный дом", {"event_id": "E-U7", "type": "NEW_ORDER", "timestamp": iso(tstart), "order": o}))
    if unstarted:
        oid, sid = unstarted[0]
        cases.append(("VISIT_DELAYED: визит не начат", {"event_id": "E-U8", "type": "VISIT_DELAYED", "timestamp": iso(tstart),
                                                        "staff_id": sid, "order_id": oid,
                                                        "expected_end": iso(tstart + dt.timedelta(hours=5))}))
        cases.append(("ORDER_TRANSFERRED: визит не начат", {"event_id": "E-U9", "type": "ORDER_TRANSFERRED",
                                                            "timestamp": iso(tstart), "staff_id": sid, "order_id": oid,
                                                            "actual_end": iso(tstart), "transferred_to": "TRUNK_CABLE"}))
    for tag, body in cases:
        s.step += 1
        res.events += 1
        r = c.post_event(date, body, "POST event (422)")
        _expect(res, r, (422,), f"events: {tag}", None)
    unchanged("после отклонённых 422 событий")

    # ---- 9. нормальные события: версии, одинаковый timestamp, смещение, идемпотентность
    r = s.send(tick, label="POST event [TIME_TICK]")
    v1 = s.model.version
    tick2 = s.tick(ts1)  # тот же timestamp допустим
    s.send(tick2, label="POST event [TIME_TICK]")
    tick_utc = {"event_id": s.next_event_id("TK"), "type": "TIME_TICK",
                "timestamp": (ts1 + dt.timedelta(minutes=5)).astimezone(dt.timezone.utc).isoformat()}
    r = s.send(tick_utc, label="POST event [TIME_TICK]")  # смещение +00:00 принимается (п. 2 спецификации)
    v3 = s.model.version
    res.check(v3 == ver + 3, "plan_version после трёх событий не равен версия+3", f"{v3} vs {ver + 3}", "contract", "versions")
    # идемпотентность: повтор первого события
    r = c.post_event(date, tick, "POST event (duplicate)")
    s.step += 1
    res.events += 1
    if _expect(res, r, (200,), "повтор event_id"):
        d = r.data
        res.check(d.get("duplicate") is True and d.get("accepted") is True, "повтор event_id: duplicate=true", str(d), "contract", "dup")
        if d.get("plan_version") != v3:
            res.add("note", "повтор старого event_id вернул plan_version не текущей версии",
                    f"{d.get('plan_version')} (версия события {v1}, текущая {v3})", "dup")
        res.check(d.get("plan_version") in (v1, v3), "повтор event_id: plan_version ни версия события, ни текущая",
                  str(d.get("plan_version")), "contract", "dup")
    # тот же event_id с другим телом - всё равно повтор
    r = c.post_event(date, {**tick, "timestamp": iso(ts1 + dt.timedelta(hours=1))}, "POST event (duplicate)")
    if _expect(res, r, (200,), "повтор event_id с другим телом"):
        res.check(r.data.get("duplicate") is True, "повтор event_id с другим телом: duplicate=true", str(r.data), "contract", "dup2")
    g = c.get_plan(date)
    res.check(g.ok and g.data["plan_version"] == v3, "план после повторов: plan_version изменился", str(g.data.get("plan_version")),
              "contract", "dup3")
    # порядок времени
    cur = s.model.model_time
    r = c.post_event(date, s.tick(cur - dt.timedelta(minutes=1)), "POST event (409)")
    _expect(res, r, (409,), "event раньше последнего (EVENT_OUT_OF_ORDER)", "EVENT_OUT_OF_ORDER")
    r = c.post_event(date, s.tick(now - dt.timedelta(minutes=30)), "POST event (409)")
    _expect(res, r, (409,), "event раньше now загрузки дня", "EVENT_OUT_OF_ORDER")
    res.events += 2
    g = c.get_plan(date)
    res.check(g.ok and g.data["plan_version"] == v3, "409-события изменили версию плана", "", "contract", "409state")

    # отмена и повторная отмена
    cand = [oid for oid in s.model.orders if oid not in visits_of(s.plan) or visits_of(s.plan)[oid][1].start > cur]
    if cand:
        r = s.send(s.cancel(cur + dt.timedelta(minutes=1), cand[0], "NOT_CONFIRMED"), label="POST event [ORDER_CANCELLED]")
        res.events += 1  # повторная отмена ниже
        r = c.post_event(date, s.cancel(cur + dt.timedelta(minutes=2), cand[0]), "POST event (422)")
        _expect(res, r, (422,), "повторная отмена наряда", None)
        unchanged("после повторной отмены")
    # POST /plan после первого события - 409
    r = c.post_plan(date, tl)
    _expect(res, r, (409,), "POST plan после первого события", None)
    unchanged("после POST plan 409")

    # ---- 10. день без нарядов и порядок вызовов (D2)
    d2 = ctx.options.get("contract_date2") or ctx.data.train_dates()[9]
    r = c.put_day(d2, {"date": d2, "now": iso(at(d2, "08:30")), "orders": []})
    if res.check(r.ok, "PUT days с пустым списком orders не 200", f"{r.status} {r.text or r.data}", "contract", "empty_day"):
        res.check((r.data or {}).get("counts", {}).get("orders") == 0, "пустой день: counts.orders != 0", str(r.data),
                  "contract", "empty_day")
        r = c.get_plan(d2)
        _expect(res, r, (404,), "GET plan до POST plan", "PLAN_NOT_FOUND")
        r = c.post_event(d2, {"event_id": "E-D2-1", "type": "TIME_TICK", "timestamp": iso(at(d2, "09:00"))}, "POST event (409)")
        _expect(res, r, (409,), "событие до построения плана", None, alt=(404,))
        r = c.post_plan(d2, 5)
        if res.check(r.ok, "POST plan для пустого дня не 200", f"{r.status} {r.text or r.data}", "contract", "empty_day"):
            errs = schema.validate(r.data, schema.PLAN)
            res.check(not errs, "пустой день: план не по схеме", "; ".join(errs[:3]), "contract", "empty_day")
            res.check(all(not rt["stops"] or all(st["type"] != "VISIT" for st in rt["stops"]) for rt in r.data["routes"]),
                      "пустой день: визиты в маршрутах", "", "contract", "empty_day")
    # первый день остался доступен (одновременно держим два дня)
    g = c.get_plan(date)
    if g.ok:
        res.note("загрузка второго дня (D2) не стёрла первый: GET plan для D1 доступен")
    else:
        res.add("note", "после PUT days другого дня план первого дня недоступен (по спецификации можно, держать >= 1 дня)",
                f"{g.status}", "two_days")

    # ---- 11. событие далеко за пределами рабочего дня (контракт молчит): фиксируем поведение
    if g.ok:
        far = at(date, "09:00") + dt.timedelta(days=1, hours=2)
        r = c.post_event(date, s.tick(far), "POST event (far)")
        res.events += 1
        res.metrics["event_next_day_status"] = r.status
        if r.status == 200:
            gp = c.get_plan(date)
            if gp.ok:
                res.schema_check("Plan (после события на следующих сутках)", schema.validate(gp.data, schema.PLAN), "far_event")
        else:
            _expect(res, r, (409, 422), "событие на следующих сутках", None, alt=(400,))
    # ---- 12. типы содержимого и 5xx
    res.check(len(c.server_errors) == errors_before, "в ходе contract были ответы 5xx/сбои соединения",
              "; ".join(c.server_errors[errors_before:][:3]), "error", "5xx")
    return res
