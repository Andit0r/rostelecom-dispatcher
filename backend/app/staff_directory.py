"""Справочник сотрудников с ФИО: /ui/directory/staff (вне контракта /api/v1).

Источник состава — справочники в Store (PUT /api/v1/reference или «Создать день» с подгрузкой из каталога):
при каждой загрузке справочников (reference_sync.on_reference_loaded) для staff_id, которых ещё нет среди
профилей, создаётся запись с пустыми ФИО и телефоном (display_name «Сотрудник <staff_id>», должность по
профилю, табельный номер = staff_id). Исключение — служебная загрузка каталога: тогда новые записи получают
ФИО из data/synth/staff_names.json (заготовка, читается только в catalog.py). Существующие записи (ФИО,
введённые диспетчером, перенесённые из прежнего файла/БД) не трогаются никогда; записи, которых нет в
текущем справочнике, остаются с пометкой in_reference=false. Выдуманные ФИО не генерируются.

Профиль человека (ФИО, телефон, должность, табельный номер, заметка) хранится на нашей стороне: в PostgreSQL
(таблица staff_profiles, значение {"profile": {...}, "equipment": {...}|null}; запись синхронная с таймаутом,
кэш в памяти), если БД доступна при старте, иначе в JSON-файле (UI_DATA_DIR/staff_directory.json, путь можно
переопределить STAFF_DIRECTORY_FILE). При первом старте с пустой таблицей файл один раз импортируется в БД как
есть (сам файл не меняется). Запись в файл атомарная (временный файл + os.replace) под собственной блокировкой.
Если каталог только для чтения, справочник работает в памяти процесса (persisted=false).

Поля контракта (профили, технологии, смена, СЦ, рюкзак АО «по данным») берутся из справочника в Store
(reference_sync.base_reference). «Рюкзак АО» можно переопределить по staff_id: ключ equipment_overrides в том
же файле. Переопределения применяются только при создании/пересборке дня из интерфейса — /api/v1 отдаёт то,
что пришло в PUT reference.
"""

from __future__ import annotations

import json
import os
import re
import threading
from functools import wraps
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import ApiError
from .service import _parse
from .storage import repos
from . import reference_sync
from .ui_api import DATA

router = APIRouter(prefix="/ui/directory", tags=["ui-directory"])

POSITIONS = {"LTP3": "Инженер ЛТП", "INSTALL": "Инсталлятор"}
_STAFF_ID = re.compile(r"^ST-([0-9]{3})$")
_NAME_FIELDS = ("first_name", "last_name", "middle_name", "phone", "position", "personnel_number")


def _initials(first: str | None, middle: str | None) -> str:
    return " ".join(f"{p.strip()[0]}." for p in (first, middle) if p and p.strip())


def format_display_name(p: dict) -> str:
    """«Фамилия И. О.»; без фамилии — «Сотрудник <staff_id>»."""
    last = (p.get("last_name") or "").strip()
    if not last:
        return f"Сотрудник {p.get('staff_id', '')}".strip()
    ini = _initials(p.get("first_name"), p.get("middle_name"))
    return f"{last} {ini}".strip()


def _full_name(p: dict) -> str:
    return " ".join(x.strip() for x in (p.get("last_name"), p.get("first_name"), p.get("middle_name")) if x and x.strip())


# ---------------------------------------------------------------- исходные данные

EQUIPMENT_KINDS = ("ONT", "ROUTER", "STB")
EQUIPMENT_MAX = 99  # верхняя граница ввода: больше в машину всё равно не влезет
_KIND_RU = {"ONT": "ONT", "ROUTER": "роутер", "STB": "STB"}


def parse_equipment(value, field: str) -> dict[str, int] | None:
    """Частичный рюкзак {ONT?, ROUTER?, STB?}: целые 0…99. Иначе 422 с понятным текстом."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ApiError(422, "INVALID_EQUIPMENT", f"Поле {field}: ожидается объект {{ONT, ROUTER, STB}}",
                       [{"field": field, "message": "ожидается объект"}])
    out: dict[str, int] = {}
    for k, v in value.items():
        if k not in EQUIPMENT_KINDS:
            raise ApiError(422, "INVALID_EQUIPMENT", f"Поле {field}: неизвестный вид АО «{k}» (ONT, ROUTER, STB)",
                           [{"field": f"{field}.{k}", "message": "неизвестный вид АО"}])
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= EQUIPMENT_MAX:
            raise ApiError(422, "INVALID_EQUIPMENT",
                           f"Поле {field}.{k}: нужно целое число от 0 до {EQUIPMENT_MAX}, получено {v!r}",
                           [{"field": f"{field}.{k}", "message": f"целое 0…{EQUIPMENT_MAX}"}])
        out[k] = v
    return out or None


def _eq_dict(value) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    try:
        return {k: int(value.get(k, 0)) for k in EQUIPMENT_KINDS}
    except (TypeError, ValueError):
        return None


_base_cache: tuple[object, dict] | None = None


def _base_staff() -> dict[str, dict]:
    """staff_id -> данные планировщика из исходного справочника в Store; справочника нет — {}."""
    global _base_cache
    ref = reference_sync.base_reference()
    if ref is None:
        return {}
    cached = _base_cache
    if cached is not None and cached[0] is ref:
        return cached[1]
    out = {}
    for s in ref.staff.values():
        d = s.model_dump(mode="json", by_alias=True)
        out[s.staff_id] = {"profiles": list(d.get("profiles") or []), "service_center_id": d.get("service_center_id"),
                           "technologies": list(d.get("technologies") or []), "height_permit": d.get("height_permit"),
                           "shift": d.get("shift"), "experience_years": d.get("experience_years"),
                           "ref_display_name": d.get("display_name"),
                           "equipment_start": _eq_dict(d.get("equipment_start")),
                           "equipment_capacity": _eq_dict(d.get("equipment_capacity"))}
    _base_cache = (ref, out)
    return out


def _service_center_ids() -> frozenset[str]:
    ref = reference_sync.current()
    return frozenset(ref.service_centers) if ref is not None else frozenset()


def reference_loaded() -> bool:
    return reference_sync.current() is not None


def _path() -> Path:
    env = os.environ.get("STAFF_DIRECTORY_FILE")
    return Path(env) if env else DATA / "staff_directory.json"


# ---------------------------------------------------------------- хранилище

def _committing(fn):
    """После мутации (lock уже отпущен) дождаться записи в БД; без БД ничего не делает."""
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        finally:
            self._commit()
    return wrapper


def _clean_override(sid, ov) -> dict | None:
    start = _eq_dict(ov.get("equipment_start")) if isinstance(ov, dict) else None
    cap = _eq_dict(ov.get("equipment_capacity")) if isinstance(ov, dict) else None
    if _STAFF_ID.match(str(sid)) and start and cap and all(0 <= start[k] <= cap[k] for k in EQUIPMENT_KINDS):
        return {"equipment_start": start, "equipment_capacity": cap}
    return None


class StaffDirectory:
    """Профили по staff_id. Все операции — под self.lock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.persisted = True
        self.backend = "file"  # "postgres", если при первом обращении БД была доступна
        self._pg: repos.PgRepo | None = None
        self._profiles: dict[str, dict] | None = None
        # staff_id → {"equipment_start": {...}, "equipment_capacity": {...}} — переопределения «рюкзака АО»
        self._equipment: dict[str, dict] = {}

    def _read_file(self) -> tuple[dict[str, dict], dict[str, dict], bool]:
        """(профили, переопределения рюкзака, файл_битый). Нет файла — пусто."""
        profiles: dict[str, dict] = {}
        equipment: dict[str, dict] = {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            items = raw.get("staff", []) if isinstance(raw, dict) else raw
            profiles = {p["staff_id"]: p for p in items if isinstance(p, dict) and _STAFF_ID.match(str(p.get("staff_id")))}
            # старый файл (без equipment_overrides) читается как «переопределений нет»
            overrides = raw.get("equipment_overrides", {}) if isinstance(raw, dict) else {}
            for sid, ov in (overrides.items() if isinstance(overrides, dict) else ()):
                clean = _clean_override(sid, ov)
                if clean:
                    equipment[sid] = clean
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError):
            return {}, {}, True  # битый файл: работаем в памяти, поверх не пишем, пока нет правок
        return profiles, equipment, False

    def _load(self, auto_sync: bool = True) -> dict[str, dict]:
        if self._profiles is not None:
            return self._profiles
        corrupt = False
        pg = repos.pg_repo("staff_profiles")
        if pg is not None:
            self._pg, self.backend = pg, "postgres"
            rows = pg.all()
            if not rows:  # пустая таблица: однократный импорт файла (идемпотентно, файл не меняется)
                f_profiles, f_equipment, _ = self._read_file()
                if f_profiles:
                    repos.import_missing(pg, {sid: {"profile": p, "equipment": f_equipment.get(sid)}
                                              for sid, p in f_profiles.items()}, "справочник сотрудников")
                    rows = pg.all()
            profiles = {sid: r["profile"] for sid, r in rows.items() if isinstance(r, dict) and r.get("profile")}
            self._equipment = {sid: r["equipment"] for sid, r in rows.items()
                               if isinstance(r, dict) and r.get("equipment") and sid in profiles}
        else:
            profiles, self._equipment, corrupt = self._read_file()
        self._profiles = profiles
        self._corrupt = corrupt
        ref = reference_sync.current() if auto_sync else None
        if ref is not None:  # справочники уже в Store (например, восстановлены из БД до первого обращения)
            self._sync_locked(list(ref.staff), {})
        return profiles

    def _sync_locked(self, staff_ids: list[str], names: dict[str, dict]) -> int:
        """Под self.lock: записи для новых staff_id (пустые ФИО или заготовка names). Существующие не меняются."""
        profiles = self._profiles
        assert profiles is not None
        base = _base_staff()
        created = 0
        for sid in staff_ids:
            if sid in profiles or not _STAFF_ID.match(sid):
                continue
            prof = ((base.get(sid) or {}).get("profiles") or ["INSTALL"])[0]
            record = {"staff_id": sid, "first_name": None, "last_name": None, "middle_name": None, "phone": None,
                      "position": POSITIONS.get(prof, "Инженер"), "personnel_number": sid, "note": None}
            hint = names.get(sid)
            if hint:
                record.update({k: hint.get(k) for k in _NAME_FIELDS if hint.get(k)})
            profiles[sid] = record
            created += 1
        if created and not getattr(self, "_corrupt", False):
            self._save()
        return created

    @_committing
    def sync(self, ref, names: dict[str, dict] | None = None) -> int:
        """После загрузки справочников: добавить недостающие записи (пустые ФИО; при загрузке каталога —
        заготовка names). Существующие записи и записи вне справочника не трогаются."""
        with self.lock:
            self._load(auto_sync=False)
            return self._sync_locked(list(ref.staff), names or {})

    def _save(self) -> None:
        """Под self.lock. БД: в очередь только изменившиеся профили (подтверждение — _commit() вне lock)."""
        assert self._profiles is not None
        if self._pg is not None:
            repos.sync_repo(self._pg, {sid: {"profile": p, "equipment": self._equipment.get(sid)}
                                       for sid, p in self._profiles.items()})
            return
        data = {"version": 1, "staff": [self._profiles[k] for k in sorted(self._profiles)],
                "equipment_overrides": {k: self._equipment[k] for k in sorted(self._equipment)}}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            self.persisted = True
        except OSError:
            self.persisted = False  # каталог только для чтения: храним в памяти процесса

    def _commit(self) -> None:
        """Вне self.lock: дождаться записи в БД (до 3 с); недоступна — работаем по кэшу, запись догонит."""
        if self._pg is not None:
            self.persisted = repos.flush()

    # ------------------------------------------------------------ чтение

    def _view(self, p: dict) -> dict:
        base = _base_staff().get(p["staff_id"])
        profiles = list(base["profiles"]) if base else ([p["profile"]] if p.get("profile") else [])
        return {
            "staff_id": p["staff_id"],
            "display_name": format_display_name(p),
            "full_name": _full_name(p),
            "name_empty": not (p.get("last_name") or "").strip(),
            "first_name": p.get("first_name"),
            "last_name": p.get("last_name"),
            "middle_name": p.get("middle_name"),
            "phone": p.get("phone"),
            "position": p.get("position"),
            "personnel_number": p.get("personnel_number"),
            "note": p.get("note"),
            "profile": profiles[0] if profiles else None,
            "profiles": profiles,
            "service_center_id": base["service_center_id"] if base else p.get("service_center_id"),
            "technologies": base["technologies"] if base else [],
            "height_permit": base["height_permit"] if base else None,
            "shift": base["shift"] if base else None,
            # Есть в текущем справочнике (Store). Нет — запись сохранена, но в план не попадает.
            "in_reference": base is not None,
            "in_planner": base is not None,
            **self._equipment_view(p["staff_id"]),
        }

    # ------------------------------------------------------------ рюкзак АО

    def _equipment_data(self, staff_id: str) -> tuple[dict, dict] | None:
        base = _base_staff().get(staff_id)
        if not base or not base["equipment_start"] or not base["equipment_capacity"]:
            return None
        return dict(base["equipment_start"]), dict(base["equipment_capacity"])

    def _effective(self, staff_id: str) -> tuple[dict, dict] | None:
        data = self._equipment_data(staff_id)
        if data is None:
            return None
        ov = self._equipment.get(staff_id)
        return (dict(ov["equipment_start"]), dict(ov["equipment_capacity"])) if ov else data

    def _equipment_view(self, staff_id: str) -> dict:
        data = self._equipment_data(staff_id)
        eff = self._effective(staff_id)
        return {
            "equipment_start_data": data[0] if data else None,
            "equipment_capacity_data": data[1] if data else None,
            "equipment_start": eff[0] if eff else None,
            "equipment_capacity": eff[1] if eff else None,
            "equipment_override": staff_id in self._equipment,
        }

    def _set_equipment(self, staff_id: str, start: dict | None, capacity: dict | None) -> None:
        """Сливает частичные start/capacity с текущими эффективными, проверяет и запоминает.
        Совпало с данными — переопределение снимается. Без self._save()."""
        eff = self._effective(staff_id)
        if eff is None:
            raise ApiError(422, "EQUIPMENT_NOT_APPLICABLE",
                           f"У {staff_id} нет в текущем справочнике (PUT reference): рюкзак АО задать нельзя",
                           [{"field": "equipment_start", "message": "сотрудник не в планировщике"}])
        new_start = {**eff[0], **(start or {})}
        new_cap = {**eff[1], **(capacity or {})}
        bad = [k for k in EQUIPMENT_KINDS if new_cap[k] < new_start[k]]
        if bad:
            names = ", ".join(f"{_KIND_RU[k]}: старт {new_start[k]} > максимум {new_cap[k]}" for k in bad)
            raise ApiError(422, "EQUIPMENT_CAPACITY_LESS_THAN_START",
                           f"У {staff_id} максимум меньше стартового количества ({names})",
                           [{"field": f"equipment_capacity.{k}", "message": "должно быть не меньше старта"} for k in bad])
        if (new_start, new_cap) == self._equipment_data(staff_id):
            self._equipment.pop(staff_id, None)
        else:
            self._equipment[staff_id] = {"equipment_start": new_start, "equipment_capacity": new_cap}

    @_committing
    def set_equipment(self, staff_id: str, start: dict | None, capacity: dict | None) -> dict:
        with self.lock:
            p = self._load().get(staff_id)
            if p is None:
                raise ApiError(404, "STAFF_NOT_FOUND", f"Сотрудник {staff_id} не найден в справочнике")
            self._set_equipment(staff_id, start, capacity)
            self._save()
            return self._view(p)

    @_committing
    def reset_equipment(self, staff_id: str) -> dict:
        with self.lock:
            p = self._load().get(staff_id)
            if p is None:
                raise ApiError(404, "STAFF_NOT_FOUND", f"Сотрудник {staff_id} не найден в справочнике")
            if self._equipment.pop(staff_id, None) is not None:
                self._save()
            return self._view(p)

    def _bulk_targets(self, profile: str | None) -> list[str]:
        base = _base_staff()
        return [sid for sid in sorted(self._load())
                if sid in base and self._equipment_data(sid) is not None
                and (profile is None or profile in base[sid]["profiles"])]

    @_committing
    def bulk_equipment(self, start: dict | None, capacity: dict | None, profile: str | None) -> int:
        """Задать рюкзак всем (или профилю) атомарно: при ошибке у кого-то не меняется никто."""
        with self.lock:
            targets = self._bulk_targets(profile)
            bad = []
            for sid in targets:
                eff = self._effective(sid)
                s, c = {**eff[0], **(start or {})}, {**eff[1], **(capacity or {})}
                if any(c[k] < s[k] for k in EQUIPMENT_KINDS):
                    bad.append(sid)
            if bad:
                raise ApiError(422, "EQUIPMENT_CAPACITY_LESS_THAN_START",
                               f"У {len(bad)} сотр. максимум получится меньше старта ({', '.join(bad[:5])}"
                               f"{'…' if len(bad) > 5 else ''}): задайте и максимум",
                               [{"field": "capacity", "message": "должно быть не меньше старта"}])
            for sid in targets:
                self._set_equipment(sid, start, capacity)
            self._save()
            return len(targets)

    @_committing
    def bulk_reset_equipment(self, profile: str | None) -> int:
        with self.lock:
            targets = [sid for sid in self._bulk_targets(profile) if sid in self._equipment]
            for sid in targets:
                self._equipment.pop(sid)
            if targets:
                self._save()
            return len(targets)

    def apply_equipment(self, staff: list[dict]) -> list[dict]:
        """Копия списка staff справочника с эффективным рюкзаком (для создания/пересборки дня)."""
        with self.lock:
            self._load()
            out = []
            for s in staff:
                ov = self._equipment.get(s.get("staff_id")) if isinstance(s, dict) else None
                out.append({**s, **{k: dict(v) for k, v in ov.items()}} if ov else s)
            return out

    def list(self) -> list[dict]:
        with self.lock:
            return [self._view(p) for _, p in sorted(self._load().items())]

    def get(self, staff_id: str) -> dict:
        with self.lock:
            p = self._load().get(staff_id)
            if p is None:
                raise ApiError(404, "STAFF_NOT_FOUND", f"Сотрудник {staff_id} не найден в справочнике")
            return self._view(p)

    def names(self) -> dict[str, str]:
        with self.lock:
            return {sid: format_display_name(p) for sid, p in self._load().items()}

    def display_name(self, staff_id: str | None, fallback: str | None = None) -> str | None:
        if not staff_id:
            return fallback
        with self.lock:
            p = self._load().get(staff_id)
        return format_display_name(p) if p else fallback

    # ------------------------------------------------------------ запись

    @_committing
    def update(self, staff_id: str, body: "StaffProfileUpdate") -> dict:
        with self.lock:
            profiles = self._load()
            p = profiles.get(staff_id)
            if p is None:
                raise ApiError(404, "STAFF_NOT_FOUND", f"Сотрудник {staff_id} не найден в справочнике")
            changes = body.model_dump(exclude_unset=True)
            eq_start = parse_equipment(changes.pop("equipment_start", None), "equipment_start")
            eq_cap = parse_equipment(changes.pop("equipment_capacity", None), "equipment_capacity")
            in_planner = staff_id in _base_staff()
            for field in ("profile", "service_center_id"):
                if field in changes and in_planner:
                    current = self._view(p)[field]
                    if changes[field] is not None and changes[field] != current:
                        raise ApiError(422, "READ_ONLY_FIELD",
                                       "Профиль и СЦ сотрудника из справочника планировщика здесь не меняются",
                                       [{"field": field, "message": "только для чтения"}])
                    changes.pop(field)
            if "service_center_id" in changes:
                _check_sc(changes["service_center_id"])
            for field in ("profile", "service_center_id"):
                if field in changes and changes[field] is None:
                    raise ApiError(400, "BAD_REQUEST", f"Поле {field}: обязательно",
                                   [{"field": field, "message": "обязательно"}])
            if eq_start or eq_cap:
                self._set_equipment(staff_id, eq_start, eq_cap)  # 422 до любых изменений профиля
            updated = {**p, **changes}
            profiles[staff_id] = updated
            self._save()
            return self._view(updated)

    @_committing
    def create(self, body: "StaffProfileCreate") -> dict:
        with self.lock:
            profiles = self._load()
            _check_sc(body.service_center_id)
            used = {int(m.group(1)) for sid in (*profiles, *_base_staff()) if (m := _STAFF_ID.match(sid))}
            num = next((n for n in range(max(used, default=0) + 1, 1000)), None)
            if num is None:
                num = next((n for n in range(1, 1000) if n not in used), None)
            if num is None:
                raise ApiError(409, "STAFF_ID_EXHAUSTED", "Нет свободных табельных идентификаторов ST-001…ST-999")
            sid = f"ST-{num:03d}"
            data = body.model_dump()
            if not data.get("position"):
                data["position"] = POSITIONS.get(data["profile"], "Инженер")
            if not data.get("personnel_number"):
                data["personnel_number"] = sid
            record = {"staff_id": sid, **data}
            profiles[sid] = record
            self._save()
            return self._view(record)


def _check_sc(sc_id: str | None) -> None:
    known = _service_center_ids()
    if sc_id and known and sc_id not in known:
        raise ApiError(422, "SERVICE_CENTER_NOT_FOUND", f"Сервисный центр {sc_id} не найден",
                       [{"field": "service_center_id", "message": "нет в справочнике СЦ"}])


_directory: StaffDirectory | None = None
_directory_lock = threading.Lock()


def directory() -> StaffDirectory:
    """Общий справочник процесса (путь берётся при первом обращении)."""
    global _directory
    with _directory_lock:
        if _directory is None or _directory.path != _path():
            _directory = StaffDirectory(_path())
        return _directory


def display_name(staff_id: str | None, fallback: str | None = None) -> str | None:
    """«Фамилия И. О.» для UI-ответов; fallback, если справочник недоступен или id неизвестен."""
    try:
        return directory().display_name(staff_id, fallback)
    except Exception:  # справочник не должен ломать основные UI-ответы
        return fallback


# ---------------------------------------------------------------- модели запросов

def _norm_phone(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    if len(digits) != 10:
        raise ValueError("ожидается телефон вида +7 (9xx) xxx-xx-xx")
    return f"+7 ({digits[:3]}) {digits[3:6]}-{digits[6:8]}-{digits[8:]}"


def _norm_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(value.split())
    return value or None


class _ProfileFields(BaseModel):
    model_config = ConfigDict(extra="ignore")

    middle_name: Optional[str] = Field(default=None, max_length=60)
    phone: Optional[str] = None
    position: Optional[str] = Field(default=None, max_length=80)
    personnel_number: Optional[str] = Field(default=None, max_length=20)
    note: Optional[str] = Field(default=None, max_length=500)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v):
        return _norm_phone(v)

    @field_validator("middle_name", "position", "personnel_number", "note", mode="before")
    @classmethod
    def _text(cls, v):
        return _norm_text(v) if isinstance(v, str) else v


class StaffProfileUpdate(_ProfileFields):
    first_name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    last_name: Optional[str] = Field(default=None, min_length=1, max_length=60)
    profile: Optional[Literal["INSTALL", "LTP3"]] = None
    service_center_id: Optional[str] = Field(default=None, pattern=r"^SC-[0-9]{2}$")
    # Рюкзак АО (частично): проверяется в StaffDirectory.update → 422 с понятным текстом.
    equipment_start: Optional[Any] = None
    equipment_capacity: Optional[Any] = None

    @field_validator("first_name", "last_name", mode="before")
    @classmethod
    def _name(cls, v):
        return _norm_text(v) if isinstance(v, str) else v


class StaffProfileCreate(_ProfileFields):
    first_name: str = Field(min_length=1, max_length=60)
    last_name: str = Field(min_length=1, max_length=60)
    profile: Literal["INSTALL", "LTP3"]
    service_center_id: str = Field(pattern=r"^SC-[0-9]{2}$")

    @field_validator("first_name", "last_name", mode="before")
    @classmethod
    def _name(cls, v):
        return (_norm_text(v) or "") if isinstance(v, str) else v


# ---------------------------------------------------------------- routes

NEXT_REPLAN_MESSAGE = "Новый сотрудник попадёт в план при следующей пересборке дня"
EQUIPMENT_REPLAN_MESSAGE = "Рюкзак АО применится при следующей пересборке дня"

# Последний результат сценария «Рюкзак АО» (/ui/scenario/equipment): p90 расхода за смену
# без заездов на склад по профилю. Подсказка, а не расчёт: без сценария — None.
_recommended: dict | None = None


def remember_recommendation(date: str, recommended: list[dict]) -> None:
    global _recommended
    by_profile = {r["profile"]: {k: r[k]["p90"] for k in EQUIPMENT_KINDS if isinstance(r.get(k), dict)}
                  for r in recommended if r.get("profile")}
    _recommended = {"date": date, "percentile": 90, "by_profile": by_profile} if by_profile else None


class EquipmentBulk(BaseModel):
    model_config = ConfigDict(extra="ignore")

    start: Optional[Any] = None
    capacity: Optional[Any] = None
    profile: Optional[Literal["INSTALL", "LTP3", "ALL"]] = None


class EquipmentBulkReset(BaseModel):
    model_config = ConfigDict(extra="ignore")

    profile: Optional[Literal["INSTALL", "LTP3", "ALL"]] = None


def _profile_filter(value: str | None) -> str | None:
    return None if value in (None, "ALL") else value


@router.get("/staff")
def list_staff() -> dict:
    """Записи справочника, склеенные с данными справочника в Store. Справочников нет — пустой список и
    reference_loaded=false (записи при этом сохраняются и вернутся после загрузки справочников)."""
    d = directory()
    loaded = reference_loaded()
    items = d.list() if loaded else []
    return {"staff": items, "count": len(items), "reference_loaded": loaded,
            "not_in_reference": sum(1 for x in items if not x["in_reference"]),
            "persisted": d.persisted, "recommended_backpack": _recommended}


@router.post("/staff/equipment/bulk")
async def bulk_equipment(request: Request) -> dict:
    body: EquipmentBulk = _parse(EquipmentBulk.model_validate_json, (await request.body()) or b"{}")
    start = parse_equipment(body.start, "start")
    capacity = parse_equipment(body.capacity, "capacity")
    if not start and not capacity:
        raise ApiError(422, "INVALID_EQUIPMENT", "Укажите старт или максимум хотя бы по одному виду АО",
                       [{"field": "start", "message": "пусто"}])
    d = directory()
    n = d.bulk_equipment(start, capacity, _profile_filter(body.profile))
    return {"updated": n, "profile": body.profile or "ALL", "applies_from_next_replan": True,
            "message": EQUIPMENT_REPLAN_MESSAGE, "persisted": d.persisted}


@router.post("/staff/equipment/bulk/reset")
async def bulk_reset_equipment(request: Request) -> dict:
    body: EquipmentBulkReset = _parse(EquipmentBulkReset.model_validate_json, (await request.body()) or b"{}")
    d = directory()
    n = d.bulk_reset_equipment(_profile_filter(body.profile))
    return {"reset": n, "profile": body.profile or "ALL", "applies_from_next_replan": True,
            "message": EQUIPMENT_REPLAN_MESSAGE, "persisted": d.persisted}


@router.get("/staff/{staff_id}")
def get_staff(staff_id: str) -> dict:
    return directory().get(staff_id)


@router.put("/staff/{staff_id}")
async def put_staff(staff_id: str, request: Request) -> dict:
    body: StaffProfileUpdate = _parse(StaffProfileUpdate.model_validate_json, await request.body())
    d = directory()
    touches_equipment = bool({"equipment_start", "equipment_capacity"} & body.model_fields_set)
    item = d.update(staff_id, body)
    extra = {"applies_from_next_replan": True, "message": EQUIPMENT_REPLAN_MESSAGE} if touches_equipment else {}
    return {**item, **extra, "persisted": d.persisted}


@router.delete("/staff/{staff_id}/equipment-override")
def delete_equipment_override(staff_id: str) -> dict:
    d = directory()
    item = d.reset_equipment(staff_id)
    return {**item, "applies_from_next_replan": True, "message": EQUIPMENT_REPLAN_MESSAGE, "persisted": d.persisted}


@router.post("/staff", status_code=201)
async def create_staff(request: Request) -> JSONResponse:
    body: StaffProfileCreate = _parse(StaffProfileCreate.model_validate_json, await request.body())
    d = directory()
    item = d.create(body)
    content = {
        **item,
        "applies_from_next_replan": True,
        "message": NEXT_REPLAN_MESSAGE,
        "persisted": d.persisted,
    }
    from .auth_api import create_for_new_staff  # аккаунт новому сотруднику (при AUTH_ENABLED=on)
    account = create_for_new_staff(item)
    if account is not None:
        content["login"] = account["login"]
        content["initial_password"] = account["initial_password"]  # показывается один раз
    return JSONResponse(status_code=201, content=content)
