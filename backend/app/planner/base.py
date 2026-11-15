"""Интерфейс планировщика.

Реальный планировщик подключается через реализацию Protocol `Planner`.
Сервис сам проставляет `plan_version`, `as_of` и `based_on_event_id`;
планировщик отвечает за содержимое маршрутов, нераспределённых, отменённых,
ПГП-групп и предупреждений.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..models import Alert, Event, Plan
from ..state import DayState, MatrixState, ReferenceState


@dataclass
class PlanUpdate:
    plan: Plan
    changed_staff_ids: list[str] = field(default_factory=list)
    new_alerts: list[Alert] = field(default_factory=list)
    message: str | None = None  # EventResult.message (справочно)
    stats: dict = field(default_factory=dict)  # диагностика планировщика, в API не выходит


class Planner(Protocol):
    def build_plan(
        self,
        ref: ReferenceState,
        matrix: MatrixState,
        day: DayState,
        time_limit_sec: int | None = None,
    ) -> Plan:
        """Строит первоначальный план на `day.now`. `day` не меняется.

        `time_limit_sec` — лимит из PlanRequest; планировщик укладывается в него с запасом.
        """
        ...

    def apply_event(
        self,
        ref: ReferenceState,
        matrix: MatrixState,
        day: DayState,
        event: Event,
    ) -> PlanUpdate:
        """Перепланирует по событию.

        `day` — клон состояния: планировщик может его менять (наряды, отмены,
        время появления). Если метод поднимает ApiError, состояние не сохраняется.
        """
        ...
