"""Планировщики: PyVrpPlanner (по умолчанию) и заглушка StubPlanner.

Выбор — переменная окружения PLANNER=pyvrp|stub.
"""

from __future__ import annotations

import os

from .base import Planner, PlanUpdate
from .stub import StubPlanner


def make_planner(name: str | None = None) -> Planner:
    name = (name or os.environ.get("PLANNER", "pyvrp")).strip().lower()
    if name == "stub":
        return StubPlanner()
    if name == "pyvrp":
        from .pyvrp_planner import PyVrpPlanner

        return PyVrpPlanner()
    raise ValueError(f"Неизвестный планировщик PLANNER={name!r}, ожидается pyvrp или stub")


__all__ = ["Planner", "PlanUpdate", "StubPlanner", "make_planner"]
