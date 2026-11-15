"""Совместные наряды (дополнительное задание ТЗ): инсталляция в частном секторе с ВЛС
(`INST_PRIVATE_VLS`) выполняется двумя сотрудниками одновременно.

Режим `JOINT_ORDERS=on|off`, по умолчанию **off** (docs/planner-notes.md, «Совместные наряды»).

Представление в плане (контракт не меняется): основной исполнитель получает обычный VISIT
по наряду `ORD-…`, напарник — VISIT того же дома, того же начала и длительности по
идентификатору `ORD-…-B` (`partner_id`). Такого наряда нет во входных данных дня, поэтому
валидатор организаторов, скорее всего, посчитает визит напарника визитом по неизвестному
наряду (H9) — отсюда выключенный по умолчанию режим.

Внутри планировщика наряд — один «совместный»: основной визит и визит напарника — это
копии OrderCtx с окном, закреплённым в одну точку `[s, s]` (`pinned`, `partner_ctx`).
Декодер (decoder.py и декодер хвоста events.py) соблюдает окно, поэтому оба визита
начинаются ровно в `s`. АО списывается только у основного исполнителя (у напарника
`dem = (0, 0, 0)`).
"""

from __future__ import annotations

import dataclasses
import os
import time
from typing import Callable

from .context import OrderCtx

JOINT_WORK_TYPE = "INST_PRIVATE_VLS"
PARTNER_SUFFIX = "-B"
GRID_SEC = 5 * 60  # шаг перебора общего начала внутри [slot.start, latest_start]


def joint_enabled() -> bool:
    """Переключатель JOINT_ORDERS=on|off (по умолчанию off). Читается при создании контекста."""
    from .. import runtime_settings

    return runtime_settings.get_bool("JOINT_ORDERS")


def partner_id(main_id: str) -> str:
    return main_id + PARTNER_SUFFIX


def main_of(order_id: str | None, known=None) -> str | None:
    """Идентификатор основного наряда для визита напарника (`ORD-…-B`), иначе None.

    known — множество/словарь известных нарядов: основной наряд должен в нём быть."""
    if not order_id or not order_id.endswith(PARTNER_SUFFIX):
        return None
    main = order_id[: -len(PARTNER_SUFFIX)]
    if known is not None and main not in known:
        return None
    return main


def is_joint(o: OrderCtx) -> bool:
    """Основной наряд, требующий напарника (по типу работ)."""
    return o.work_type.code == JOINT_WORK_TYPE and not o.order_id.endswith(PARTNER_SUFFIX)


def is_joint_code(work_type_code: str) -> bool:
    return work_type_code == JOINT_WORK_TYPE


def pinned(o: OrderCtx, s: int) -> OrderCtx:
    """Копия наряда с окном начала, закреплённым в момент s."""
    return dataclasses.replace(o, a=s, b=s, eligible=list(o.eligible))


def partner_ctx(o: OrderCtx, s: int) -> OrderCtx:
    """Визит напарника: тот же дом и норматив, начало ровно s, без АО и без веса в целевой."""
    return dataclasses.replace(
        o, order_id=partner_id(o.order_id), a=s, b=s, dem=(0, 0, 0), weight=0, eligible=list(o.eligible)
    )


def start_grid(o: OrderCtx, lo: int) -> list[int]:
    """Кандидаты общего начала: [max(a, lo), b] с шагом GRID_SEC, b включительно."""
    a = max(o.a, lo)
    if a > o.b:
        return []
    out = list(range(a, o.b + 1, GRID_SEC))
    if out[-1] != o.b:
        out.append(o.b)
    return out


def search(
    o: OrderCtx,
    starts: list[int],
    staff: list[int],
    try_insert: Callable[[int, OrderCtx], tuple[int, object] | None],
    deadline: float | None = None,
) -> tuple[int, int, object, OrderCtx, int, object, OrderCtx] | None:
    """Лучшая пара (основной k1, напарник k2 ≠ k1) и общее начало s по сумме прироста пути.

    try_insert(k, наряд) -> (прирост, расписание) или None. Возвращает
    (s, k1, расписание k1, закреплённый основной, k2, расписание k2, визит напарника)."""
    best = None
    for s in starts:
        if deadline is not None and time.perf_counter() > deadline and best is not None:
            break
        m, p = pinned(o, s), partner_ctx(o, s)
        mains = []
        for k in staff:
            r = try_insert(k, m)
            if r is not None:
                mains.append((r[0], k, r[1]))
        if not mains:
            continue
        partners = []
        for k in staff:
            r = try_insert(k, p)
            if r is not None:
                partners.append((r[0], k, r[1]))
        if not partners:
            continue
        mains.sort(key=lambda x: x[0])
        partners.sort(key=lambda x: x[0])
        for c1, k1, r1 in mains[:3]:
            for c2, k2, r2 in partners[:3]:
                if k1 == k2:
                    continue
                if best is None or c1 + c2 < best[0]:
                    best = (c1 + c2, (s, k1, r1, m, k2, r2, p))
                break
    return best[1] if best is not None else None


def unassigned_text(o: OrderCtx, n_staff: int) -> tuple[bool, str]:
    """(нет двух допустимых сотрудников, текст причины) для совместного наряда без пары."""
    window = f"{o.a // 3600:02d}:{o.a % 3600 // 60:02d}–{o.b // 3600:02d}:{o.b % 3600 // 60:02d}"
    if n_staff < 2:
        return True, (
            f"Совместный наряд: нужны два сотрудника с допуском к работам на высоте, профилем "
            f"{'/'.join(o.work_type.profiles)} и технологией {o.model.technology}; на смене таких {n_staff}"
        )
    return False, (
        f"Совместный наряд: среди {n_staff} допустимых сотрудников не нашлось двух, у которых одновременно "
        f"свободно {o.p // 60} мин с началом в окне {window} без срыва уже назначенных визитов"
    )
