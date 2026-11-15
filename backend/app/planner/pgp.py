"""Выявление ПГП (подозрение на групповую проблему) — docs/formalization.md, раздел 6.

Кандидаты — наряды с `work_type.pgp_relevant = true`. На узле доступа наряды
упорядочиваются по `registered_at`; наряд входит в инцидент, если попадает в
какое-либо 60-минутное окно, где таких нарядов не меньше 3. Соседние помеченные
наряды узла с разрывом не больше 60 минут образуют одну группу.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..models import Order, WorkType

PGP_MIN_ORDERS = 3
PGP_WINDOW = dt.timedelta(minutes=60)


@dataclass
class PgpCluster:
    access_node_id: str
    orders: list[Order]

    @property
    def span_min(self) -> int:
        regs = [o.registered_at for o in self.orders]
        return int(round((max(regs) - min(regs)).total_seconds() / 60))


def detect_pgp(orders: list[Order], work_types: dict[str, WorkType]) -> list[PgpCluster]:
    by_node: dict[str, list[Order]] = {}
    for o in orders:
        wt = work_types.get(o.work_type_code)
        if wt is not None and wt.pgp_relevant:
            by_node.setdefault(o.access_node_id, []).append(o)

    clusters: list[PgpCluster] = []
    for node_id in sorted(by_node):
        items = sorted(by_node[node_id], key=lambda o: (o.registered_at, o.order_id))
        n = len(items)
        marked = [False] * n
        hi = 0
        for lo in range(n):
            hi = max(hi, lo)
            while hi + 1 < n and items[hi + 1].registered_at - items[lo].registered_at <= PGP_WINDOW:
                hi += 1
            if hi - lo + 1 >= PGP_MIN_ORDERS:
                for j in range(lo, hi + 1):
                    marked[j] = True
        current: list[Order] = []
        for j in range(n):
            if not marked[j]:
                continue
            if current and items[j].registered_at - current[-1].registered_at > PGP_WINDOW:
                clusters.append(PgpCluster(node_id, current))
                current = []
            current.append(items[j])
        if current:
            clusters.append(PgpCluster(node_id, current))
    clusters.sort(key=lambda c: (min(o.registered_at for o in c.orders), c.access_node_id))
    return clusters
