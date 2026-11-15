"""Планировщик на PyVRP: первоначальный план (этап A).

Конвейер build_plan (docs/formalization.md, разделы 3–7):
1. ПГП-группы (≥3 наряда pgp_relevant с узла за ≤60 мин) выносятся из распределения.
2. Наряды без допустимых сотрудников (H1) -> NO_QUALIFIED_STAFF.
3. PyVRP выбирает назначение и порядок визитов (vrp_model).
4. Декодер строит точное расписание с перерывом и пополнениями; недопустимые
   маршруты чинятся снятием минимального числа нарядов.
5. Жадная вставка снятых и невыбранных нарядов (по весу, затем по редкости допуска).
6. Выравнивание загрузки внутри профилей (перенос, обмен; бюджет + ≤ 5 с), добор нераспределённых.
7. Причины unassigned, предупреждения, сводка; самопроверка validator.py.

События (этап B) обрабатываются инкрементально в events.py.
"""

from __future__ import annotations

import os
import random
import time

from .. import runtime_settings
from ..models import Event, Plan
from ..state import DayState, MatrixState, ReferenceState
from . import assemble
from .base import PlanUpdate
from .context import DayContext, OrderCtx, StaffCtx, traffic_enabled
from .decoder import VISIT, RouteSchedule, best_insertion, decode
from . import joint as jnt
from .pgp import detect_pgp
from .validator import validate_plan
from .vrp_model import Weights, solve_sequences

# Выравнивание загрузки (docs/planner-notes.md, «Баланс загрузки»). Штраф Σ_p Σ_{k∈p}(ρ_k − ρ̄_p)²
# считается по профилям отдельно (между профилями наряды не взаимозаменяемы, H1), ρ̄_p — по всем
# работающим сотрудникам профиля, включая пустые маршруты. W4 в formalization.md — 3600; при нём
# перенос наряда к простаивающему почти никогда не окупает выезд из СЦ, поэтому вес поднят, а
# приоритет пробега держит жёсткий потолок роста суммарной дороги.
W4_BALANCE = 8_000
BALANCE_MAX_TRAVEL_GROWTH = 0.04  # дорога после выравнивания ≤ (1 + 4 %) · дорога до него
BALANCE_TARGETS = 6  # сколько наименее загруженных допустимых сотрудников пробуем на наряд
BALANCE_EXTRA_SHARE = 0.08  # доп. бюджет на выравнивание сверх основного: доля бюджета дня,
BALANCE_EXTRA_MAX_SEC = 5.0  # но не больше 5 с (бюджет дня ≤ 0,8·лимит − 3 с, лимит не превышается)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def solve_budget(time_limit_sec: int | None) -> float:
    """Внутренний бюджет на день: min(PLANNER_DAY_BUDGET_SEC, 80 % лимита − 3 с)."""
    day_budget = float(runtime_settings.get("PLANNER_DAY_BUDGET_SEC"))
    limit = float(time_limit_sec or 300)
    return max(0.3, min(day_budget, limit * 0.8 - 3.0))


class PyVrpPlanner:
    def __init__(self, seed: int = 1, traffic: bool | None = None, joint: bool | None = None) -> None:
        self.seed = seed
        # учёт пробок по часам в слое расписания; None — из TRAFFIC_FACTORS (context.traffic_enabled)
        self.traffic = traffic
        # совместные наряды; None — из JOINT_ORDERS (planner/joint.py)
        self.joint = joint
        self.last_stats: dict = {}
        self._day_modes: dict = {}  # date -> (traffic, joint) на момент последнего build_plan

    # ------------------------------------------------------------ build
    def build_plan(
        self,
        ref: ReferenceState,
        matrix: MatrixState,
        day: DayState,
        time_limit_sec: int | None = None,
    ) -> Plan:
        t0 = time.perf_counter()
        budget = solve_budget(time_limit_sec)
        deadline = t0 + budget - min(1.5, 0.1 * budget)  # запас на сборку и самопроверку
        stats: dict = {"budget_sec": round(budget, 2)}

        pending = day.pending_orders()
        clusters = detect_pgp(pending, ref.work_types)
        pgp_ids = {o.order_id for c in clusters for o in c.orders}
        routable = [o for o in pending if o.order_id not in pgp_ids]

        # Режим фиксируется на день при построении: события этого плана идут в том же режиме,
        # даже если настройки переключили позже (следующая пересборка возьмёт новые).
        traffic = self.traffic if self.traffic is not None else traffic_enabled()
        joint = self.joint if self.joint is not None else jnt.joint_enabled()
        self._day_modes[day.date] = (bool(traffic), bool(joint))
        ctx = DayContext(ref, matrix, day, routable, day.now, traffic=traffic, joint=joint)
        stats["traffic"] = ctx.traffic
        weights = Weights.from_ctx(ctx)
        candidates = [o for o in ctx.orders if o.eligible]

        # ---- PyVRP
        prep = time.perf_counter() - t0
        share = _env_float("PLANNER_PYVRP_SHARE", 0.7)
        pyvrp_time = max(0.1, (budget - prep) * share)
        t1 = time.perf_counter()
        seqs, info = solve_sequences(ctx, candidates, weights, pyvrp_time, seed=self.seed)
        stats["pyvrp_sec"] = round(time.perf_counter() - t1, 2)
        stats["pyvrp_feasible"] = info.get("feasible")
        stats["pyvrp_iterations"] = info.get("iterations")
        stats["pyvrp_visits"] = sum(len(s) for s in seqs.values())

        # ---- декодер + починка
        schedules: dict[int, RouteSchedule] = {}
        dropped: list[OrderCtx] = []
        for k in ctx.staff:
            sched, removed = self._feasible_schedule(ctx, k, seqs.get(k.idx, []))
            schedules[k.idx] = sched
            dropped.extend(removed)
        stats["decoder_dropped"] = len(dropped)

        # ---- вставка
        routed = {o.order_id for s in schedules.values() for o in s.orders}
        pool = [o for o in candidates if o.order_id not in routed]
        inserted, exhausted = self._insert(ctx, schedules, pool, deadline)
        stats["repair_inserted"] = inserted
        if not exhausted:
            gain = self._eject_insert(ctx, schedules, deadline)
            stats["eject_gain"] = gain
            # LNS на точном декодере в остаток бюджета (оставляем запас на выравнивание)
            lns_deadline = deadline - 0.1 * (deadline - time.perf_counter())
            stats["lns"] = self._lns(ctx, schedules, weights, lns_deadline, self.seed)
            stats["eject_gain_2"] = self._eject_insert(ctx, schedules, deadline)

        # ---- совместные наряды: напарник к каждому размещённому INST_PRIVATE_VLS (JOINT_ORDERS=on)
        if ctx.joint:
            stats["joint"] = self._joint_pairs(ctx, schedules, deadline)

        # ---- выравнивание загрузки внутри профилей (+ добор освободившегося времени)
        extra = min(BALANCE_EXTRA_MAX_SEC, BALANCE_EXTRA_SHARE * budget)
        bal = self._balance(ctx, schedules, max(deadline, time.perf_counter()) + extra)
        stats["balance"] = bal
        stats["balance_moves"] = bal["moves"] + bal["swaps"]

        plan = self._assemble(ctx, day, schedules, clusters, exhausted)

        # ---- самопроверка: наряды с нарушениями снимаются (не должно срабатывать)
        violations = validate_plan(plan, ref, matrix, day, joint=ctx.joint)
        stats["violations_fixed"] = len(violations)
        if violations:
            bad = {v.order_id for v in violations if v.order_id}
            if ctx.joint:
                # пара снимается целиком: основной наряд и визит напарника
                bad |= {jnt.partner_id(x) for x in bad} | {jnt.main_of(x) for x in bad if jnt.main_of(x)}
                for x in bad:
                    if x in ctx.order_by_id and jnt.is_joint(ctx.order_by_id[x]):
                        ctx.joint_fail.setdefault(x, "Совместный наряд снят самопроверкой плана")
            bad_staff = {v.staff_id for v in violations if v.staff_id and not v.order_id}
            for kidx, sched in list(schedules.items()):
                k = ctx.staff[kidx]
                keep = [o for o in sched.orders if o.order_id not in bad]
                if k.staff_id in bad_staff:
                    keep = []
                if len(keep) != len(sched.orders):
                    schedules[kidx], _ = self._feasible_schedule(ctx, k, keep)
            plan = self._assemble(ctx, day, schedules, clusters, exhausted)
        stats["utilization"] = assemble.utilization_breakdown(plan, assemble.staff_profiles(ref))
        stats["total_sec"] = round(time.perf_counter() - t0, 2)
        self.last_stats = stats
        return plan

    # ------------------------------------------------------------ steps
    @staticmethod
    def _feasible_schedule(
        ctx: DayContext, k: StaffCtx, seq: list[OrderCtx]
    ) -> tuple[RouteSchedule, list[OrderCtx]]:
        """Расписание для seq; при недопустимости снимаются наряды (минимальный вес первым)."""
        seq = list(seq)
        removed: list[OrderCtx] = []
        while True:
            sched = decode(ctx, k, seq)
            if sched is not None:
                return sched, removed
            # сначала перестановка внутри маршрута (чаще всего мешает перерыв)
            moved: RouteSchedule | None = None
            for i in range(len(seq)):
                rest = seq[:i] + seq[i + 1 :]
                for j in range(len(seq)):
                    if j == i:
                        continue
                    res = decode(ctx, k, rest[:j] + [seq[i]] + rest[j:])
                    if res is not None and (moved is None or res.travel < moved.travel):
                        moved = res
            if moved is not None:
                return moved, removed
            best: tuple[tuple, int, RouteSchedule] | None = None
            for i in range(len(seq)):
                res = decode(ctx, k, seq[:i] + seq[i + 1 :])
                if res is not None:
                    key = (seq[i].weight, res.travel)
                    if best is None or key < best[0]:
                        best = (key, i, res)
            if best is not None:
                removed.append(seq[best[1]])
                return best[2], removed
            # одиночное снятие не помогло: снимаем первый наряд, на котором ломается префикс
            cut = len(seq) - 1
            for n in range(1, len(seq) + 1):
                if decode(ctx, k, seq[:n]) is None:
                    cut = n - 1
                    break
            removed.append(seq.pop(cut))

    @staticmethod
    def _insert(
        ctx: DayContext,
        schedules: dict[int, RouteSchedule],
        pool: list[OrderCtx],
        deadline: float,
    ) -> tuple[int, bool]:
        """Жадная вставка: сначала повторные, затем с меньшим числом допустимых исполнителей."""
        pool = sorted(pool, key=lambda o: (-o.weight, len(o.eligible), o.a, o.order_id))
        inserted = 0
        for _round in range(2):
            rest = []
            for o in pool:
                if time.perf_counter() > deadline:
                    return inserted, True
                best = None
                for kidx in o.eligible:
                    res = best_insertion(ctx, schedules[kidx], o)
                    if res is not None and (best is None or res[0] < best[0]):
                        best = (res[0], kidx, res[1])
                if best is None:
                    rest.append(o)
                    continue
                schedules[best[1]] = best[2]
                inserted += 1
            if len(rest) == len(pool):
                break
            pool = rest
        return inserted, False

    @staticmethod
    def _eject_insert(
        ctx: DayContext,
        schedules: dict[int, RouteSchedule],
        deadline: float,
    ) -> int:
        """Цепочка выталкивания длины 2: наряд o встаёт на место x, x переезжает к другому
        сотруднику (или снимается, если вес x меньше веса o). Возвращает прирост Σw."""
        routed = {o.order_id for s in schedules.values() for o in s.orders}
        pool = sorted(
            (o for o in ctx.orders if o.eligible and o.order_id not in routed),
            key=lambda o: (-o.weight, len(o.eligible), o.a, o.order_id),
        )
        gained = 0
        window = 3 * 3600
        for o in pool:
            if time.perf_counter() > deadline:
                break
            applied = False
            for kidx in o.eligible:
                sched = schedules[kidx]
                for x in sched.orders:
                    if x.weight > o.weight or abs(x.a - o.a) > window:
                        continue
                    base = decode(ctx, sched.staff, [y for y in sched.orders if y is not x])
                    if base is None:
                        continue
                    ins = best_insertion(ctx, base, o, max_tries=3)
                    if ins is None:
                        continue
                    moved = None
                    for k2 in x.eligible:
                        if k2 == kidx:
                            continue
                        res = best_insertion(ctx, schedules[k2], x, max_tries=3)
                        if res is not None and (moved is None or res[0] < moved[0]):
                            moved = (res[0], k2, res[1])
                    if moved is not None:
                        schedules[kidx] = ins[1]
                        schedules[moved[1]] = moved[2]
                        gained += o.weight
                        applied = True
                    elif x.weight < o.weight:
                        schedules[kidx] = ins[1]
                        gained += o.weight - x.weight
                        applied = True
                    if applied:
                        break
                    if time.perf_counter() > deadline:
                        break
                if applied or time.perf_counter() > deadline:
                    break
        return gained

    @staticmethod
    def _lns(
        ctx: DayContext,
        schedules: dict[int, RouteSchedule],
        weights: Weights,
        deadline: float,
        seed: int,
    ) -> dict:
        """Разрушить-и-восстановить: снимаются связанные наряды (близкие по месту и
        времени), затем жадная вставка снятых и нераспределённых. Принимается только
        улучшение W1·Σw − T."""
        rng = random.Random(seed)

        def value(scheds: dict[int, RouteSchedule]) -> int:
            return sum(weights.w1 * o.weight for s in scheds.values() for o in s.orders) - sum(
                s.travel for s in scheds.values() if s.orders
            )

        tau = ctx.tau
        current = value(schedules)
        iters = accepted = 0
        while time.perf_counter() < deadline:
            iters += 1
            routed = [(o, kidx) for kidx, s in schedules.items() for o in s.orders]
            if not routed:
                break
            seed_o, _ = rng.choice(routed)
            related = sorted(
                (x for x in routed if abs(x[0].a - seed_o.a) <= 2 * 3600),
                key=lambda x: tau[seed_o.point][x[0].point] + rng.random() * 600,
            )[: rng.randint(3, 8)]
            removed_ids = {x[0].order_id for x in related}
            trial = dict(schedules)
            ok = True
            for kidx in {x[1] for x in related}:
                s = trial[kidx]
                res = decode(ctx, s.staff, [o for o in s.orders if o.order_id not in removed_ids])
                if res is None:
                    ok = False
                    break
                trial[kidx] = res
            if not ok:
                continue
            in_routes = {o.order_id for s in trial.values() for o in s.orders}
            pool = [
                o
                for o in ctx.orders
                if o.eligible and o.order_id not in in_routes and abs(o.a - seed_o.a) <= 3 * 3600
            ]
            pool.sort(key=lambda o: (-o.weight, len(o.eligible) + rng.random() * 4))
            for o in pool:
                best = None
                for kidx in o.eligible:
                    res = best_insertion(ctx, trial[kidx], o, max_tries=3)
                    if res is not None and (best is None or res[0] < best[0]):
                        best = (res[0], kidx, res[1])
                if best is not None:
                    trial[best[1]] = best[2]
            val = value(trial)
            if val > current:
                schedules.update(trial)
                current = val
                accepted += 1
        return {"iterations": iters, "accepted": accepted}

    @staticmethod
    def _balance(ctx: DayContext, schedules: dict[int, RouteSchedule], deadline: float) -> dict:
        """Выравнивание загрузки внутри профилей (docs/planner-notes.md, «Баланс загрузки»).

        Минимизируется T + W4·Σ_p Σ_{k∈p}(ρ_k − ρ̄_p)²: p — профиль сотрудника, ρ̄_p — среднее по
        всем работающим сотрудникам профиля (пустые маршруты с ρ = 0). Ходы: перенос наряда от
        сотрудника выше среднего своего профиля к допустимому сотруднику ниже среднего (с
        учётом наряда двух профилей, например SURVEY_PRIVATE), затем обмен парой нарядов.
        Каждый ход проверяется точным декодером (H1–H10); число размещённых не меняется,
        суммарная дорога не растёт больше BALANCE_MAX_TRAVEL_GROWTH. В конце освободившееся
        время добирается жадной вставкой нераспределённых (размещённых только больше)."""
        group_of = {k.idx: "/".join(k.model.profiles) for k in ctx.staff}
        size: dict[str, int] = {}
        s1: dict[str, float] = {}
        s2: dict[str, float] = {}
        for kidx, p in group_of.items():
            u = schedules[kidx].utilization
            size[p] = size.get(p, 0) + 1
            s1[p] = s1.get(p, 0.0) + u
            s2[p] = s2.get(p, 0.0) + u * u

        def var(p: str, a: float, b: float) -> float:  # Σ(ρ − ρ̄)² = Σρ² − (Σρ)²/n
            return b - a * a / size[p]

        group_members: dict[str, list[int]] = {}
        for kidx, p in group_of.items():
            group_members.setdefault(p, []).append(kidx)

        def penalty() -> float:
            return sum(var(p, s1[p], s2[p]) for p in size)

        def dev(kidx: int) -> float:
            p = group_of[kidx]
            return schedules[kidx].utilization - s1[p] / size[p]

        def has_lighter(kidx: int) -> bool:
            p = group_of[kidx]
            u = schedules[kidx].utilization
            return any(schedules[k].utilization < u - 1e-9 for k in group_members[p] if k != kidx)

        def evaluate(changes: list[tuple[int, RouteSchedule]]) -> tuple[float, int]:
            d1: dict[str, float] = {}
            d2: dict[str, float] = {}
            dtr = 0
            for kidx, new in changes:
                old = schedules[kidx]
                p = group_of[kidx]
                un, uo = new.utilization, old.utilization
                d1[p] = d1.get(p, 0.0) + un - uo
                d2[p] = d2.get(p, 0.0) + un * un - uo * uo
                dtr += new.travel - old.travel
            dvar = sum(var(p, s1[p] + d1[p], s2[p] + d2[p]) - var(p, s1[p], s2[p]) for p in d1)
            return dtr + W4_BALANCE * dvar, dtr

        travel0 = sum(s.travel for s in schedules.values())
        cap = travel0 * (1 + BALANCE_MAX_TRAVEL_GROWTH)
        state = {"travel": travel0}

        def apply(changes: list[tuple[int, RouteSchedule]]) -> None:
            for kidx, new in changes:
                old = schedules[kidx]
                p = group_of[kidx]
                s1[p] += new.utilization - old.utilization
                s2[p] += new.utilization ** 2 - old.utilization ** 2
                state["travel"] += new.travel - old.travel
                schedules[kidx] = new

        def movable(o: OrderCtx) -> bool:
            return not (ctx.joint and jnt.is_joint_code(o.work_type.code))  # пара закреплена

        def out_of_time() -> bool:
            return time.perf_counter() > deadline

        def accept(d: float, dtr: int, best) -> bool:
            return d < -1e-6 and state["travel"] + dtr <= cap and (best is None or d < best[0])

        def relocate(si: int) -> bool:
            src = schedules[si]
            dsrc = dev(si)
            best = None
            for o in src.orders:
                if not movable(o):
                    continue
                targets = sorted(
                    (k for k in o.eligible if k != si and dev(k) < dsrc), key=dev
                )[:BALANCE_TARGETS]
                if not targets:
                    continue
                rest = decode(ctx, src.staff, [x for x in src.orders if x is not o])
                if rest is None:
                    continue
                for k in targets:
                    ins = best_insertion(ctx, schedules[k], o, max_tries=3)
                    if ins is None:
                        continue
                    changes = [(si, rest), (k, ins[1])]
                    d, dtr = evaluate(changes)
                    if accept(d, dtr, best):
                        best = (d, changes)
                if out_of_time():
                    break
            if best is None:
                return False
            apply(best[1])
            return True

        def swap(si: int) -> bool:
            src = schedules[si]
            dsrc = dev(si)
            dsts = sorted((k for k in schedules if k != si and dev(k) < dsrc and schedules[k].orders),
                          key=dev)[:4]
            rest_x: dict[tuple[int, str], RouteSchedule | None] = {}
            best = None
            for o in src.orders:
                if not movable(o):
                    continue
                elig_o = set(o.eligible)
                if not any(di in elig_o for di in dsts):
                    continue
                rest_o = decode(ctx, src.staff, [y for y in src.orders if y is not o])
                if rest_o is None:
                    continue
                for di in dsts:
                    if di not in elig_o:
                        continue
                    for x in schedules[di].orders:
                        if (not movable(x) or x.p >= o.p or si not in x.eligible
                                or abs(x.a - o.a) > 3 * 3600):
                            continue
                        key = (di, x.order_id)
                        if key not in rest_x:
                            dst = schedules[di]
                            rest_x[key] = decode(ctx, dst.staff, [y for y in dst.orders if y is not x])
                        if rest_x[key] is None:
                            continue
                        ins_o = best_insertion(ctx, rest_x[key], o, max_tries=3)
                        if ins_o is None:
                            continue
                        ins_x = best_insertion(ctx, rest_o, x, max_tries=3)
                        if ins_x is None:
                            continue
                        changes = [(si, ins_x[1]), (di, ins_o[1])]
                        d, dtr = evaluate(changes)
                        if accept(d, dtr, best):
                            best = (d, changes)
                if out_of_time():
                    break
            if best is None:
                return False
            apply(best[1])
            return True

        stats = {"moves": 0, "swaps": 0, "rounds": 0, "refill": 0,
                 "penalty_before": round(penalty(), 4)}
        while not out_of_time():
            stats["rounds"] += 1
            # источник — любой с нарядами и загрузкой выше самого свободного в своём профиле
            # (в том числе ниже среднего: перенос к менее загруженному тоже уменьшает штраф)
            srcs = sorted((k for k in schedules if schedules[k].orders and has_lighter(k)),
                          key=lambda k: -dev(k))
            changed = False
            for si in srcs:
                if out_of_time():
                    break
                if has_lighter(si) and relocate(si):
                    stats["moves"] += 1
                    changed = True
            if not changed:
                for si in srcs:
                    if out_of_time():
                        break
                    if has_lighter(si) and swap(si):
                        stats["swaps"] += 1
                        changed = True
            if not changed:
                break

        # освободившееся у перегруженных время — нераспределённым (только прирост размещённых)
        routed = {o.order_id for s in schedules.values() for o in s.orders}
        pool = [o for o in ctx.orders if o.eligible and o.order_id not in routed and movable(o)]
        if pool and not out_of_time():
            stats["refill"], _ = PyVrpPlanner._insert(ctx, schedules, pool, deadline)
        stats["penalty_after"] = round(sum(
            var(p, sum(schedules[k].utilization for k in group_of if group_of[k] == p),
                sum(schedules[k].utilization ** 2 for k in group_of if group_of[k] == p))
            for p in size), 4)  # пересчёт с нуля: добор шёл мимо инкрементальных сумм
        stats["travel_delta_sec"] = sum(s.travel for s in schedules.values()) - travel0
        stats["out_of_time"] = out_of_time()
        return stats

    @staticmethod
    def _joint_pairs(ctx: DayContext, schedules: dict[int, RouteSchedule], deadline: float) -> dict:
        """Пост-шаг совместных нарядов (docs/planner-notes.md, «Совместные наряды»).

        Для каждого размещённого INST_PRIVATE_VLS: (1) напарник к прежнему началу s — вставка
        закреплённого визита [s, s] в маршрут другого допустимого сотрудника (остальные его
        визиты могут сдвинуться в своих окнах); (2) иначе наряд снимается и ищется лучшая
        тройка (s из сетки окна, основной, напарник); (3) иначе — в unassigned с причиной.
        Освободившееся время добирается жадной вставкой обычных нарядов."""
        stats = {"pairs": 0, "moved": 0, "added": 0, "dropped": 0, "refill": 0}
        routed_kidx = {o.order_id: kidx for kidx, s in schedules.items() for o in s.orders}
        # сначала размещённые (их пару ищем у прежнего начала), затем неразмещённые с ≥ 2 допустимыми
        mains = sorted(
            ((routed_kidx.get(o.order_id), o) for o in ctx.orders
             if jnt.is_joint(o) and (o.order_id in routed_kidx or len(o.eligible) >= 2)),
            key=lambda x: (x[0] is None, x[1].a, x[1].order_id),
        )

        def try_insert(k: int, x: OrderCtx):
            res = best_insertion(ctx, schedules[k], x, max_tries=4)
            return None if res is None else (res[0], res[1])

        for kidx, o in mains:
            if kidx is None and time.perf_counter() > deadline:
                ctx.joint_fail[o.order_id] = jnt.unassigned_text(o, len(o.eligible))[1]
                continue
            sched = schedules[kidx] if kidx is not None else None
            s0 = None
            if sched is not None:
                k = sched.staff
                s0 = next((st.st for st in sched.stops if st.kind == VISIT and st.order is o), None)
            if s0 is None:
                # не размещён (или снят починкой маршрута при подборе другой пары): только поиск пары
                found = jnt.search(o, jnt.start_grid(o, ctx.now), o.eligible, try_insert)
                if found is not None:
                    _, k1, r1, _, k2, r2, _ = found
                    schedules[k1], schedules[k2] = r1, r2
                    stats["pairs"] += 1
                    stats["moved" if kidx is not None else "added"] += 1
                else:
                    ctx.joint_fail[o.order_id] = jnt.unassigned_text(o, len(o.eligible))[1]
                    if kidx is not None:
                        stats["dropped"] += 1
                continue
            # (1) напарник к прежнему началу
            p = jnt.partner_ctx(o, s0)
            best = None
            for k2 in o.eligible:
                if k2 == kidx:
                    continue
                r = try_insert(k2, p)
                if r is not None and (best is None or r[0] < best[0]):
                    best = (r[0], k2, r[1])
            if best is not None:
                m = jnt.pinned(o, s0)
                fixed = decode(ctx, k, [m if x is o else x for x in sched.orders])
                if fixed is not None:
                    schedules[kidx] = fixed
                    schedules[best[1]] = best[2]
                    stats["pairs"] += 1
                    continue
            # (2) снять и подобрать пару заново
            rest = [x for x in sched.orders if x is not o]
            base = decode(ctx, k, rest)
            if base is None:
                base, removed = PyVrpPlanner._feasible_schedule(ctx, k, rest)
            schedules[kidx] = base
            found = jnt.search(o, jnt.start_grid(o, ctx.now), o.eligible, try_insert)
            if found is not None:
                _, k1, r1, _, k2, r2, _ = found
                schedules[k1] = r1
                schedules[k2] = r2
                stats["pairs"] += 1
                stats["moved"] += 1
                continue
            # (3) пары нет
            _, text = jnt.unassigned_text(o, len(o.eligible))
            ctx.joint_fail[o.order_id] = text
            stats["dropped"] += 1

        if stats["dropped"] or stats["moved"]:
            routed = {o.order_id for s in schedules.values() for o in s.orders}
            pool = [o for o in ctx.orders if o.eligible and o.order_id not in routed and not jnt.is_joint(o)]
            before = sum(len(s.orders) for s in schedules.values())
            PyVrpPlanner._insert(ctx, schedules, pool, max(deadline, time.perf_counter() + 1.0))
            stats["refill"] = sum(len(s.orders) for s in schedules.values()) - before
        return stats

    @staticmethod
    def _assemble(ctx, day, schedules, clusters, exhausted) -> Plan:
        now = day.now
        if ctx.joint:
            main_staff = {o.order_id: s.staff.staff_id for s in schedules.values() for o in s.orders}
            ctx.joint_staff = {
                m: (main_staff[m], s.staff.staff_id)
                for s in schedules.values()
                for o in s.orders
                if (m := jnt.main_of(o.order_id)) and m in main_staff
            }
        routes = [assemble.route_model(ctx, schedules[k.idx]) for k in ctx.staff]
        routed = {o.order_id for s in schedules.values() for o in s.orders}
        unassigned = [
            assemble.classify_unassigned(ctx, o, schedules, budget_exhausted=exhausted)
            for o in ctx.orders
            if o.order_id not in routed
        ]
        node_names = {n.access_node_id: n.name for n in ctx.ref.access_nodes.values()}
        groups = assemble.pgp_groups(day.date, clusters, now, node_names)
        ids = assemble.AlertIds(day.date)
        alerts = [assemble.pgp_alert(ids, g, now) for g in groups]
        alerts += [assemble.unassigned_alert(ids, u, now) for u in unassigned]
        plan = Plan(
            date=day.date,
            plan_version=1,
            as_of=now,
            routes=routes,
            unassigned=unassigned,
            pgp_groups=groups,
            cancelled=list(day.cancelled.values()),
            alerts=alerts,
        )
        plan.summary = assemble.summary(plan, len(day.orders))
        return plan

    # ------------------------------------------------------------ events
    def apply_event(
        self,
        ref: ReferenceState,
        matrix: MatrixState,
        day: DayState,
        event: Event,
    ) -> PlanUpdate:
        from .events import apply_event

        pinned = self._day_modes.get(day.date)
        traffic = self.traffic if self.traffic is not None else (pinned[0] if pinned else None)
        joint = self.joint if self.joint is not None else (pinned[1] if pinned else None)
        update = apply_event(ref, matrix, day, event, traffic=traffic, joint=joint)
        self.last_event_stats = update.stats
        return update
