"""Тонкая обёртка над tools/sim: быстрые версии сценариев симулятора через TestClient.

Адаптер имеет тот же интерфейс, что и HTTP-клиент CLI (simlib.client.ApiClient над httpx-совместимым
клиентом). Полный прогон на живом сервере - только `python tools/sim/run_sim.py` (см. tools/sim/README.md).

Тест падает на нарушениях H1-H10, инвариантах, превышении времени и 5xx. Расхождения с контрактом из
KNOWN_CONTRACT_ISSUES (сейчас пуст) не валят тест; любое расхождение с контрактом - валит.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tools" / "sim"
if not (ROOT / "data" / "synth" / "travel_matrix.json").exists():
    pytest.skip("нет data/synth (сгенерируйте: python tools/synth/generate.py)", allow_module_level=True)
sys.path.insert(0, str(SIM))

from simlib.client import ApiClient  # noqa: E402
from simlib.data import SynthData  # noqa: E402
from simlib.report import FAIL_SEVERITIES  # noqa: E402
from simlib.session import Ctx  # noqa: E402

from app.main import create_app  # noqa: E402
from app.planner import make_planner  # noqa: E402
from app.store import Store  # noqa: E402

# Известные расхождения с data/api_spec.yaml (подстрока заголовка находки). Сейчас пусто: null у необязательных
# non-nullable полей устранён (app/api/serialize.py), любое расхождение с контрактом валит тест.
KNOWN_CONTRACT_ISSUES: tuple[str, ...] = ()

SCENARIOS = ["contract", "bulk", "calls", "churn", "pgp", "late"]


@pytest.fixture(scope="module")
def ctx():
    old = os.environ.get("PLANNER_DAY_BUDGET_SEC")
    os.environ["PLANNER_DAY_BUDGET_SEC"] = "10"
    app = create_app(planner=make_planner(), store=Store())
    try:
        with TestClient(app) as http:
            yield Ctx(ApiClient(http), SynthData(), seed=1, scale=0.12, time_limit=8, quick=True)
    finally:
        if old is None:
            os.environ.pop("PLANNER_DAY_BUDGET_SEC", None)
        else:
            os.environ["PLANNER_DAY_BUDGET_SEC"] = old


@pytest.mark.parametrize("name", SCENARIOS)
def test_scenario(ctx, name):
    res = importlib.import_module(f"scenarios.{name}").run(ctx)
    assert res.checks > 0, "сценарий не выполнил ни одной проверки"
    bad = []
    for f in res.findings.values():
        if f.severity not in FAIL_SEVERITIES:
            continue
        if f.severity == "contract" and any(k in f.title for k in KNOWN_CONTRACT_ISSUES):
            continue
        bad.append(f"[{f.severity}] {f.title} x{f.count}: {f.examples[0] if f.examples else ''}")
    assert not bad, f"сценарий {name} (seed {res.seed}):\n" + "\n".join(bad)
    assert not ctx.client.server_errors, ctx.client.server_errors[:3]


def test_checker_detects_tampered_plan(ctx):
    """Самопроверка инструмента: испорченный план (визит сдвинут за смену) должен давать нарушения H1-H10."""
    import copy

    from app.demo_dates import train_dates

    from simlib.report import ScenarioResult
    from simlib.session import DaySession, at

    res = ScenarioResult("selftest", 1)
    date = train_dates()[2]
    now = at(date, "08:30")
    s = DaySession(ctx, res, date, now, ctx.data.day_orders(date, now, 0.05, ctx.rng("selftest")), [], time_limit=5)
    assert s.setup()
    assert not res.findings or all(f.severity != "violation" for f in res.findings.values())
    bad = copy.deepcopy(s.plan_json)
    visit = next(st for r in bad["routes"] for st in r["stops"] if st["type"] == "VISIT")
    visit["start"] = visit["end"] = visit["arrival"] = f"{date}T23:30:00+05:00"
    res2 = ScenarioResult("selftest2", 1)
    ctx.checker.check(res2, s.model, bad, None, None, "tampered")
    assert any(f.severity == "violation" for f in res2.findings.values())
