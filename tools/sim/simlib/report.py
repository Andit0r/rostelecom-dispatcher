"""Результаты сценариев и общий отчёт (markdown + json)."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any

FAIL_SEVERITIES = {"violation", "contract", "invariant", "latency", "error"}
SEV_TITLE = {
    "violation": "нарушение H1-H10",
    "contract": "расхождение с контрактом",
    "invariant": "нарушение инварианта",
    "latency": "превышение времени ответа",
    "error": "сбой симулятора/сервера",
    "note": "замечание (не провал)",
}


@dataclass
class Finding:
    severity: str
    title: str
    count: int = 0
    examples: list[dict] = field(default_factory=list)


@dataclass
class ScenarioResult:
    name: str
    seed: int
    checks: int = 0
    failed: int = 0
    events: int = 0
    duration_sec: float = 0.0
    findings: dict[tuple[str, str], Finding] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    violations_seen: set = field(default_factory=set)
    latency: dict[str, dict] = field(default_factory=dict)
    max_examples: int = 4

    def add(self, severity: str, title: str, detail: str = "", step: Any = None) -> None:
        f = self.findings.setdefault((severity, title), Finding(severity, title))
        f.count += 1
        if len(f.examples) < self.max_examples:
            f.examples.append({"detail": detail[:600], "step": step, "seed": self.seed})

    def check(self, cond: bool, title: str, detail: str = "", severity: str = "contract", step: Any = None) -> bool:
        self.checks += 1
        if not cond:
            self.failed += 1
            self.add(severity, title, detail, step)
        return bool(cond)

    def schema_check(self, what: str, errs: list[str], step: Any = None) -> bool:
        """Результат проверки схемы: каждая уникальная (без индексов) ошибка - отдельная находка."""
        import re

        self.checks += 1
        if not errs:
            return True
        self.failed += 1
        seen = set()
        for e in errs:
            pat = re.sub(r"\[\d+\]", "[]", e)
            if pat not in seen:
                seen.add(pat)
                self.add("contract", f"{what}: {pat}", e, step)
        return False

    def note(self, text: str) -> None:
        self.notes.append(text)

    @property
    def status(self) -> str:
        return "FAIL" if any(f.severity in FAIL_SEVERITIES for f in self.findings.values()) else "PASS"

    def to_json(self) -> dict:
        return {
            "name": self.name, "seed": self.seed, "status": self.status, "checks": self.checks,
            "failed_checks": self.failed, "events": self.events, "duration_sec": round(self.duration_sec, 2),
            "metrics": self.metrics, "notes": self.notes, "latency": self.latency,
            "findings": [
                {"severity": f.severity, "title": f.title, "count": f.count, "examples": f.examples}
                for f in self.findings.values()
            ],
        }


def render_markdown(results: list[ScenarioResult], meta: dict, latency_all: dict, server_errors: list[str]) -> str:
    L: list[str] = []
    L.append("# Отчёт симулятора нагрузки")
    L.append("")
    L.append(f"- Сервер: `{meta.get('base_url')}`; seed: {meta.get('seed')}; масштаб: {meta.get('scale')}; "
             f"time_limit: {meta.get('time_limit')} с; дата: {meta.get('generated')}")
    total_events = sum(r.events for r in results)
    passed = sum(1 for r in results if r.status == "PASS")
    L.append(f"- Сценариев: {len(results)}, прошло: {passed}, упало: {len(results) - passed}; "
             f"событий отправлено: {total_events}; проверок: {sum(r.checks for r in results)} "
             f"(не прошло {sum(r.failed for r in results)})")
    if server_errors:
        L.append(f"- Ответов 5xx / сетевых сбоев: {len(server_errors)}")
    L.append("")
    L.append("## Сводка по сценариям")
    L.append("")
    L.append("| Сценарий | Статус | Проверок | Не прошло | Событий | Время, с | Находок |")
    L.append("|---|---|---|---|---|---|---|")
    for r in results:
        bad = sum(1 for f in r.findings.values() if f.severity in FAIL_SEVERITIES)
        L.append(f"| {r.name} | **{r.status}** | {r.checks} | {r.failed} | {r.events} | {r.duration_sec:.1f} | {bad} |")
    L.append("")
    L.append("## Латентность запросов (с, реальное время)")
    L.append("")
    L.append("| Запрос | n | p50 | p95 | max |")
    L.append("|---|---|---|---|---|")
    for k, v in latency_all.items():
        L.append(f"| {k} | {v['n']} | {v['p50']} | {v['p95']} | {v['max']} |")
    L.append("")
    for r in results:
        L.append(f"## Сценарий `{r.name}` - {r.status}")
        L.append("")
        if r.metrics:
            L.append("Метрики:")
            for k, v in r.metrics.items():
                L.append(f"- {k}: {json.dumps(v, ensure_ascii=False, default=str)}")
            L.append("")
        if r.notes:
            L.append("Примечания:")
            for n in r.notes:
                L.append(f"- {n}")
            L.append("")
        fails = [f for f in r.findings.values() if f.severity in FAIL_SEVERITIES]
        notes = [f for f in r.findings.values() if f.severity not in FAIL_SEVERITIES]
        if not fails:
            L.append("Нарушений и расхождений не найдено.")
            L.append("")
        for grp, title in ((fails, "Находки"), (notes, "Замечания")):
            if not grp:
                continue
            L.append(f"{title}:")
            L.append("")
            for f in grp:
                L.append(f"- **[{SEV_TITLE.get(f.severity, f.severity)}] {f.title}** - {f.count} раз(а)")
                for ex in f.examples:
                    L.append(f"  - seed={ex['seed']}, шаг={ex['step']}: {ex['detail']}")
            L.append("")
    if server_errors:
        L.append("## Ответы 5xx и сбои соединения")
        L.append("")
        for e in server_errors[:30]:
            L.append(f"- {e}")
        L.append("")
    L.append("## Как воспроизвести")
    L.append("")
    L.append("`python tools/sim/run_sim.py --base-url <url> --scenario <имя> --seed <seed> --scale <масштаб> "
             "--time-limit <с>`; номер шага в находке - порядковый номер события сценария (с 1), либо имя проверки.")
    return "\n".join(L) + "\n"


def build_json(results: list[ScenarioResult], meta: dict, latency_all: dict, server_errors: list[str]) -> dict:
    return {
        "meta": meta, "generated": dt.datetime.now().isoformat(timespec="seconds"),
        "summary": {
            "scenarios": len(results), "passed": sum(1 for r in results if r.status == "PASS"),
            "failed": sum(1 for r in results if r.status != "PASS"),
            "events": sum(r.events for r in results), "checks": sum(r.checks for r in results),
            "failed_checks": sum(r.failed for r in results), "server_errors": len(server_errors),
        },
        "latency": latency_all, "server_errors": server_errors[:100],
        "scenarios": [r.to_json() for r in results],
    }
