#!/usr/bin/env python3
"""Генератор синтетических данных для кейса «Умная диспетчеризация выездных бригад».

Запуск из корня репозитория:  python tools/synth/generate.py [--base-date YYYY-MM-DD] [--dates-only] [--dense]

  --base-date  «сегодняшний» день (демо-день, dense). По умолчанию — сегодня по системным часам,
               а если это выходной — ближайший следующий рабочий день (пн–пт).
  --dates-only перегенерировать только файлы с датами (orders_train.json, events_example.json,
               events_dense.json); справочники и матрица не пишутся, а сгенерированные в памяти
               сверяются с файлами байт-в-байт (расхождение — ошибка).
  --dense      только events_dense.json (читает уже записанный orders_train.json).

Пишет в data/synth/:
  service_centers.json, staff.json, access_nodes.json, work_types.csv,
  travel_matrix.json, orders_train.json, events_example.json

Генерация детерминирована (фиксированный SEED). Каждый файл после записи
перечитывается и проходит валидацию моделями backend/app/models.py.
Зависимости генератора: стандартная библиотека и pydantic (из backend).
"""

from __future__ import annotations

import csv
import json
import math
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "synth"
BACKEND_DIR = ROOT / "backend"

SEED = 20261026
TZ = timezone(timedelta(hours=5))  # Екатеринбург, UTC+5 (без перехода на летнее время)

# Рабочие дни набора (пн–пт; праздники не учитываются по ТЗ): 5 дней до базового, сам базовый
# (демо-день «сегодня», индекс BASE_IDX) и 4 дня после него.
# Наряды всегда генерируются на эталонном календаре CANON_DATES (так при одном SEED набор нарядов
# один и тот же, бенчмарки сопоставимы), после чего даты переносятся на реальные TRAIN_DATES
# (retarget_*): сдвигаются только даты/идентификаторы с датой, дедлайны «в рабочих днях»
# пересчитываются по реальному календарю.
CANON_DATES = [date(2026, 10, d) for d in (1, 2, 5, 6, 7, 8, 9, 12, 13, 14)]
N_DAYS = len(CANON_DATES)
BASE_IDX = 5
EXAMPLE_IDX = 2  # день сценария events_example.json
TRAIN_DATES: list[date] = []  # реальные даты, заполняются configure_dates()
ORDERS_PER_DAY = (330, 370)
SLOT_HOURS = [9, 11, 13, 15, 17, 19]
SLOT_HOUR_WEIGHTS = [0.20, 0.20, 0.20, 0.20, 0.13, 0.07]

# Индекс дня в TRAIN_DATES -> число ПГП-кластеров в этот день (кластеры есть в нескольких днях).
PGP_CLUSTERS = {1: 1, 3: 1, 5: 2, 8: 1}
P_REPEAT_REPAIR = 0.33  # при 24% REPAIR это даёт ~8% повторных нарядов

LAT_RANGE = (56.75, 56.93)
LON_RANGE = (60.45, 60.80)
NODE_COUNTS = {"PON": 50, "FTTB": 30, "XDSL": 35, "DOCSIS": 25}  # 140 узлов
N_BUILDINGS = 1260  # уникальных домов -> матрица 1265 x 1265 (5 СЦ + 1260 домов)
P_DUAL_TECH = 0.08  # доля домов, подключённых к узлам двух технологий
ROAD_FACTOR = 1.35
AVG_SPEED_KMH = 25.0
PARKING_SEC = 120
TECHS = ["PON", "FTTB", "XDSL", "DOCSIS"]
NODE_PREFIX = {"PON": ("OLT", "OLT"), "FTTB": ("SWITCH", "SWITCH"), "XDSL": ("DSLAM", "DSLAM"), "DOCSIS": ("CMTS", "CMTS")}

SHIFT_DAY = {"start": "09:00", "end": "18:00", "break_duration_min": 60}
SHIFT_LATE = {"start": "12:00", "end": "21:00", "break_duration_min": 60}

SERVICE_CENTERS = [
    {"service_center_id": "SC-01", "name": "СЦ «Север»", "address": "г. Екатеринбург, ул. Победы, 30",
     "lat": 56.8941, "lon": 60.6105, "open": "08:00", "close": "21:00", "restock": 15},
    {"service_center_id": "SC-02", "name": "СЦ «Восток»", "address": "г. Екатеринбург, ул. Машиностроителей, 29",
     "lat": 56.8790, "lon": 60.6480, "open": "08:00", "close": "21:00", "restock": 15},
    {"service_center_id": "SC-03", "name": "СЦ «Центр»", "address": "г. Екатеринбург, ул. Малышева, 51",
     "lat": 56.8390, "lon": 60.6040, "open": "09:00", "close": "20:00", "restock": 20},
    {"service_center_id": "SC-04", "name": "СЦ «Запад»", "address": "г. Екатеринбург, ул. Бардина, 21",
     "lat": 56.8120, "lon": 60.5240, "open": "08:00", "close": "20:00", "restock": 15},
    {"service_center_id": "SC-05", "name": "СЦ «Юг»", "address": "г. Екатеринбург, ул. Волгоградская, 170",
     "lat": 56.7880, "lon": 60.5950, "open": "08:00", "close": "21:00", "restock": 15},
]
SC_IDS = [sc["service_center_id"] for sc in SERVICE_CENTERS]

DISTRICTS = [
    "Уралмаш", "Втузгородок", "Пионерский", "Вокзальный", "Шарташ", "Химмаш", "Эльмаш",
    "Ботанический", "Академический", "Уктус", "Широкая Речка", "Автовокзал", "Юго-Западный",
    "Новая Сортировка", "Заречный", "Старая Сортировка", "Компрессорный", "Лечебный",
]
STREETS = [
    "Учителей", "Уральская", "Белинского", "Бебеля", "Малышева", "Вайнера", "Щорса", "Фрунзе",
    "Посадская", "Громова", "Тверитина", "Ключевская", "Шейнкмана", "Народной Воли", "Декабристов",
    "Студенческая", "Куйбышева", "Сибирский тракт", "Восточная", "Блюхера", "Машиностроителей",
    "Бардина", "Мамина-Сибиряка", "Вулканическая", "Водников", "Кузнецова", "Бакинских Комиссаров",
]

# Типы работ — по таблице из ТЗ (п. 4). Вес — доля в генерации, в справочник не попадает.
WORK_TYPES = [
    {"code": "INST_PON_MKD", "name": "Инсталляция ШПД/IPTV, МКД, PON", "profiles": ["INSTALL"],
     "duration_min": 90, "max_start_delay_min": 10, "deadline_kind": "WORKING_DAYS", "deadline_value": 3,
     "repeat_deadline_value": None, "pgp_relevant": False, "weight": 0.12},
    {"code": "INST_FTTB_MKD", "name": "Инсталляция ШПД/IPTV, МКД, FTTB", "profiles": ["INSTALL"],
     "duration_min": 60, "max_start_delay_min": 10, "deadline_kind": "WORKING_DAYS", "deadline_value": 3,
     "repeat_deadline_value": None, "pgp_relevant": False, "weight": 0.10},
    {"code": "INST_PRIVATE_VLS", "name": "Инсталляция, частный сектор, с ВЛС", "profiles": ["INSTALL"],
     "duration_min": 180, "max_start_delay_min": 10, "deadline_kind": "WORKING_DAYS", "deadline_value": 5,
     "repeat_deadline_value": None, "pgp_relevant": False, "weight": 0.04},
    {"code": "ADD_SERVICE", "name": "Доподключение услуги (IPTV, Wi-Fi-роутер)", "profiles": ["INSTALL"],
     "duration_min": 40, "max_start_delay_min": None, "deadline_kind": "SLOT", "deadline_value": None,
     "repeat_deadline_value": None, "pgp_relevant": False, "weight": 0.20},
    {"code": "SURVEY_PRIVATE", "name": "Обследование (частный сектор)", "profiles": ["INSTALL", "LTP3"],
     "duration_min": 45, "max_start_delay_min": None, "deadline_kind": "WORKING_DAYS", "deadline_value": 2,
     "repeat_deadline_value": None, "pgp_relevant": False, "weight": 0.14},
    {"code": "REPAIR", "name": "Устранение неисправности (обращение абонента)", "profiles": ["LTP3"],
     "duration_min": 60, "max_start_delay_min": None, "deadline_kind": "HOURS", "deadline_value": 24,
     "repeat_deadline_value": 12, "pgp_relevant": True, "weight": 0.24},
    {"code": "DEGRADATION", "name": "Устранение деградации, выявленной системой мониторинга качества",
     "profiles": ["LTP3"], "duration_min": 45, "max_start_delay_min": None, "deadline_kind": "HOURS",
     "deadline_value": 48, "repeat_deadline_value": None, "pgp_relevant": True, "weight": 0.10},
    {"code": "DISMANTLE", "name": "Демонтаж АО", "profiles": ["LTP3"], "duration_min": 30,
     "max_start_delay_min": None, "deadline_kind": "WORKING_DAYS", "deadline_value": 5,
     "repeat_deadline_value": None, "pgp_relevant": False, "weight": 0.06},
]
WT_BY_CODE = {wt["code"]: wt for wt in WORK_TYPES}
WT_CSV_FIELDS = [
    "code", "name", "profiles", "duration_min", "max_start_delay_min", "deadline_kind",
    "deadline_value", "repeat_deadline_value", "pgp_relevant",
]

# Допустимые ожидания «отпуска» наряда: время от регистрации до начала слота, мин.
LEAD_RANGE_MIN = {
    "INST": (60, 4 * 24 * 60),
    "REPAIR": (30, 22 * 60),
    "REPAIR_REPEAT": (30, 10 * 60),
    "DEGRADATION": (60, 40 * 60),
    "DISMANTLE": (60, 6 * 24 * 60),
    "ADD_SERVICE": (60, 6 * 24 * 60),
    "SURVEY_PRIVATE": (60, 3 * 24 * 60),
}


# ---------------------------------------------------------------- calendar

def next_workday(d: date) -> date:
    """d, если это пн–пт, иначе ближайший следующий рабочий день."""
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def default_base_date() -> date:
    return next_workday(datetime.now(TZ).date())


def workday_shift(d: date, n: int) -> date:
    """Рабочий день на n рабочих дней позже (n<0 — раньше) рабочего дня d."""
    step = 1 if n > 0 else -1
    left = abs(n)
    while left:
        d += timedelta(days=step)
        if d.weekday() < 5:
            left -= 1
    return d


def configure_dates(base: date) -> None:
    """Заполняет TRAIN_DATES вокруг базового (рабочего) дня."""
    if base.weekday() >= 5:
        raise SystemExit(f"--base-date {base} — выходной; нужен рабочий день (пн–пт)")
    TRAIN_DATES[:] = [workday_shift(base, i - BASE_IDX) if i != BASE_IDX else base for i in range(N_DAYS)]


# ---------------------------------------------------------------- helpers

def iso(dt: datetime) -> str:
    return dt.isoformat()


def clamp(value: float, bounds: tuple[float, float]) -> float:
    return min(max(value, bounds[0]), bounds[1])


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def add_working_days(dt: datetime, n: int) -> datetime:
    result = dt
    left = n
    while left > 0:
        result += timedelta(days=1)
        if result.weekday() < 5:
            left -= 1
    return result


def compute_deadline(wt: dict, registered: datetime, latest: datetime, is_repeat: bool) -> datetime:
    kind = wt["deadline_kind"]
    if kind == "SLOT":
        return latest + timedelta(minutes=wt["duration_min"])
    value = wt["repeat_deadline_value"] if (is_repeat and wt["repeat_deadline_value"]) else wt["deadline_value"]
    if kind == "HOURS":
        return registered + timedelta(hours=value)
    return add_working_days(registered, value)


def latest_start_of(slot_start: datetime, slot_end: datetime, max_delay: int | None) -> datetime:
    if max_delay is None:
        return slot_end
    return min(slot_end, slot_start + timedelta(minutes=max_delay))


def lead_range(code: str, is_repeat: bool) -> tuple[int, int]:
    if code == "REPAIR" and is_repeat:
        return LEAD_RANGE_MIN["REPAIR_REPEAT"]
    if code in ("INST_PON_MKD", "INST_FTTB_MKD", "INST_PRIVATE_VLS"):
        return LEAD_RANGE_MIN["INST"]
    return LEAD_RANGE_MIN[code]


# ---------------------------------------------------------------- staff

def gen_staff(rng: random.Random) -> list[dict]:
    staff: list[dict] = []
    for i in range(1, 41):
        profile = "LTP3" if i <= 28 else "INSTALL"
        if profile == "LTP3":
            techs = rng.sample(TECHS, rng.choice([2, 3, 3, 4]))
        else:
            techs = rng.sample(TECHS, rng.choice([2, 3]))
        late = rng.random() < 0.45
        base = SHIFT_LATE if late else SHIFT_DAY
        if late:
            window_from = rng.choice(["15:00", "16:00"])
        else:
            window_from = rng.choice(["12:00", "13:00"])
        window_to = f"{int(window_from[:2]) + 2:02d}:00"
        staff.append({
            "staff_id": f"ST-{i:03d}",
            "display_name": f"Техник ЗЛТП {i:03d}" if profile == "LTP3" else f"Инсталлятор {i:03d}",
            "service_center_id": SC_IDS[(i - 1) % len(SC_IDS)],
            "profiles": [profile],
            "technologies": sorted(techs, key=TECHS.index),
            "height_permit": False,
            "shift": {
                "start": base["start"],
                "end": base["end"],
                "break_duration_min": base["break_duration_min"],
                "break_window": {"from": window_from, "to": window_to},
            },
            "equipment_start": {
                "ONT": rng.randint(2, 4),
                "ROUTER": rng.randint(3, 5),
                "STB": rng.randint(2, 3),
            },
            "equipment_capacity": {"ONT": 6, "ROUTER": 6, "STB": 4},
            "experience_years": round(rng.uniform(0.5, 14.0), 1),
        })

    # Допуск к высоте: у части инсталляторов и ЗЛТП.
    ltp = [s for s in staff if s["profiles"] == ["LTP3"]]
    inst = [s for s in staff if s["profiles"] == ["INSTALL"]]
    for s in rng.sample(ltp, 6) + rng.sample(inst, 6):
        s["height_permit"] = True

    # Покрытие технологий: каждая технология есть хотя бы у нескольких сотрудников профиля.
    for pool, minimum in ((ltp, 6), (inst, 3)):
        for tech in TECHS:
            have = [s for s in pool if tech in s["technologies"]]
            for s in rng.sample([x for x in pool if tech not in x["technologies"]], max(0, minimum - len(have))):
                s["technologies"] = sorted(s["technologies"] + [tech], key=TECHS.index)
    return staff


# ---------------------------------------------------------------- network

def _nearest(points: list[tuple[float, float]], lat: float, lon: float, cos_lat: float) -> int:
    best, best_d = 0, float("inf")
    for idx, (plat, plon) in enumerate(points):
        d = (plat - lat) ** 2 + ((plon - lon) * cos_lat) ** 2
        if d < best_d:
            best, best_d = idx, d
    return best


def gen_network(rng: random.Random) -> tuple[list[dict], dict[str, dict]]:
    """Возвращает (узлы доступа, словарь домов по building_id)."""
    cos_lat = math.cos(math.radians(56.84))
    anchors = [(sc["lat"], sc["lon"]) for sc in SERVICE_CENTERS]
    anchors += [(rng.uniform(*LAT_RANGE), rng.uniform(*LON_RANGE)) for _ in range(12)]

    raw: list[dict] = []
    for _ in range(N_BUILDINGS):
        a_lat, a_lon = rng.choice(anchors)
        lat = round(clamp(rng.gauss(a_lat, 0.018), LAT_RANGE), 6)
        lon = round(clamp(rng.gauss(a_lon, 0.03), LON_RANGE), 6)
        btype = "MKD" if rng.random() < 0.55 else "PRIVATE"
        if btype == "MKD":
            tech = "PON" if rng.random() < 0.55 else "FTTB"
        else:
            r = rng.random()
            tech = "XDSL" if r < 0.40 else ("DOCSIS" if r < 0.70 else "PON")
        raw.append({
            "lat": lat, "lon": lon, "building_type": btype, "tech": tech,
            "overhead_line": btype == "PRIVATE" and rng.random() < 0.35,
            "street": rng.choice(STREETS), "house": rng.randint(1, 140),
        })

    # Идентификаторы домов: B-00001..B-01260 в случайном порядке.
    building_numbers = list(range(1, N_BUILDINGS + 1))
    rng.shuffle(building_numbers)
    for b, number in zip(raw, building_numbers):
        b["building_id"] = f"B-{number:05d}"

    # Узлы доступа: k-means внутри каждой технологии.
    nodes_raw: list[dict] = []
    for tech, count in NODE_COUNTS.items():
        members = [b for b in raw if b["tech"] == tech]
        seeds = rng.sample(members, count)
        centers = [(s["lat"], s["lon"]) for s in seeds]
        assign: list[int] = []
        for _ in range(4):
            assign = [_nearest(centers, b["lat"], b["lon"], cos_lat) for b in members]
            for idx in range(count):
                pts = [(b["lat"], b["lon"]) for b, a in zip(members, assign) if a == idx]
                if pts:
                    centers[idx] = (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))
        # Пустых узлов быть не должно: переносим ближайший дом от самого крупного узла.
        for _ in range(count):
            sizes = [assign.count(i) for i in range(count)]
            empty = [i for i, s in enumerate(sizes) if s == 0]
            if not empty:
                break
            donor = max(range(count), key=lambda i: sizes[i])
            target = empty[0]
            cand = [k for k, a in enumerate(assign) if a == donor]
            move = min(cand, key=lambda k: (members[k]["lat"] - centers[target][0]) ** 2
                       + ((members[k]["lon"] - centers[target][1]) * cos_lat) ** 2)
            assign[move] = target
        for idx in range(count):
            nodes_raw.append({"tech": tech, "members": [m for m, a in zip(members, assign) if a == idx]})

    # Дома с двумя технологиями: второй узел другой технологии.
    dual_extra: list[tuple[dict, str]] = []
    for b in raw:
        if rng.random() < P_DUAL_TECH:
            other = rng.choice([t for t in TECHS if t != b["tech"]])
            dual_extra.append((b, other))

    # Центроиды узлов (для привязки второго узла и выбора СЦ).
    for node in nodes_raw:
        node["lat"] = round(sum(m["lat"] for m in node["members"]) / len(node["members"]), 6)
        node["lon"] = round(sum(m["lon"] for m in node["members"]) / len(node["members"]), 6)
    for b, other in dual_extra:
        cands = [n for n in nodes_raw if n["tech"] == other]
        target = min(cands, key=lambda n: (n["lat"] - b["lat"]) ** 2 + ((n["lon"] - b["lon"]) * cos_lat) ** 2)
        target["members"].append(b)

    # Идентификаторы узлов AN-0001..AN-0140 в случайном порядке.
    node_numbers = list(range(1, sum(NODE_COUNTS.values()) + 1))
    rng.shuffle(node_numbers)

    sc_points = [(sc["lat"], sc["lon"]) for sc in SERVICE_CENTERS]
    buildings: dict[str, dict] = {}
    for b in raw:
        zone = SC_IDS[_nearest(sc_points, b["lat"], b["lon"], cos_lat)]
        buildings[b["building_id"]] = {
            "building_id": b["building_id"],
            "address": f"г. Екатеринбург, ул. {b['street']}, {b['house']}",
            "lat": b["lat"],
            "lon": b["lon"],
            "building_type": b["building_type"],
            "overhead_line": b["overhead_line"],
            "zone_service_center_id": zone,
        }

    access_nodes: list[dict] = []
    for node, number in zip(nodes_raw, node_numbers):
        prefix, node_type = NODE_PREFIX[node["tech"]]
        district = rng.choice(DISTRICTS)
        members = sorted({m["building_id"]: m for m in node["members"]}.values(),
                         key=lambda m: m["building_id"])
        access_nodes.append({
            "access_node_id": f"AN-{number:04d}",
            "node_type": node_type,
            "technology": node["tech"],
            "name": f"{prefix} {district}-{rng.randint(1, 9)}",
            "address": f"г. Екатеринбург, ул. {rng.choice(STREETS)}, {rng.randint(1, 140)}",
            "lat": node["lat"],
            "lon": node["lon"],
            "buildings": [dict(buildings[m["building_id"]]) for m in members],
        })
    access_nodes.sort(key=lambda n: n["access_node_id"])
    return access_nodes, buildings


# ---------------------------------------------------------------- matrix

def gen_matrix(rng: random.Random, buildings: dict[str, dict]) -> dict:
    points = [(sc["service_center_id"], sc["lat"], sc["lon"]) for sc in SERVICE_CENTERS]
    for bid in sorted(buildings):
        b = buildings[bid]
        points.append((bid, b["lat"], b["lon"]))
    n = len(points)
    dist = [[0] * n for _ in range(n)]
    dur = [[0] * n for _ in range(n)]
    for i in range(n):
        _, lat1, lon1 = points[i]
        for j in range(n):
            if i == j:
                continue
            _, lat2, lon2 = points[j]
            base = haversine_m(lat1, lon1, lat2, lon2) * ROAD_FACTOR
            # Небольшая несимметричность: каждое направление имеет свой коэффициент.
            d = max(1, round(base * rng.uniform(0.96, 1.04)))
            dist[i][j] = d
            dur[i][j] = round(d / (AVG_SPEED_KMH / 3.6) + PARKING_SEC)
    return {
        "matrix_id": "ekb-2026-10-v1",
        "points": [p[0] for p in points],
        "durations_sec": dur,
        "distances_m": dist,
        "hourly_factors": [
            {"hour": 7, "factor": 1.25}, {"hour": 8, "factor": 1.35}, {"hour": 9, "factor": 1.2},
            {"hour": 12, "factor": 1.1}, {"hour": 13, "factor": 1.1}, {"hour": 16, "factor": 1.15},
            {"hour": 17, "factor": 1.3}, {"hour": 18, "factor": 1.2},
        ],
    }


# ---------------------------------------------------------------- orders

@dataclass
class Rec:
    order_id: str
    day: date
    wt: dict
    building: dict
    node: dict
    registered: datetime
    slot_start: datetime
    slot_end: datetime
    latest: datetime
    deadline: datetime
    is_repeat: bool
    previous_order_id: str | None
    equipment: dict
    cluster_id: str | None = None

    def to_order(self) -> dict:
        b, n = self.building, self.node
        return {
            "order_id": self.order_id,
            "work_type_code": self.wt["code"],
            "technology": n["technology"],
            "building_id": b["building_id"],
            "address": b["address"],
            "lat": b["lat"],
            "lon": b["lon"],
            "building_type": b["building_type"],
            "requires_height": b["building_type"] == "PRIVATE" and b["overhead_line"],
            "access_node_id": n["access_node_id"],
            "registered_at": iso(self.registered),
            "slot": {"start": iso(self.slot_start), "end": iso(self.slot_end)},
            "latest_start": iso(self.latest),
            "deadline": iso(self.deadline),
            "required_equipment": dict(self.equipment),
            "is_repeat": self.is_repeat,
            "previous_order_id": self.previous_order_id,
        }


@dataclass
class Gen:
    rng: random.Random
    staff: list[dict]
    pairs_by_type: dict[str, list[tuple[dict, dict]]]
    counters: dict[date, int] = field(default_factory=dict)
    node_regs: dict[str, list[datetime]] = field(default_factory=dict)
    repair_history: list[Rec] = field(default_factory=list)
    records: list[Rec] = field(default_factory=list)

    def next_order_id(self, day: date) -> str:
        self.counters[day] = self.counters.get(day, 0) + 1
        return f"ORD-{day:%Y%m%d}-{self.counters[day]:04d}"

    def pgp_ok(self, node_id: str, t: datetime) -> bool:
        """Не более одного нарядов ПГП-типа с узла в пределах ±60 мин от t.

        Гарантирует: в любом окне 60 минут не будет трёх нарядов с одного узла
        (фоновые совпадения ≤ 2), если все пары проверяются при добавлении.
        """
        close = sum(1 for x in self.node_regs.get(node_id, []) if abs((x - t).total_seconds()) <= 3600)
        return close < 2

    def make_record(
        self,
        wt: dict,
        building: dict,
        node: dict,
        day: date,
        *,
        registered: datetime | None = None,
        repeat_of: Rec | None = None,
        attempts: int = 80,
    ) -> Rec | None:
        is_repeat = repeat_of is not None
        for _ in range(attempts):
            hour = self.rng.choices(SLOT_HOURS, SLOT_HOUR_WEIGHTS)[0]
            slot_start = datetime.combine(day, time(hour), tzinfo=TZ)
            slot_end = slot_start + timedelta(hours=2)
            latest = latest_start_of(slot_start, slot_end, wt["max_start_delay_min"])
            if registered is not None:
                reg = registered
            else:
                lo, hi = lead_range(wt["code"], is_repeat)
                lead = self.rng.uniform(lo, hi)
                reg = (slot_start - timedelta(minutes=lead)).replace(second=0, microsecond=0)
            if not 7 <= reg.hour < 22:
                continue
            if slot_start - reg < timedelta(minutes=30):
                continue
            deadline = compute_deadline(wt, reg, latest, is_repeat)
            if latest + timedelta(minutes=wt["duration_min"]) > deadline:
                continue
            return Rec(
                order_id=self.next_order_id(day),
                day=day,
                wt=wt,
                building=building,
                node=node,
                registered=reg,
                slot_start=slot_start,
                slot_end=slot_end,
                latest=latest,
                deadline=deadline,
                is_repeat=is_repeat,
                previous_order_id=repeat_of.order_id if repeat_of else None,
                equipment=self.equipment_for(wt["code"], node["technology"]),
            )
        return None

    def equipment_for(self, code: str, tech: str) -> dict:
        eq = {"ONT": 0, "ROUTER": 0, "STB": 0}
        r = self.rng
        if code == "INST_PON_MKD":
            eq.update(ONT=1, ROUTER=1, STB=1 if r.random() < 0.5 else 0)
        elif code == "INST_FTTB_MKD":
            eq.update(ROUTER=1, STB=1 if r.random() < 0.6 else 0)
        elif code == "INST_PRIVATE_VLS":
            eq.update(ONT=1 if tech == "PON" else 0, ROUTER=1, STB=1 if r.random() < 0.5 else 0)
        elif code == "ADD_SERVICE":
            if r.random() < 0.5:
                eq["ROUTER"] = 1
            else:
                eq["STB"] = 1
        elif code in ("REPAIR", "DEGRADATION"):
            eq["ONT"] = 1 if tech == "PON" else 0
            eq["ROUTER"] = 1 if r.random() < 0.35 else 0
        return eq

    def add(self, rec: Rec) -> None:
        self.records.append(rec)
        if rec.wt["pgp_relevant"]:
            self.node_regs.setdefault(rec.node["access_node_id"], []).append(rec.registered)
        if rec.wt["code"] == "REPAIR":
            self.repair_history.append(rec)

    def make_cluster(self, day: date, cluster_no: int, used_nodes: set[str], big_nodes: list[dict]) -> list[Rec]:
        """ПГП-кластер: 3–4 наряда на неисправность с одного узла за ≤ 40 минут."""
        for _ in range(200):
            node = self.rng.choice(big_nodes)
            if node["access_node_id"] in used_nodes:
                continue
            k = self.rng.randint(3, 4)
            t0 = datetime.combine(day, time(8, 45), tzinfo=TZ) + timedelta(minutes=self.rng.randint(0, 6 * 60))
            offsets = sorted(self.rng.sample(range(0, 41), k))
            buildings = self.rng.sample(node["buildings"], k)
            recs: list[Rec] = []
            for off, bld in zip(offsets, buildings):
                wt = WT_BY_CODE["REPAIR"] if self.rng.random() < 0.7 else WT_BY_CODE["DEGRADATION"]
                rec = self.make_record(wt, bld, node, day, registered=(t0 + timedelta(minutes=off)).replace(second=0))
                if rec is None:
                    break
                recs.append(rec)
            if len(recs) != k:
                continue
            used_nodes.add(node["access_node_id"])
            incident = f"INC-{day:%Y%m%d}-{cluster_no:02d}"
            for rec in recs:
                rec.cluster_id = incident
                self.add(rec)
            return recs
        raise RuntimeError("не удалось построить ПГП-кластер")

    def make_background_day(self, day: date, cluster_count: int) -> None:
        """Фоновые наряды дня до целевого количества (ПГП-кластеры уже учтены в cluster_count)."""
        target = self.rng.randint(*ORDERS_PER_DAY)
        count = cluster_count
        codes = [wt["code"] for wt in WORK_TYPES]
        weights = [wt["weight"] for wt in WORK_TYPES]
        attempts = 0
        while count < target and attempts < 40000:
            attempts += 1
            code = self.rng.choices(codes, weights)[0]
            wt = WT_BY_CODE[code]
            repeat_src: Rec | None = None
            if code == "REPAIR" and self.rng.random() < P_REPEAT_REPAIR:
                pool = [r for r in self.repair_history if 1 <= (day - r.day).days <= 7]
                if pool:
                    repeat_src = self.rng.choice(pool)
            if repeat_src is not None:
                building, node = repeat_src.building, repeat_src.node
            else:
                building, node = self.rng.choice(self.pairs_by_type[code])
            rec = self.make_record(wt, building, node, day, repeat_of=repeat_src)
            if rec is None:
                continue
            if wt["pgp_relevant"] and not self.pgp_ok(node["access_node_id"], rec.registered):
                # Наряд отбрасывается; его порядковый номер в дне просто пропускается.
                continue
            self.add(rec)
            count += 1


def make_fact(rng: random.Random, rec: Rec, staff: list[dict]) -> dict:
    if rec.cluster_id:
        return {"outcome": "PGP_GROUPED", "staff_id": None, "actual_start": None,
                "actual_duration_min": None, "pgp_incident_id": rec.cluster_id}
    r = rng.random()
    wt = rec.wt
    if r < 0.84:
        outcome = "COMPLETED"
    elif r < 0.88 and wt["code"] in ("REPAIR", "DEGRADATION"):
        outcome = "TRANSFERRED_TO_STATION"
    elif r < 0.94:
        outcome = "CANCELLED"
    else:
        outcome = "RESCHEDULED"
    if outcome in ("COMPLETED", "TRANSFERRED_TO_STATION"):
        tech = rec.node["technology"]
        eligible = [
            s for s in staff
            if set(wt["profiles"]) & set(s["profiles"])
            and tech in s["technologies"]
            and (not (rec.building["building_type"] == "PRIVATE" and rec.building["overhead_line"]) or s["height_permit"])
        ]
        chosen = rng.choice(eligible) if eligible else None
        actual_start = rec.slot_start + timedelta(minutes=rng.randint(-5, 60))
        if outcome == "TRANSFERRED_TO_STATION":
            duration = rng.randint(10, 30)
        else:
            duration = max(5, round(wt["duration_min"] * rng.lognormvariate(0, 0.2)))
        return {"outcome": outcome, "staff_id": chosen["staff_id"] if chosen else None,
                "actual_start": iso(actual_start), "actual_duration_min": duration, "pgp_incident_id": None}
    return {"outcome": outcome, "staff_id": None, "actual_start": None,
            "actual_duration_min": None, "pgp_incident_id": None}


def gen_orders(rng: random.Random, staff: list[dict], access_nodes: list[dict]) -> list[Rec]:
    pairs_all: list[tuple[dict, dict]] = []
    for node in access_nodes:
        for b in node["buildings"]:
            pairs_all.append((b, node))
    pairs_by_type = {
        "INST_PON_MKD": [(b, n) for b, n in pairs_all if b["building_type"] == "MKD" and n["technology"] == "PON"],
        "INST_FTTB_MKD": [(b, n) for b, n in pairs_all if b["building_type"] == "MKD" and n["technology"] == "FTTB"],
        "INST_PRIVATE_VLS": [(b, n) for b, n in pairs_all if b["building_type"] == "PRIVATE" and b["overhead_line"]],
        "SURVEY_PRIVATE": [(b, n) for b, n in pairs_all if b["building_type"] == "PRIVATE"],
    }
    for code in WT_BY_CODE:
        pairs_by_type.setdefault(code, pairs_all)
    for code, pairs in pairs_by_type.items():
        assert pairs, f"нет домов для типа {code}"

    gen = Gen(rng=rng, staff=staff, pairs_by_type=pairs_by_type)
    big_nodes = [n for n in access_nodes if len(n["buildings"]) >= 3]
    used_nodes: set[str] = set()
    clusters_by_day: dict[int, list[Rec]] = {}
    # ПГП-кластеры создаём до фонового потока, чтобы фон их учитывал.
    for day_idx, count in PGP_CLUSTERS.items():
        day = CANON_DATES[day_idx]
        clusters_by_day[day_idx] = []
        for no in range(1, count + 1):
            clusters_by_day[day_idx].extend(gen.make_cluster(day, no, used_nodes, big_nodes))

    for day_idx, day in enumerate(CANON_DATES):
        gen.make_background_day(day, len(clusters_by_day.get(day_idx, [])))
    return gen.records


# ---------------------------------------------------------------- events

def build_events(rng: random.Random, day_idx: int, recs: list[Rec]) -> dict:
    day = CANON_DATES[day_idx]
    now = datetime.combine(day, time(8, 30), tzinfo=TZ)
    day_recs = [r for r in recs if r.day == day]
    known = [r for r in day_recs if r.registered <= now]
    late = sorted((r for r in day_recs if r.registered > now), key=lambda r: r.registered)[:18]
    cancel_pool = [r for r in known if not r.cluster_id and r.slot_start - timedelta(minutes=45) > now + timedelta(minutes=15)]
    cancels = rng.sample(cancel_pool, 3)
    delay_pool = [r for r in known if not r.cluster_id and time(9) <= r.slot_start.time() <= time(17)]
    delayed = rng.sample(delay_pool, 4)
    transferred = rng.sample([r for r in delay_pool if r not in delayed], 2)

    raw: list[tuple[datetime, int, dict]] = []  # (timestamp, порядок, событие)
    for r in late:
        order = r.to_order()
        raw.append((r.registered, 0, {"type": "NEW_ORDER", "timestamp": iso(r.registered), "order": order}))
    for r in cancels:
        ts = r.slot_start - timedelta(minutes=45)
        raw.append((ts, 1, {"type": "ORDER_CANCELLED", "timestamp": iso(ts), "order_id": r.order_id,
                            "reason": rng.choice(["NOT_CONFIRMED", "CUSTOMER_REQUEST"])}))
    unav = [
        (datetime.combine(day, time(11, 10), tzinfo=TZ), {"staff_id": "ST-014", "until": None, "reason": "VEHICLE_BREAKDOWN"}),
        (datetime.combine(day, time(13, 20), tzinfo=TZ), {"staff_id": "ST-022", "until": iso(datetime.combine(day, time(15, 0), tzinfo=TZ)), "reason": "ILLNESS"}),
    ]
    for ts, body in unav:
        raw.append((ts, 2, {"type": "STAFF_UNAVAILABLE", "timestamp": iso(ts), **body}))
    for hour in range(10, 17):
        ts = datetime.combine(day, time(hour), tzinfo=TZ)
        raw.append((ts, 3, {"type": "TIME_TICK", "timestamp": iso(ts)}))
    raw.sort(key=lambda item: (item[0], item[1]))
    events = []
    for i, (_, _, body) in enumerate(raw, start=1):
        events.append({"event_id": f"EV-{day:%Y%m%d}-{i:04d}", **body})

    triggers = []
    for i, r in enumerate(delayed, start=1):
        triggers.append({
            "trigger_id": f"TR-{day:%Y%m%d}-{i:02d}", "type": "VISIT_DELAYED", "order_id": r.order_id,
            "fire_after_start_min": rng.randint(10, 30), "extra_duration_min": rng.randint(15, 45),
            "transferred_to": None,
        })
    for j, r in enumerate(transferred, start=len(delayed) + 1):
        triggers.append({
            "trigger_id": f"TR-{day:%Y%m%d}-{j:02d}", "type": "ORDER_TRANSFERRED", "order_id": r.order_id,
            "fire_after_start_min": rng.randint(15, 40), "extra_duration_min": None,
            "transferred_to": rng.choice(["STATION_EQUIPMENT", "TRUNK_CABLE"]),
        })
    return {"date": day.isoformat(), "now": iso(now), "events": events, "triggers": triggers}


# ---------------------------------------------------------------- checks

def check_orders(recs: list[Rec], buildings: dict[str, dict], nodes: dict[str, dict]) -> dict:
    """Проверяет инварианты генератора и возвращает статистику."""
    stats = {"orders": len(recs), "repeats": 0, "clusters": 0}
    for r in recs:
        assert r.slot_end - r.slot_start == timedelta(hours=2)
        assert r.registered <= r.slot_start
        assert r.slot_start <= r.latest <= r.slot_end
        assert r.latest + timedelta(minutes=r.wt["duration_min"]) <= r.deadline, r.order_id
        assert r.building["building_id"] in {b["building_id"] for b in r.node["buildings"]}, r.order_id
        assert r.slot_start.date() == r.day
        stats["repeats"] += int(r.is_repeat)
    incidents = {r.cluster_id for r in recs if r.cluster_id}
    stats["clusters"] = len(incidents)
    # Фоновые совпадения: ни в одном окне 60 мин не должно быть ≥3 ПГП-нарядов с одного узла,
    # кроме нарядов из кластеров.
    by_node: dict[str, list[Rec]] = {}
    for r in recs:
        if r.wt["pgp_relevant"]:
            by_node.setdefault(r.node["access_node_id"], []).append(r)
    worst_background = 0
    for node_id, items in by_node.items():
        items.sort(key=lambda x: x.registered)
        for i, a in enumerate(items):
            window = [b for b in items[i:] if (b.registered - a.registered) <= timedelta(minutes=60)]
            background = [b for b in window if b.cluster_id is None]
            worst_background = max(worst_background, len(background))
    stats["max_background_pgp_in_60min"] = worst_background
    return stats


# ---------------------------------------------------------------- validation

def validate_outputs() -> None:
    """Перечитывает все записанные файлы и валидирует их моделями backend."""
    sys.path.insert(0, str(BACKEND_DIR))
    from pydantic import TypeAdapter  # noqa: WPS433 - импорт после правки sys.path

    from app.models import (  # noqa: WPS433
        AccessNode,
        EventScenarioFile,
        Order,
        ServiceCenter,
        Staff,
        TravelMatrix,
        WorkType,
    )

    def load(name: str) -> bytes:
        return (OUT_DIR / name).read_bytes()

    TypeAdapter(list[ServiceCenter]).validate_json(load("service_centers.json"))
    TypeAdapter(list[Staff]).validate_json(load("staff.json"))
    TypeAdapter(list[AccessNode]).validate_json(load("access_nodes.json"))
    rows = []
    with open(OUT_DIR / "work_types.csv", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            rows.append({
                "code": row["code"],
                "name": row["name"],
                "profiles": row["profiles"].split("|") if row["profiles"] != "null" else None,
                "duration_min": int(row["duration_min"]),
                "max_start_delay_min": None if row["max_start_delay_min"] == "null" else int(row["max_start_delay_min"]),
                "deadline_kind": row["deadline_kind"],
                "deadline_value": None if row["deadline_value"] == "null" else int(row["deadline_value"]),
                "repeat_deadline_value": None if row["repeat_deadline_value"] == "null" else int(row["repeat_deadline_value"]),
                "pgp_relevant": row["pgp_relevant"] == "true",
            })
    TypeAdapter(list[WorkType]).validate_python(rows)
    TravelMatrix.model_validate_json(load("travel_matrix.json"))
    TypeAdapter(list[Order]).validate_json(load("orders_train.json"))
    EventScenarioFile.model_validate_json(load("events_example.json"))


# ---------------------------------------------------------------- перенос дат

_ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID_DATE = re.compile(r"(ORD|INC|EV|TR)-(\d{8})")


def _day_map() -> dict[str, date]:
    return dict(zip((d.isoformat() for d in CANON_DATES), TRAIN_DATES))


def _shift_str(v: str, delta: timedelta) -> str:
    if _ISO_TS.match(v):
        return iso(datetime.fromisoformat(v) + delta)
    if _ISO_DATE.match(v):
        return (date.fromisoformat(v) + delta).isoformat()
    ids = {c.strftime("%Y%m%d"): t.strftime("%Y%m%d") for c, t in zip(CANON_DATES, TRAIN_DATES)}
    return _ID_DATE.sub(lambda m: f"{m.group(1)}-{ids.get(m.group(2), m.group(2))}", v)


def _shift_tree(v, delta: timedelta):
    if isinstance(v, str):
        return _shift_str(v, delta)
    if isinstance(v, list):
        return [_shift_tree(x, delta) for x in v]
    if isinstance(v, dict):
        return {k: _shift_tree(x, delta) for k, x in v.items()}
    return v


def _delta_for(day_iso: str) -> timedelta:
    return timedelta(days=(_day_map()[day_iso] - date.fromisoformat(day_iso)).days)


def retarget_order(order: dict) -> dict:
    """Переносит наряд с эталонного дня на реальный: все даты на одинаковое число суток,
    идентификаторы с датой переименовываются, дедлайн пересчитывается по реальному календарю."""
    delta = _delta_for(order["slot"]["start"][:10])
    res = _shift_tree(order, delta)
    wt = WT_BY_CODE[res["work_type_code"]]
    registered = datetime.fromisoformat(res["registered_at"])
    latest = datetime.fromisoformat(res["latest_start"])
    deadline = compute_deadline(wt, registered, latest, res["is_repeat"])
    res["deadline"] = iso(max(deadline, latest + timedelta(minutes=wt["duration_min"])))
    return res


def retarget_events(payload: dict) -> dict:
    delta = _delta_for(payload["date"])
    out = {}
    for key, val in payload.items():
        if key == "events":
            out[key] = [
                {**_shift_tree({k: x for k, x in e.items() if k != "order"}, delta),
                 **({"order": retarget_order(e["order"])} if "order" in e else {})}
                for e in val
            ]
        else:
            out[key] = _shift_tree(val, delta)
    return out


# ---------------------------------------------------------------- main

def serialize_json(payload, *, compact: bool = False) -> bytes:
    if compact:
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return text.encode("utf-8")


def write_json(name: str, payload, *, compact: bool = False) -> None:
    (OUT_DIR / name).write_bytes(serialize_json(payload, compact=compact))


def write_work_types() -> None:
    path = OUT_DIR / "work_types.csv"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)  # RFC 4180: разделитель запятая, CRLF
        writer.writerow(WT_CSV_FIELDS)
        for wt in WORK_TYPES:
            writer.writerow([
                wt["code"],
                wt["name"],
                "|".join(wt["profiles"]),
                wt["duration_min"],
                "null" if wt["max_start_delay_min"] is None else wt["max_start_delay_min"],
                wt["deadline_kind"],
                "null" if wt["deadline_value"] is None else wt["deadline_value"],
                "null" if wt["repeat_deadline_value"] is None else wt["repeat_deadline_value"],
                "true" if wt["pgp_relevant"] else "false",
            ])


def main(dates_only: bool = False) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    staff = gen_staff(rng)
    access_nodes, buildings = gen_network(rng)
    matrix = gen_matrix(rng, buildings)
    recs = gen_orders(rng, staff, access_nodes)
    nodes_index = {n["access_node_id"]: n for n in access_nodes}
    stats = check_orders(recs, buildings, nodes_index)

    orders_out = []
    for r in sorted(recs, key=lambda x: (x.slot_start, x.order_id)):
        order = r.to_order()
        order["fact"] = make_fact(rng, r, staff)
        orders_out.append(retarget_order(order))
    events = retarget_events(build_events(rng, EXAMPLE_IDX, recs))

    sc_out = [
        {
            "service_center_id": sc["service_center_id"],
            "name": sc["name"],
            "address": sc["address"],
            "lat": sc["lat"],
            "lon": sc["lon"],
            "opening_hours": {"open": sc["open"], "close": sc["close"]},
            "restock_duration_min": sc["restock"],
        }
        for sc in SERVICE_CENTERS
    ]

    if dates_only:
        # пространственная часть не пишется; убеждаемся, что она совпала бы с файлами байт-в-байт
        for name, payload, compact in (("service_centers.json", sc_out, False), ("staff.json", staff, False),
                                       ("access_nodes.json", access_nodes, False),
                                       ("travel_matrix.json", matrix, True)):
            if serialize_json(payload, compact=compact) != (OUT_DIR / name).read_bytes():
                raise SystemExit(f"--dates-only: {name} отличается от записанного файла; справочник не тронут")
        print("Справочники и матрица совпадают с файлами байт-в-байт (не перезаписаны)")
    else:
        write_json("service_centers.json", sc_out)
        write_json("staff.json", staff)
        write_json("access_nodes.json", access_nodes)
        write_work_types()
        write_json("travel_matrix.json", matrix, compact=True)
    write_json("orders_train.json", orders_out, compact=True)
    write_json("events_example.json", events)

    validate_outputs()

    per_day = {}
    for o in orders_out:
        per_day[o["slot"]["start"][:10]] = per_day.get(o["slot"]["start"][:10], 0) + 1
    print(f"Файлы записаны в {OUT_DIR}")
    print(f"  сотрудников: {len(staff)}, СЦ: {len(sc_out)}, узлов: {len(access_nodes)}, домов (уникальных): {len(buildings)}")
    print(f"  матрица: {len(matrix['points'])} x {len(matrix['points'])}")
    print(f"  нарядов: {stats['orders']}, повторных: {stats['repeats']} "
          f"({stats['repeats'] / stats['orders']:.1%}), ПГП-кластеров: {stats['clusters']}, "
          f"макс. фоновых ПГП-нарядов с узла за 60 мин: {stats['max_background_pgp_in_60min']}")
    print(f"  нарядов по дням: {min(per_day.values())}..{max(per_day.values())} "
          f"({len(per_day)} дней)")
    print(f"  событий в сценарии: {len(events['events'])}, триггеров: {len(events['triggers'])}")
    print("Валидация моделями backend: OK")


# ---------------------------------------------------------------- плотный сценарий (этап B)

# День с двумя эталонными ПГП-инцидентами = базовый день (TRAIN_DATES[BASE_IDX]).
DENSE_FILE = "events_dense.json"


def build_dense_events() -> dict:
    """Плотный сценарий для бенча перепланирования: все поздние наряды дня (включая
    ПГП-всплески), больше отмен, задержек и передач, три выбытия сотрудников.

    Читает уже сгенерированные orders_train.json и staff.json и пишет только
    events_dense.json: существующие файлы не меняются.
    """
    rng = random.Random(SEED + 2)
    day = TRAIN_DATES[BASE_IDX]
    now = datetime.combine(day, time(8, 30), tzinfo=TZ)
    orders = json.loads((OUT_DIR / "orders_train.json").read_text(encoding="utf-8"))
    staff = json.loads((OUT_DIR / "staff.json").read_text(encoding="utf-8"))
    day_orders = [o for o in orders if o["slot"]["start"][:10] == day.isoformat()]

    def reg(o: dict) -> datetime:
        return datetime.fromisoformat(o["registered_at"])

    def slot(o: dict) -> datetime:
        return datetime.fromisoformat(o["slot"]["start"])

    def clean(o: dict) -> dict:
        return {k: v for k, v in o.items() if k != "fact"}

    def in_cluster(o: dict) -> bool:
        return bool((o.get("fact") or {}).get("pgp_incident_id"))

    known = [o for o in day_orders if reg(o) <= now]
    late = sorted((o for o in day_orders if reg(o) > now), key=lambda o: (reg(o), o["order_id"]))
    raw: list[tuple[datetime, int, dict]] = []
    for o in late:
        raw.append((reg(o), 0, {"type": "NEW_ORDER", "timestamp": o["registered_at"], "order": clean(o)}))

    cancel_pool = [o for o in known if not in_cluster(o) and slot(o) - timedelta(minutes=45) > now + timedelta(minutes=15)]
    for o in rng.sample(cancel_pool, 6):
        ts = slot(o) - timedelta(minutes=rng.choice([45, 60, 90]))
        raw.append((ts, 1, {"type": "ORDER_CANCELLED", "timestamp": iso(ts), "order_id": o["order_id"],
                            "reason": rng.choice(["NOT_CONFIRMED", "CUSTOMER_REQUEST"])}))
    # отмена одного наряда из ПГП-всплеска через 20 минут после его регистрации
    cluster_late = [o for o in late if in_cluster(o)]
    if cluster_late:
        o = cluster_late[-1]
        ts = reg(o) + timedelta(minutes=20)
        raw.append((ts, 1, {"type": "ORDER_CANCELLED", "timestamp": iso(ts), "order_id": o["order_id"],
                            "reason": "CUSTOMER_REQUEST"}))
    # новый наряд, отменённый до визита
    plain_late = [o for o in late if not in_cluster(o) and slot(o) - reg(o) > timedelta(hours=3)]
    if plain_late:
        o = rng.choice(plain_late)
        ts = reg(o) + timedelta(minutes=40)
        raw.append((ts, 1, {"type": "ORDER_CANCELLED", "timestamp": iso(ts), "order_id": o["order_id"],
                            "reason": "NOT_CONFIRMED"}))

    ltp_day = [s["staff_id"] for s in staff if s["profiles"] == ["LTP3"] and s["shift"]["start"] == "09:00"]
    ltp_late = [s["staff_id"] for s in staff if s["profiles"] == ["LTP3"] and s["shift"]["start"] == "12:00"]
    inst = [s["staff_id"] for s in staff if s["profiles"] == ["INSTALL"]]
    picks = [rng.choice(ltp_day), rng.choice(ltp_late), rng.choice(inst)]
    unav = [
        (datetime.combine(day, time(10, 40), tzinfo=TZ), {"staff_id": picks[0], "until": None, "reason": "VEHICLE_BREAKDOWN"}),
        (datetime.combine(day, time(11, 20), tzinfo=TZ),
         {"staff_id": picks[1], "until": iso(datetime.combine(day, time(14, 30), tzinfo=TZ)), "reason": "OTHER"}),
        (datetime.combine(day, time(14, 5), tzinfo=TZ), {"staff_id": picks[2], "until": None, "reason": "ILLNESS"}),
    ]
    for ts, body in unav:
        raw.append((ts, 2, {"type": "STAFF_UNAVAILABLE", "timestamp": iso(ts), **body}))
    tick = datetime.combine(day, time(9, 0), tzinfo=TZ)
    while tick <= datetime.combine(day, time(18, 0), tzinfo=TZ):
        raw.append((tick, 3, {"type": "TIME_TICK", "timestamp": iso(tick)}))
        tick += timedelta(minutes=30)
    raw.sort(key=lambda item: (item[0], item[1]))
    events = [{"event_id": f"EV-{day:%Y%m%d}-{i:04d}", **body} for i, (_, _, body) in enumerate(raw, start=1)]

    delay_pool = [o for o in known if not in_cluster(o) and time(9) <= slot(o).time() <= time(17)]
    delayed = rng.sample(delay_pool, 10)
    transferred = rng.sample([o for o in delay_pool if o not in delayed and o["work_type_code"] in ("REPAIR", "DEGRADATION")], 4)
    triggers = []
    for i, o in enumerate(delayed, start=1):
        triggers.append({
            "trigger_id": f"TR-{day:%Y%m%d}-{i:02d}", "type": "VISIT_DELAYED", "order_id": o["order_id"],
            "fire_after_start_min": rng.randint(10, 30), "extra_duration_min": rng.randint(20, 60),
            "transferred_to": None,
        })
    for j, o in enumerate(transferred, start=len(delayed) + 1):
        triggers.append({
            "trigger_id": f"TR-{day:%Y%m%d}-{j:02d}", "type": "ORDER_TRANSFERRED", "order_id": o["order_id"],
            "fire_after_start_min": rng.randint(15, 40), "extra_duration_min": None,
            "transferred_to": rng.choice(["STATION_EQUIPMENT", "TRUNK_CABLE"]),
        })
    return {"date": day.isoformat(), "now": iso(now), "events": events, "triggers": triggers}


def main_dense() -> None:
    payload = build_dense_events()
    write_json(DENSE_FILE, payload)
    sys.path.insert(0, str(BACKEND_DIR))
    from app.models import EventScenarioFile  # noqa: WPS433

    EventScenarioFile.model_validate_json((OUT_DIR / DENSE_FILE).read_bytes())
    kinds: dict[str, int] = {}
    for e in payload["events"]:
        kinds[e["type"]] = kinds.get(e["type"], 0) + 1
    print(f"Записан {OUT_DIR / DENSE_FILE}: событий {len(payload['events'])} {kinds}, триггеров {len(payload['triggers'])}")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-date", help="базовый (демо) рабочий день YYYY-MM-DD; по умолчанию сегодня/ближайший рабочий")
    ap.add_argument("--dates-only", action="store_true", help="писать только файлы с датами, сверив справочники")
    ap.add_argument("--dense", action="store_true", help="только events_dense.json")
    args = ap.parse_args()
    base = date.fromisoformat(args.base_date) if args.base_date else default_base_date()
    configure_dates(base)
    print(f"Базовый день: {base} ({base:%a}); история: {TRAIN_DATES[0]}..{TRAIN_DATES[-1]}")
    if args.dense:
        main_dense()
    else:
        main(dates_only=args.dates_only)
        main_dense()
