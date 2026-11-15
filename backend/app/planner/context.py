"""Контекст дня для планировщика: целочисленное время, компактная матрица, допуски.

Время — целые секунды от 00:00 местного дня (UTC+5, Екатеринбург), как в
docs/formalization.md. Матрица сжимается до точек дня (СЦ + дома нарядов) и
хранится списками Python: декодер обращается к ней миллионы раз.
"""

from __future__ import annotations

import datetime as dt
import math
import os
from dataclasses import dataclass, field

import numpy as np

from ..models import Order, ServiceCenter, Staff, WorkType
from ..state import DayState, MatrixState, ReferenceState

TZ = dt.timezone(dt.timedelta(hours=5))
EQUIPMENT_KINDS = ("ONT", "ROUTER", "STB")


def hhmm_to_sec(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 3600 + int(minutes) * 60


def eq_tuple(eq) -> tuple[int, int, int]:
    return (eq.ONT, eq.ROUTER, eq.STB)


@dataclass
class ScCtx:
    sc_id: str
    point: int  # индекс в компактной матрице
    open: int
    close: int
    rho: int  # restock_duration, с
    model: ServiceCenter


@dataclass
class StaffCtx:
    staff_id: str
    idx: int
    sc: ScCtx
    start: int  # S_k
    end: int  # F_k
    beta: int  # длительность перерыва, с
    bf: int  # окно начала перерыва
    bt: int
    q0: tuple[int, int, int]
    cap: tuple[int, int, int]
    model: Staff

    @property
    def net_shift(self) -> int:
        return max(1, self.end - self.start - self.beta)


@dataclass
class OrderCtx:
    order_id: str
    idx: int
    point: int
    a: int  # slot.start
    b: int  # latest_start
    p: int  # норматив, с
    deadline: int
    reg: int  # registered_at
    dem: tuple[int, int, int]
    weight: int  # 2 для повторных
    work_type: WorkType
    model: Order
    eligible: list[int] = field(default_factory=list)  # индексы StaffCtx (H1)


def qualifies(staff: Staff, order: Order, wt: WorkType) -> bool:
    """H1: профиль, технология, допуск к высоте."""
    if not set(staff.profiles) & set(wt.profiles):
        return False
    if order.technology not in staff.technologies:
        return False
    if order.requires_height and not staff.height_permit:
        return False
    return True


def traffic_enabled() -> bool:
    """Переключатель TRAFFIC_FACTORS=on|off (по умолчанию off): учёт загруженности дорог.

    Читается при создании контекста (не при импорте), поэтому тесты и сценарии могут
    передать режим явно через DayContext(traffic=...)."""
    from .. import runtime_settings

    return runtime_settings.get_bool("TRAFFIC_FACTORS")


def hour_factors(matrix: MatrixState) -> list[float]:
    """24 коэффициента по часу отправления, не меньше 1 (часы без записи — 1)."""
    out = [1.0] * 24
    for hf in matrix.matrix.hourly_factors:
        if 0 <= hf.hour <= 23:
            out[hf.hour] = max(1.0, float(hf.factor))
    return out


def matrix_arrays(matrix: MatrixState) -> tuple[np.ndarray, np.ndarray]:
    """numpy-копии матриц (кэшируются на объекте MatrixState)."""
    cached = getattr(matrix, "_np_cache", None)
    if cached is not None and cached[0] is matrix.matrix:
        return cached[1], cached[2]
    dur = np.asarray(matrix.matrix.durations_sec, dtype=np.int64)
    dist = np.asarray(matrix.matrix.distances_m, dtype=np.int64)
    matrix._np_cache = (matrix.matrix, dur, dist)  # type: ignore[attr-defined]
    return dur, dist


class DayContext:
    """Все данные дня в форме, удобной для декодера и солвера."""

    def __init__(
        self,
        ref: ReferenceState,
        matrix: MatrixState,
        day: DayState,
        orders: list[Order],
        now: dt.datetime,
        traffic: bool | None = None,
        joint: bool | None = None,
    ) -> None:
        self.ref = ref
        # Совместные наряды (JOINT_ORDERS, planner/joint.py): при выключенном режиме код идёт
        # по прежней ветке. joint_fail — основные наряды без пары (причина для unassigned),
        # joint_staff — main_id -> (основной сотрудник, напарник) для пояснений.
        if joint is None:
            from .joint import joint_enabled

            joint = joint_enabled()
        self.joint = bool(joint)
        self.joint_fail: dict[str, str] = {}
        self.joint_staff: dict[str, tuple[str, str]] = {}
        # Пробки по часам: travel = ceil(τ · max(1, f[час отправления])) ≥ τ, поэтому H6 по
        # базовой матрице выполняется всегда. Без коэффициентов в матрице — no-op.
        self.hour_factor = hour_factors(matrix)
        if traffic is None:
            traffic = traffic_enabled()
        self.traffic = bool(traffic) and any(f > 1.0 for f in self.hour_factor)
        self.day = day
        self.date = day.date
        self.midnight = dt.datetime.combine(day.date, dt.time(0), tzinfo=TZ)
        self.now = self.to_sec(now)

        # ---- точки дня: сначала все СЦ, затем дома нарядов
        point_ids: list[str] = list(ref.service_centers)
        point_of: dict[str, int] = {p: i for i, p in enumerate(point_ids)}
        for o in orders:
            if o.building_id not in point_of:
                point_of[o.building_id] = len(point_ids)
                point_ids.append(o.building_id)
        self.point_ids = point_ids
        self.point_of = point_of
        full_dur, full_dist = matrix_arrays(matrix)
        sel = np.array([matrix.index[p] for p in point_ids], dtype=np.int64)
        self.dur_np = full_dur[np.ix_(sel, sel)]
        self.dist_np = full_dist[np.ix_(sel, sel)]
        self.tau: list[list[int]] = self.dur_np.tolist()
        self.dist: list[list[int]] = self.dist_np.tolist()
        self.tau_max = int(full_dur.max()) if full_dur.size else 0

        # ---- СЦ
        self.scs: list[ScCtx] = []
        self.sc_by_id: dict[str, ScCtx] = {}
        for sc in ref.service_centers.values():
            ctx = ScCtx(
                sc_id=sc.service_center_id,
                point=point_of[sc.service_center_id],
                open=hhmm_to_sec(sc.opening_hours.open),
                close=hhmm_to_sec(sc.opening_hours.close),
                rho=sc.restock_duration_min * 60,
                model=sc,
            )
            self.scs.append(ctx)
            self.sc_by_id[ctx.sc_id] = ctx

        # ---- сотрудники на смене
        self.staff: list[StaffCtx] = []
        for s in ref.staff.values():
            if s.staff_id in day.absent_staff_ids:
                continue
            self.staff.append(
                StaffCtx(
                    staff_id=s.staff_id,
                    idx=len(self.staff),
                    sc=self.sc_by_id[s.service_center_id],
                    start=hhmm_to_sec(s.shift.start),
                    end=hhmm_to_sec(s.shift.end),
                    beta=s.shift.break_duration_min * 60,
                    bf=hhmm_to_sec(s.shift.break_window.from_),
                    bt=hhmm_to_sec(s.shift.break_window.to),
                    q0=eq_tuple(s.equipment_start),
                    cap=eq_tuple(s.equipment_capacity),
                    model=s,
                )
            )
        self.staff_by_id = {s.staff_id: s for s in self.staff}

        # ---- наряды
        self.orders: list[OrderCtx] = []
        for o in orders:
            wt = ref.work_types[o.work_type_code]
            octx = OrderCtx(
                order_id=o.order_id,
                idx=len(self.orders),
                point=point_of[o.building_id],
                a=self.to_sec(o.slot.start),
                b=self.to_sec(o.latest_start),
                p=wt.duration_min * 60,
                deadline=self.to_sec(o.deadline),
                reg=self.to_sec(o.registered_at),
                dem=eq_tuple(o.required_equipment),
                weight=2 if o.is_repeat else 1,
                work_type=wt,
                model=o,
            )
            octx.eligible = [k.idx for k in self.staff if qualifies(k.model, o, wt)]
            self.orders.append(octx)
        self.order_by_id = {o.order_id: o for o in self.orders}

    # ------------------------------------------------------------ travel
    def tt(self, i: int, j: int, t: int) -> int:
        """Время в пути i→j при отправлении в момент t (с от полуночи, местное время)."""
        base = self.tau[i][j]
        if not self.traffic or base == 0:
            return base
        f = self.hour_factor[(int(t) // 3600) % 24]
        if f <= 1.0:
            return base
        return max(base, math.ceil(base * f - 1e-9))

    # ------------------------------------------------------------ time
    def to_sec(self, moment: dt.datetime) -> int:
        return int(round((moment - self.midnight).total_seconds()))

    def to_dt(self, seconds: int) -> dt.datetime:
        return self.midnight + dt.timedelta(seconds=int(seconds))
