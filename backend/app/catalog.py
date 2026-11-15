"""Служебный загрузчик каталога data/synth (UI_DATA_DIR) — единственное место чтения каталога.

Используется ТОЛЬКО служебными путями: `/ui/demo/load` (и восстановление его проигрывателя),
«Создать день» при отсутствии справочников (подгрузка справочников и матрицы, без нарядов), скрипты
(tools/sim, tools\\seed.bat читают каталог сами) и тесты. Рабочие пути (GET /ui/directory/*,
/ui/orders/catalog, /ui/forecast/*, /ui/employee/*, синхронизация аккаунтов) каталог не читают: они берут
справочники из Store (то, что пришло в PUT /api/v1/reference).

`staff_names.json` — справочная заготовка ФИО для staff_id каталога: применяется только при загрузке
справочников из каталога (новые записи справочника сотрудников получают эти ФИО), не в рабочих эндпоинтах.
"""

from __future__ import annotations

import csv
import json
import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = {"dense": "events_dense.json", "example": "events_example.json"}


def data_dir() -> Path:
    return Path(os.environ.get("UI_DATA_DIR", ROOT / "data" / "synth"))


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


@lru_cache(maxsize=2)
def _reference_bytes(data: str) -> bytes:
    d = Path(data)
    with open(d / "work_types.csv", encoding="utf-8", newline="") as fh:
        work_types = [{k: _csv_value(k, v) for k, v in row.items()} for row in csv.DictReader(fh)]
    payload = {
        "service_centers": json.loads((d / "service_centers.json").read_text(encoding="utf-8")),
        "staff": json.loads((d / "staff.json").read_text(encoding="utf-8")),
        "access_nodes": json.loads((d / "access_nodes.json").read_text(encoding="utf-8")),
        "work_types": work_types,
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def reference_bytes() -> bytes:
    """Тело PUT /api/v1/reference из каталога (как прислал бы внешний клиент). OSError — каталога нет."""
    return _reference_bytes(str(data_dir()))


def matrix_bytes() -> bytes:
    return (data_dir() / "travel_matrix.json").read_bytes()


@lru_cache(maxsize=2)
def _orders_train(data: str) -> list[dict]:
    return json.loads((Path(data) / "orders_train.json").read_text(encoding="utf-8"))


def orders_train() -> list[dict]:
    return _orders_train(str(data_dir()))


@lru_cache(maxsize=4)
def _scenario(data: str, name: str) -> dict:
    return json.loads((Path(data) / SCENARIOS[name]).read_text(encoding="utf-8"))


def scenario(name: str) -> dict:
    return _scenario(str(data_dir()), name)


def staff_names() -> dict[str, dict]:
    """staff_id -> {first_name, last_name, middle_name, phone, position, personnel_number}; нет файла — {}."""
    try:
        raw = json.loads((data_dir() / "staff_names.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
