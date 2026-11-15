"""Прогноз фактической длительности работ по истории нарядов (/ui/forecast, вне контракта /api/v1).

Лёгкая объяснимая статистика без обучения нейросетей. История — только реальная, если она есть:
* завершённые наряды из хранилища: наряды дней в Store с fact.outcome=COMPLETED (если внешняя система их
  прислала) и отметки сотрудников ARRIVED → DONE (длительность = DONE − ARRIVED);
* файл истории, явно заданный env HISTORY_FILE (JSON-список нарядов в формате Order с fact, как
  data/synth/orders_train.json). По умолчанию не задан: каталог data/synth здесь не читается.
Нормативы и опыт сотрудников — из справочника в Store. Истории меньше MIN_N выполненных нарядов или
справочника нет — модель недоступна: /ui/forecast/model и /risk отвечают {available: false, reason}.

По выполненным нарядам для типа работ (и типа здания, если разница значима и наблюдений >= MIN_N) считаются среднее,
медиана, p80 и n; поправка на опыт сотрудника включается, только если линейная зависимость
отношения факт/норматив от experience_years статистически заметна. Иерархический откат:
(тип + здание) -> тип -> норматив.

Прогноз НЕ подменяет норматив в плане: валидатор проверяет duration_min, поэтому визит в плане
остаётся нормативным, а прогноз — информация и раннее предупреждение (см. docs/planner-notes.md).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from pathlib import Path

import numpy as np
from fastapi import APIRouter, Request

from .errors import ApiError

MIN_N = 20  # минимальная выборка группы; меньше — откат на уровень выше
T_CRIT = 2.58  # |t| для значимости (p < 0,01)
TEST_DAYS_SHARE = 0.3  # доля последних дней под отложенную выборку

router = APIRouter(prefix="/ui/forecast", tags=["ui-forecast"])


def _stats(values: list[float] | np.ndarray) -> dict:
    a = np.asarray(values, dtype=float)
    return {"mean": float(a.mean()), "median": float(np.median(a)), "p80": float(np.percentile(a, 80)), "n": int(a.size)}


def _welch_t(a: np.ndarray, b: np.ndarray) -> float:
    va, vb = a.var(ddof=1) / a.size, b.var(ddof=1) / b.size
    return float((a.mean() - b.mean()) / np.sqrt(va + vb)) if va + vb > 0 else 0.0


class DurationModel:
    def __init__(self, rows: list[dict], normative: dict[str, int], names: dict[str, str], experience: dict[str, float]):
        """rows: {work_type, building_type, staff_id, actual, date}."""
        self.normative, self.names, self.experience = normative, names, experience
        self.n_rows = len(rows)
        self.by_type: dict[str, dict] = {}
        self.by_building: dict[tuple[str, str], dict] = {}
        self.building_effect: dict[str, bool] = {}
        by_t: dict[str, list[dict]] = {}
        for r in rows:
            by_t.setdefault(r["work_type"], []).append(r)
        for wt, rs in by_t.items():
            self.by_type[wt] = _stats([r["actual"] for r in rs])
            groups: dict[str, np.ndarray] = {}
            for b in {r["building_type"] for r in rs}:
                groups[b] = np.array([r["actual"] / normative[wt] for r in rs if r["building_type"] == b])
            ok = len(groups) == 2 and all(g.size >= MIN_N for g in groups.values())
            ga, gb = list(groups.values()) if ok else (None, None)
            # здание учитываем только при значимой разнице отношений факт/норматив
            sig = bool(ok and abs(_welch_t(ga, gb)) > T_CRIT)
            self.building_effect[wt] = sig
            if sig:
                for b in groups:
                    self.by_building[(wt, b)] = _stats([r["actual"] for r in rs if r["building_type"] == b])
        # поправка на опыт: ratio = a * experience + c
        xs = np.array([experience[r["staff_id"]] for r in rows if r["staff_id"] in experience], dtype=float)
        ys = np.array([r["actual"] / normative[r["work_type"]] for r in rows if r["staff_id"] in experience], dtype=float)
        self.exp_slope = self.exp_icpt = 0.0
        self.exp_t = 0.0
        self.exp_corr = 0.0
        self.exp_mean_ratio = 1.0
        self.exp_mean_x = 0.0
        if xs.size > 30 and xs.std() > 0 and ys.std() > 0:
            self.exp_corr = float(np.corrcoef(xs, ys)[0, 1])
            self.exp_t = self.exp_corr * float(np.sqrt((xs.size - 2) / max(1e-12, 1 - self.exp_corr ** 2)))
            slope, icpt = np.polyfit(xs, ys, 1)
            self.exp_slope, self.exp_icpt = float(slope), float(icpt)
            self.exp_mean_ratio, self.exp_mean_x = float(ys.mean()), float(xs.mean())
        self.exp_used = abs(self.exp_t) > T_CRIT

    def predict(self, work_type: str, building_type: str | None = None, staff_id: str | None = None) -> dict:
        norm = self.normative[work_type]
        key = (work_type, building_type)
        if key in self.by_building and self.by_building[key]["n"] >= MIN_N:
            st, source = self.by_building[key], "type+building"
        elif work_type in self.by_type and self.by_type[work_type]["n"] >= MIN_N:
            st, source = self.by_type[work_type], "type"
        else:
            return {"normative": norm, "expected": float(norm), "p80": float(norm), "n": self.by_type.get(work_type, {}).get("n", 0),
                    "source": "normative"}
        k = 1.0
        if self.exp_used and staff_id in self.experience:
            k = (self.exp_icpt + self.exp_slope * self.experience[staff_id]) / (
                self.exp_icpt + self.exp_slope * self.exp_mean_x)
            source += "+experience"
        return {"normative": norm, "expected": round(st["mean"] * k, 1), "p80": round(st["p80"] * k, 1), "n": st["n"], "source": source}


# ---------------------------------------------------------------- обучение и кэш

_lock = threading.Lock()
_cache: dict = {}


NO_HISTORY = "Недостаточно истории для прогноза"


def _fact_row(o: dict, normative: dict[str, int]) -> dict | None:
    f = o.get("fact") or {}
    if f.get("outcome") != "COMPLETED" or f.get("actual_duration_min") is None or o.get("work_type_code") not in normative:
        return None
    return {"work_type": o["work_type_code"], "building_type": o["building_type"], "staff_id": f.get("staff_id"),
            "actual": float(f["actual_duration_min"]), "date": o["slot"]["start"][:10]}


def _history_file() -> Path | None:
    raw = (os.environ.get("HISTORY_FILE") or "").strip()
    return Path(raw) if raw else None


def _file_rows(path: Path, normative: dict[str, int]) -> list[dict]:
    try:
        orders = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    rows = []
    for o in orders if isinstance(orders, list) else []:
        try:
            r = _fact_row(o, normative)
        except (KeyError, TypeError, AttributeError):
            r = None
        if r is not None:
            rows.append(r)
    return rows


def _store_rows(app, normative: dict[str, int]) -> list[dict]:
    """Завершённые наряды из Store: fact.COMPLETED в нарядах дней и отметки сотрудников ARRIVED → DONE."""
    svc = app.state.service
    rows: list[dict] = []
    with svc.store.lock:
        days = list(svc.store.days.values())
        box = getattr(app.state, "employee_marks", None)
        marks_by_day = dict(box["days"]) if box and box.get("ref") is svc.store.reference else {}
        seen: set[str] = set()
        for day in days:
            for o in day.orders.values():
                r = _fact_row(o.model_dump(mode="json"), normative)
                if r is not None:
                    rows.append(r)
                    seen.add(o.order_id)
            marks = marks_by_day.get((day.date, day.load_id or day.now)) or {}
            for oid, m in marks.items():
                o = day.orders.get(oid)
                if o is None or oid in seen or "ARRIVED" not in m or "DONE" not in m:
                    continue
                if o.work_type_code not in normative:
                    continue
                try:
                    dur = (dt.datetime.fromisoformat(m["DONE"]["at"]) - dt.datetime.fromisoformat(m["ARRIVED"]["at"]))
                except (KeyError, TypeError, ValueError):
                    continue
                minutes = dur.total_seconds() / 60
                if minutes <= 0:
                    continue
                rows.append({"work_type": o.work_type_code, "building_type": getattr(o.building_type, "value", o.building_type),
                             "staff_id": m["DONE"].get("staff_id") or m["ARRIVED"].get("staff_id"),
                             "actual": float(minutes), "date": day.date.isoformat()})
    return rows


def _evaluate(rows, normative, names, exp) -> dict:
    days = sorted({r["date"] for r in rows})
    n_test = max(1, round(len(days) * TEST_DAYS_SHARE)) if len(days) > 1 else 0
    if n_test == 0 or len(days) - n_test < 1:
        return {"available": False}
    test_days = set(days[-n_test:])
    train = [r for r in rows if r["date"] not in test_days]
    test = [r for r in rows if r["date"] in test_days]
    m = DurationModel(train, normative, names, exp)
    err_n = np.array([abs(r["actual"] - normative[r["work_type"]]) for r in test])
    preds = [m.predict(r["work_type"], r["building_type"], r["staff_id"]) for r in test]
    err_f = np.array([abs(r["actual"] - p["expected"]) for r, p in zip(test, preds)])
    cover = float(np.mean([r["actual"] <= p["p80"] for r, p in zip(test, preds)]))
    cover_n = float(np.mean([r["actual"] <= normative[r["work_type"]] for r in test]))
    return {
        "available": True,
        "train_days": [d for d in days if d not in test_days], "test_days": sorted(test_days),
        "n_train": len(train), "n_test": len(test),
        "mae_normative_min": round(float(err_n.mean()), 2), "mae_forecast_min": round(float(err_f.mean()), 2),
        "improvement_pct": round(100 * (1 - float(err_f.mean()) / float(err_n.mean())), 1) if err_n.mean() > 0 else 0.0,
        "p80_coverage": round(cover, 3), "normative_coverage": round(cover_n, 3),
    }


def _model_state(app) -> dict:
    """{available, reason?, model?, quality?}: модель по истории, если она есть (кэш по составу истории)."""
    ref = app.state.service.store.reference
    if ref is None:
        return {"available": False, "reason": "Справочник не загружен: нормативы типов работ неизвестны"}
    normative = {c: int(w.duration_min) for c, w in ref.work_types.items()}
    names = {c: w.name for c, w in ref.work_types.items()}
    exp = {sid: float(s.experience_years) for sid, s in ref.staff.items() if s.experience_years is not None}
    path = _history_file()
    try:
        file_key = (str(path), path.stat().st_mtime_ns) if path is not None else None
    except OSError:
        file_key = (str(path), None)
    store_rows = _store_rows(app, normative)
    key = (file_key, id(ref), len(store_rows), sum(r["actual"] for r in store_rows))
    with _lock:
        if _cache.get("key") == key and _cache.get("ref") is ref:
            return _cache["state"]
        rows = (_file_rows(path, normative) if path is not None else []) + store_rows
        if len(rows) < MIN_N:
            state = {"available": False, "reason": NO_HISTORY, "n_total": len(rows),
                     "history_file": str(path) if path is not None else None}
        else:
            state = {"available": True, "model": DurationModel(rows, normative, names, exp),
                     "quality": _evaluate(rows, normative, names, exp)}
        _cache.clear()
        _cache.update(key=key, ref=ref, state=state)
        return state


def _unavailable(st: dict, **extra) -> dict:
    return {"available": False, "reason": st.get("reason") or NO_HISTORY, "n_total": st.get("n_total", 0), **extra}


# ---------------------------------------------------------------- эндпоинты

@router.get("/model")
def forecast_model(request: Request) -> dict:
    st = _model_state(request.app)
    if not st["available"]:
        return _unavailable(st, rows=[])
    m: DurationModel = st["model"]
    table = []
    for wt, s in sorted(m.by_type.items(), key=lambda kv: -kv[1]["n"]):
        norm = m.normative[wt]
        table.append({
            "work_type_code": wt, "work_type_name": m.names.get(wt, wt), "normative_min": norm,
            "mean_min": round(s["mean"], 1), "median_min": round(s["median"], 1), "p80_min": round(s["p80"], 1), "n": s["n"],
            "deviation_pct": round(100 * (s["mean"] - norm) / norm, 1),
            "p80_deviation_pct": round(100 * (s["p80"] - norm) / norm, 1),
            "building_split": m.building_effect.get(wt, False),
            "source": "type" if s["n"] >= MIN_N else "normative",
        })
    return {
        "available": True, "rows": table, "n_total": m.n_rows,
        "experience": {"used": m.exp_used, "slope_per_year": round(m.exp_slope, 5), "correlation": round(m.exp_corr, 4),
                       "t": round(m.exp_t, 2), "t_critical": T_CRIT},
        "quality": st["quality"],
        "note": "План остаётся нормативным (валидатор проверяет duration_min); прогноз — только информация и раннее предупреждение.",
    }


@router.get("/risk")
def forecast_risk(request: Request, date: str | None = None) -> dict:
    """Риск опоздания при p80 для дня `date` (по умолчанию — активный день, как /ui/plan/status)."""
    if date:
        try:
            day_str = dt.date.fromisoformat(date).isoformat()
        except ValueError:
            raise ApiError(400, "BAD_REQUEST", f"Некорректная дата '{date}', ожидается YYYY-MM-DD") from None
    else:
        from . import ui_api
        day_str = ui_api.require_active_day(request).isoformat()
    st = _model_state(request.app)
    if not st["available"]:
        return _unavailable(st, date=day_str, orders=[], at_risk=0)
    svc = request.app.state.service
    plan = svc.get_plan(day_str)
    with svc.store.lock:
        day = svc._day(dt.date.fromisoformat(day_str))
        orders = dict(day.orders)
    m: DurationModel = st["model"]
    items: list[dict] = []
    total_extra = 0.0
    visits = 0
    for route in plan.routes:
        carry = 0.0  # накопленная задержка, мин
        for s in route.stops:
            wait = max(0.0, (s.start - s.arrival).total_seconds() / 60)
            new_shift = max(0.0, carry - wait)  # ожидание слота гасит часть задержки
            if s.type.value == "VISIT" and s.order_id:
                o = orders.get(s.order_id) or orders.get(s.order_id.removesuffix("-B"))
                done = s.status is not None and s.status.value in ("DONE", "IN_PROGRESS")
                extra = 0.0
                if o is not None and not done:
                    visits += 1
                    p = m.predict(o.work_type_code, getattr(o.building_type, "value", o.building_type), route.staff_id)
                    extra = max(0.0, p["p80"] - p["normative"])
                    total_extra += extra
                    if new_shift > 0 and s.start + dt.timedelta(minutes=new_shift) > o.latest_start:
                        over = (s.start + dt.timedelta(minutes=new_shift) - o.latest_start).total_seconds() / 60
                        items.append({
                            "order_id": s.order_id, "staff_id": route.staff_id,
                            "work_type_code": o.work_type_code,
                            "planned_start": s.start.isoformat(), "latest_start": o.latest_start.isoformat(),
                            "forecast_start": (s.start + dt.timedelta(minutes=new_shift)).isoformat(),
                            "accumulated_delay_min": round(new_shift, 1), "lateness_min": round(over, 1),
                            "reason": f"Затяжка предыдущих визитов по p80 копится до +{round(new_shift)} мин; "
                                      f"начало сдвигается за latest_start на {round(over)} мин",
                        })
                carry = new_shift + extra
            else:
                carry = new_shift  # перерыв/пополнение/конец: задержка переносится как есть
    items.sort(key=lambda x: (-x["lateness_min"], x["order_id"]))
    return {"available": True, "date": day_str, "plan_version": plan.plan_version, "visits_checked": visits,
            "extra_p80_total_min": round(total_extra, 1), "at_risk": len(items), "orders": items,
            "note": "Оценка по накоплению разницы p80 − норматив вдоль маршрута; в алерты контракта не добавляется."}
