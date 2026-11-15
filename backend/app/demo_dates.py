"""Даты демо-набора data/synth: единый источник вместо жёстко прошитых дат.

Дни читаются из самих данных (их задаёт `tools/synth/generate.py --base-date ...`):
  * dense_date()   - демо-день «сегодня» (events_dense.json, он же TRAIN_DATES[5]);
  * example_date() - день сценария events_example.json;
  * train_dates()  - все дни orders_train.json по возрастанию (индекс 5 = dense_date()).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _data_dir() -> Path:
    return Path(os.environ.get("UI_DATA_DIR", ROOT / "data" / "synth"))


@lru_cache(maxsize=4)
def _scenario_date(data_dir: str, name: str) -> str:
    return json.loads((Path(data_dir) / name).read_text(encoding="utf-8"))["date"]


@lru_cache(maxsize=4)
def _train_dates(data_dir: str) -> tuple[str, ...]:
    orders = json.loads((Path(data_dir) / "orders_train.json").read_text(encoding="utf-8"))
    return tuple(sorted({o["slot"]["start"][:10] for o in orders}))


def dense_date() -> str:
    return _scenario_date(str(_data_dir()), "events_dense.json")


def example_date() -> str:
    return _scenario_date(str(_data_dir()), "events_example.json")


def train_dates() -> list[str]:
    return list(_train_dates(str(_data_dir())))


def date_compact(date: str) -> str:
    """'2026-11-16' -> '20261116' (часть order_id вида ORD-YYYYMMDD-NNNN)."""
    return date.replace("-", "")
