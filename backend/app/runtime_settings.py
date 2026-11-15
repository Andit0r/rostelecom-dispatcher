"""Настройки режимов планировщика, переключаемые на лету из интерфейса диспетчера.

Значение = оверрайд (память + PostgreSQL settings[runtime_overrides], без БД — JSON-файл) →
переменная окружения → значение по умолчанию. При первом старте с БД без ключа файл один раз
импортируется в БД (файл не меняется).
Читается в местах использования через get(key); уже построенный план режим не меняет
(см. PyVrpPlanner: режим фиксируется на день при построении).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from .storage import repos

log = logging.getLogger("rostelecom.settings")

_TRUE = ("on", "1", "true", "yes")
_FALSE = ("off", "0", "false", "no")

# applies_to_plan: смена делает построенный план устаревшим
REGISTRY: dict[str, dict] = {
    "TRAFFIC_FACTORS": {
        "label": "Пробки по часам",
        "description": "Учитывать загруженность дорог по часам суток: время в пути умножается на "
                       "коэффициент часа отправления. Влияет на расчётное время прибытия и риск опозданий.",
        "type": "enum", "options": ["on", "off"], "default": "off", "risk": None, "plan_outdated": True,
    },
    "JOINT_ORDERS": {
        "label": "Совместные наряды (ВЛС)",
        "description": "Монтаж INST_PRIVATE_VLS выполняют двое одновременно: напарник получает "
                       "отдельный визит ORD-…-B с тем же началом.",
        "type": "enum", "options": ["on", "off"], "default": "off", "plan_outdated": True,
        "risk": "Визита напарника ORD-…-B нет во входных данных дня: валидатор организаторов может "
                "засчитать его как нарушение (неизвестный наряд). Для боевого сценария оставьте «off».",
    },
    "PLANNER_DAY_BUDGET_SEC": {
        "label": "Бюджет решателя на день, с",
        "description": "Максимум времени на построение плана дня: больше — выше качество, меньше — быстрее ответ.",
        "type": "number", "min": 10, "max": 120, "default": 60.0, "risk": None, "plan_outdated": False,
    },
    "UI_PLAN_BUDGET_SEC": {
        "label": "Бюджет расчёта в интерфейсе, с",
        "description": "Время расчёта при загрузке дня и пересборке из интерфейса диспетчера.",
        "type": "number", "min": 5, "max": 60, "default": 15.0, "risk": None, "plan_outdated": False,
    },
    "CLOCK_FOLLOWS_WALL": {
        "label": "Время плана идёт по часам",
        "description": "Раз в минуту сервер отправляет активному дню событие TIME_TICK с текущим временем по "
                       "Екатеринбургу (UTC+5). Включайте для живой работы с сегодняшним днём. Для проверки "
                       "организаторами и симулятора держите выключенной: иначе их более ранние события будут отклонены.",
        "type": "enum", "options": ["on", "off"], "default": "off", "plan_outdated": False,
        "risk": "Для проверки организаторами и симулятора держите выключенной: иначе их более ранние события "
                "будут отклонены.",
    },
}

_lock = threading.RLock()
_overrides: dict[str, object] = {}
_loaded_from: Path | None = None
_pg: repos.PgRepo | None = None  # settings (БД доступна при первой загрузке) — иначе JSON-файл
OVERRIDES_KEY = "runtime_overrides"


def _file() -> Path:
    env = os.environ.get("RUNTIME_SETTINGS_FILE")
    if env:
        return Path(env)
    base = os.environ.get("UI_DATA_DIR")
    if base:
        return Path(base) / "runtime_settings.json"
    return Path(__file__).resolve().parents[2] / "data" / "synth" / "runtime_settings.json"


def _parse(key: str, raw) -> object:
    """Приводит значение к типу настройки; ValueError с русским текстом."""
    spec = REGISTRY[key]
    if spec["type"] == "enum":
        if isinstance(raw, bool):
            raw = "on" if raw else "off"
        s = str(raw).strip().lower()
        if s in _TRUE:
            return "on"
        if s in _FALSE:
            return "off"
        raise ValueError(f"{key}: допустимы значения on или off")
    if isinstance(raw, bool):
        raise ValueError(f"{key}: ожидается число от {spec['min']} до {spec['max']}")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{key}: ожидается число от {spec['min']} до {spec['max']}") from None
    if not (spec["min"] <= v <= spec["max"]) or v != v:
        raise ValueError(f"{key}: значение {raw} вне диапазона {spec['min']}–{spec['max']}")
    return v


def _env_value(key: str):
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return None
    try:
        return _parse(key, raw)
    except ValueError:
        # прежнее поведение: мусор в окружении для бюджетов -> по умолчанию; для on/off -> off
        return "off" if REGISTRY[key]["type"] == "enum" else None


def _read_file(path: Path) -> dict[str, object]:
    out: dict[str, object] = {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for k, v in (raw.get("overrides", raw) if isinstance(raw, dict) else {}).items():
            if k in REGISTRY:
                try:
                    out[k] = _parse(k, v)
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("runtime_settings: файл %s не прочитан (%s), оверрайдов нет", path, exc)
    return out


def _ensure_loaded() -> None:
    global _loaded_from, _pg
    path = _file()
    if _loaded_from == path:
        return
    with _lock:
        if _loaded_from == path:
            return
        _overrides.clear()
        _pg = repos.pg_repo("settings")
        if _pg is not None:
            stored = _pg.get(OVERRIDES_KEY)
            if stored is None:  # ключа нет: однократный импорт файла (идемпотентно, файл не меняется)
                from_file = _read_file(path)
                if from_file:
                    repos.import_missing(_pg, {OVERRIDES_KEY: from_file}, "настройки режимов")
                stored = from_file
            for k, v in (stored if isinstance(stored, dict) else {}).items():
                if k in REGISTRY:
                    try:
                        _overrides[k] = _parse(k, v)
                    except ValueError:
                        pass
        else:
            _overrides.update(_read_file(path))
        _loaded_from = path


def _save() -> None:
    """Под _lock. БД: в очередь (подтверждение — _commit() вне lock, чтобы get() не ждал БД)."""
    if _pg is not None:
        _pg.put(OVERRIDES_KEY, dict(_overrides))
        return
    path = _file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".rtset-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"overrides": _overrides}, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("runtime_settings: не удалось сохранить %s: %s", path, exc)


def _commit() -> None:
    if _pg is not None:
        repos.flush()


def source(key: str) -> str:
    _ensure_loaded()
    if key in _overrides:
        return "override"
    return "env" if _env_value(key) is not None else "default"


def get(key: str):
    """Эффективное значение: оверрайд → окружение → умолчание. enum -> 'on'/'off', number -> float."""
    _ensure_loaded()
    with _lock:
        if key in _overrides:
            return _overrides[key]
    env = _env_value(key)
    return env if env is not None else REGISTRY[key]["default"]


def get_bool(key: str) -> bool:
    return get(key) == "on"


def describe() -> list[dict]:
    out = []
    for key, spec in REGISTRY.items():
        item = {"key": key, "label": spec["label"], "description": spec["description"],
                "type": spec["type"], "value": get(key), "default": spec["default"],
                "source": source(key), "risk": spec["risk"], "applies": "next_rebuild"}
        if spec["type"] == "enum":
            item["options"] = list(spec["options"])
        else:
            item["min"], item["max"] = spec["min"], spec["max"]
        out.append(item)
    return out


def update(changes: dict, who: str = "?") -> list[str]:
    """Частичное обновление; всё или ничего. Возвращает ключи, значение которых изменилось."""
    errors = []
    parsed: dict[str, object] = {}
    for k, v in changes.items():
        if k not in REGISTRY:
            errors.append(f"{k}: неизвестная настройка")
            continue
        try:
            parsed[k] = _parse(k, v)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("; ".join(errors))
    changed = []
    with _lock:
        _ensure_loaded()
        for k, new in parsed.items():
            old = get(k)
            _overrides[k] = new
            if old != new:
                changed.append(k)
                log.info("runtime_settings: %s: %s = %s -> %s", who, k, old, new)
        _save()
    _commit()
    return changed


def reset(who: str = "?") -> list[str]:
    with _lock:
        _ensure_loaded()
        before = {k: get(k) for k in REGISTRY}
        _overrides.clear()
        _save()
        changed = [k for k in REGISTRY if get(k) != before[k]]
    _commit()
    for k in changed:
        log.info("runtime_settings: %s: сброс %s: %s -> %s", who, k, before[k], get(k))
    return changed


def plan_outdated(keys) -> bool:
    return any(REGISTRY[k]["plan_outdated"] for k in keys if k in REGISTRY)
