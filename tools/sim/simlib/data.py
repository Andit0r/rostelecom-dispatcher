"""Данные data/synth и генератор новых нарядов (по правилам tools/synth/generate.py)."""

from __future__ import annotations

import csv
import datetime as dt
import json
import random
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data" / "synth"
TZ = dt.timezone(dt.timedelta(hours=5))
SLOT_HOURS = [9, 11, 13, 15, 17, 19]

ALL_CODES = ["INST_PON_MKD", "INST_FTTB_MKD", "INST_PRIVATE_VLS", "ADD_SERVICE", "SURVEY_PRIVATE",
             "REPAIR", "DEGRADATION", "DISMANTLE"]


def parse_ts(v: str) -> dt.datetime:
    return dt.datetime.fromisoformat(v)


def iso(v: dt.datetime) -> str:
    return v.astimezone(TZ).isoformat()


def _csv_value(field: str, value: str):
    if value in ("null", ""):
        return None
    if field == "profiles":
        return value.split("|")
    if field in ("duration_min", "max_start_delay_min", "deadline_value", "repeat_deadline_value"):
        return int(value)
    if field == "pgp_relevant":
        return value == "true"
    return value


def add_working_days(d: dt.datetime, n: int) -> dt.datetime:
    left = n
    while left > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


_ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_DATE = re.compile(r"(ORD|INC|EV|TR)-(\d{8})")


def next_workday(d: dt.date) -> dt.date:
    while d.weekday() >= 5:
        d += dt.timedelta(days=1)
    return d


def default_date() -> str:
    """Сегодня по системным часам (Екатеринбург), в выходной — ближайший рабочий день (как tools/synth/generate.py)."""
    return next_workday(dt.datetime.now(TZ).date()).isoformat()


def _compute_deadline(wt: dict, registered: dt.datetime, latest: dt.datetime, is_repeat: bool) -> dt.datetime:
    kind = wt["deadline_kind"]
    if kind == "SLOT":
        return latest + dt.timedelta(minutes=wt["duration_min"])
    val = wt["repeat_deadline_value"] if (is_repeat and wt["repeat_deadline_value"]) else wt["deadline_value"]
    if kind == "HOURS":
        return registered + dt.timedelta(hours=val)
    return add_working_days(registered, val)


def retarget_order(order: dict, src: dt.date, dst: dt.date, wt: dict[str, dict]) -> dict:
    """Перенос наряда эталонного дня src на дату dst (логика tools/synth/generate.py, retarget_order):
    все даты и время сдвигаются на одинаковое число суток, идентификаторы ORD/INC/EV/TR-YYYYMMDD с датой src
    переименовываются, контрольный срок в рабочих днях пересчитывается по календарю новой даты."""
    delta = dt.timedelta(days=(dst - src).days)
    ids = {src.strftime("%Y%m%d"): dst.strftime("%Y%m%d")}

    def shift(v):
        if isinstance(v, str):
            if _ISO_TS.match(v):
                return iso(parse_ts(v) + delta)
            if _ISO_DATE.match(v):
                return (dt.date.fromisoformat(v) + delta).isoformat()
            return _ID_DATE.sub(lambda m: f"{m.group(1)}-{ids.get(m.group(2), m.group(2))}", v)
        if isinstance(v, list):
            return [shift(x) for x in v]
        if isinstance(v, dict):
            return {k: shift(x) for k, x in v.items()}
        return v

    res = shift(order)
    w = wt[res["work_type_code"]]
    registered, latest = parse_ts(res["registered_at"]), parse_ts(res["latest_start"])
    deadline = _compute_deadline(w, registered, latest, bool(res.get("is_repeat")))
    res["deadline"] = iso(max(deadline, latest + dt.timedelta(minutes=w["duration_min"])))
    return res


class SynthData:
    def __init__(self, data_dir: Path = DATA) -> None:
        self.dir = data_dir
        with open(data_dir / "work_types.csv", encoding="utf-8", newline="") as fh:
            work_types = [{k: _csv_value(k, v) for k, v in row.items()} for row in csv.DictReader(fh)]
        self.reference = {
            "service_centers": json.loads((data_dir / "service_centers.json").read_text(encoding="utf-8")),
            "staff": json.loads((data_dir / "staff.json").read_text(encoding="utf-8")),
            "access_nodes": json.loads((data_dir / "access_nodes.json").read_text(encoding="utf-8")),
            "work_types": work_types,
        }
        self.wt = {w["code"]: w for w in work_types}
        self.staff_ids = [s["staff_id"] for s in self.reference["staff"]]
        self._matrix_raw: bytes | None = None
        self._orders: list[dict] | None = None
        self.pairs: list[tuple[dict, dict]] = []  # (дом, узел)
        for n in self.reference["access_nodes"]:
            for b in n["buildings"]:
                self.pairs.append((b, n))
        self.nodes = {n["access_node_id"]: n for n in self.reference["access_nodes"]}

    @property
    def matrix_raw(self) -> bytes:
        if self._matrix_raw is None:
            self._matrix_raw = (self.dir / "travel_matrix.json").read_bytes()
        return self._matrix_raw

    @property
    def train(self) -> list[dict]:
        if self._orders is None:
            self._orders = json.loads((self.dir / "orders_train.json").read_text(encoding="utf-8"))
        return self._orders

    def day_orders(self, date: str, now: dt.datetime, scale: float = 1.0,
                   rng: random.Random | None = None) -> list[dict]:
        """Наряды обучающего дня со слотом в `date` и registered_at <= now; scale<1 — случайная подвыборка."""
        res = [o for o in self.train if o["slot"]["start"][:10] == date and parse_ts(o["registered_at"]) <= now]
        res.sort(key=lambda o: o["order_id"])
        if scale < 1.0:
            rng = rng or random.Random(0)
            k = max(1, int(len(res) * scale))
            res = sorted(rng.sample(res, k), key=lambda o: o["order_id"])
        return res

    def train_dates(self) -> list[str]:
        return sorted({o["slot"]["start"][:10] for o in self.train})

    def reference_day(self) -> str:
        """Эталонный день для --date: демо-день events_dense.json (он же train_dates()[5])."""
        try:
            return json.loads((self.dir / "events_dense.json").read_text(encoding="utf-8"))["date"]
        except (OSError, ValueError, KeyError):
            return self.train_dates()[0]

    def retarget(self, target: str) -> dict:
        """Один и тот же набор нарядов эталонного дня — на дату target: наряды со слотом в target заменяются
        перенесёнными нарядами эталонного дня. Возвращает {reference, target, orders, weekend}."""
        src = self.reference_day()
        dst = dt.date.fromisoformat(target)
        ref_orders = [o for o in self.train if o["slot"]["start"][:10] == src]
        moved = [retarget_order(o, dt.date.fromisoformat(src), dst, self.wt) for o in ref_orders]
        self._orders = [o for o in self.train if o["slot"]["start"][:10] != target] + moved
        return {"reference": src, "target": target, "orders": len(moved), "weekend": dst.weekday() >= 5}

    def compatible(self, code: str, b: dict, n: dict) -> bool:
        if code == "INST_PON_MKD":
            return n["technology"] == "PON" and b["building_type"] == "MKD"
        if code == "INST_FTTB_MKD":
            return n["technology"] == "FTTB" and b["building_type"] == "MKD"
        if code == "INST_PRIVATE_VLS":
            return b["building_type"] == "PRIVATE" and b["overhead_line"]
        if code == "SURVEY_PRIVATE":
            return b["building_type"] == "PRIVATE"
        return True

    def pick_building(self, rng: random.Random, code: str, node_id: str | None = None,
                      avoid_nodes: set[str] | None = None) -> tuple[dict, dict]:
        cands = [(b, n) for b, n in self.pairs if self.compatible(code, b, n)
                 and (node_id is None or n["access_node_id"] == node_id)
                 and not (avoid_nodes and n["access_node_id"] in avoid_nodes)]
        if not cands:
            raise LookupError(f"нет дома под {code} node={node_id}")
        return rng.choice(cands)


class OrderFactory:
    """Новые наряды для NEW_ORDER: id `ORD-YYYYMMDD-NNNN` с номера 9001, слот - целый час с запасом."""

    def __init__(self, data: SynthData, rng: random.Random, date: str, start_no: int = 9000) -> None:
        self.data, self.rng, self.date = data, rng, date
        self.no = start_no

    def make(self, registered: dt.datetime, code: str | None = None, *, node_id: str | None = None,
             avoid_nodes: set[str] | None = None, lead_min: int = 90, is_repeat: bool = False,
             slot_hour: int | None = None) -> dict:
        d, rng = self.data, self.rng
        code = code or rng.choice(ALL_CODES)
        b, n = d.pick_building(rng, code, node_id, avoid_nodes)
        wt = d.wt[code]
        day = dt.date.fromisoformat(self.date)
        earliest = registered + dt.timedelta(minutes=lead_min)
        if slot_hour is None:
            hours = [h for h in SLOT_HOURS if dt.datetime.combine(day, dt.time(h), tzinfo=TZ) >= earliest]
            slot_hour = hours[0] if hours else SLOT_HOURS[-1]
        slot_start = dt.datetime.combine(day, dt.time(slot_hour), tzinfo=TZ)
        slot_end = slot_start + dt.timedelta(hours=2)
        delay = wt["max_start_delay_min"]
        latest = slot_end if delay is None else min(slot_end, slot_start + dt.timedelta(minutes=delay))
        kind = wt["deadline_kind"]
        if kind == "SLOT":
            deadline = latest + dt.timedelta(minutes=wt["duration_min"])
        else:
            val = wt["repeat_deadline_value"] if (is_repeat and wt["repeat_deadline_value"]) else wt["deadline_value"]
            deadline = (registered + dt.timedelta(hours=val)) if kind == "HOURS" else add_working_days(registered, val)
        eq = {"ONT": 0, "ROUTER": 0, "STB": 0}
        tech = n["technology"]
        if code == "INST_PON_MKD":
            eq.update(ONT=1, ROUTER=1, STB=rng.choice([0, 1]))
        elif code == "INST_FTTB_MKD":
            eq.update(ROUTER=1, STB=rng.choice([0, 1]))
        elif code == "INST_PRIVATE_VLS":
            eq.update(ONT=1 if tech == "PON" else 0, ROUTER=1, STB=rng.choice([0, 1]))
        elif code == "ADD_SERVICE":
            eq["ROUTER" if rng.random() < 0.5 else "STB"] = 1
        elif code in ("REPAIR", "DEGRADATION"):
            eq["ONT"] = 1 if tech == "PON" else 0
            eq["ROUTER"] = 1 if rng.random() < 0.35 else 0
        self.no += 1
        return {
            "order_id": f"ORD-{day:%Y%m%d}-{self.no:04d}", "work_type_code": code, "technology": tech,
            "building_id": b["building_id"], "address": b["address"], "lat": b["lat"], "lon": b["lon"],
            "building_type": b["building_type"],
            "requires_height": b["building_type"] == "PRIVATE" and bool(b["overhead_line"]),
            "access_node_id": n["access_node_id"], "registered_at": iso(registered),
            "slot": {"start": iso(slot_start), "end": iso(slot_end)}, "latest_start": iso(latest),
            "deadline": iso(deadline), "required_equipment": eq, "is_repeat": is_repeat,
            "previous_order_id": None,
        }


def expected_pgp(orders: list[dict], wt: dict[str, dict]) -> list[set[str]]:
    """Независимый детектор эталона: >=3 pgp_relevant наряда одного узла в окне 60 мин по registered_at.
    Соседние отмеченные наряды с разрывом <= 60 мин склеиваются в одну группу."""
    by_node: dict[str, list[dict]] = {}
    for o in orders:
        if wt[o["work_type_code"]]["pgp_relevant"]:
            by_node.setdefault(o["access_node_id"], []).append(o)
    groups: list[set[str]] = []
    w = dt.timedelta(minutes=60)
    for items in by_node.values():
        items = sorted(items, key=lambda o: (parse_ts(o["registered_at"]), o["order_id"]))
        regs = [parse_ts(o["registered_at"]) for o in items]
        marked = [False] * len(items)
        for lo in range(len(items)):
            hi = lo
            while hi + 1 < len(items) and regs[hi + 1] - regs[lo] <= w:
                hi += 1
            if hi - lo + 1 >= 3:
                for j in range(lo, hi + 1):
                    marked[j] = True
        cur: list[int] = []
        for j in range(len(items)):
            if not marked[j]:
                continue
            if cur and regs[j] - regs[cur[-1]] > w:
                groups.append({items[k]["order_id"] for k in cur})
                cur = []
            cur.append(j)
        if cur:
            groups.append({items[k]["order_id"] for k in cur})
    return groups
