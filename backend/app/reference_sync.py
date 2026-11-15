"""Справочники из Store для /ui: синхронизация справочника сотрудников и аккаунтов при загрузке справочников.

Store (in-memory состояние сервиса) хранит справочники, пришедшие в PUT /api/v1/reference (или подгруженные
«Создать день» из каталога). Модули /ui (staff_directory, auth) не имеют доступа к app, поэтому create_app
привязывает Store процесса здесь (`bind`), а Store вызывает `on_reference_loaded` после каждой загрузки
справочников (PUT reference, «Пересобрать», восстановление из БД).

«Исходный» справочник (`base_reference`) — последний пришедший извне (PUT reference / каталог / восстановление),
без переопределений рюкзаков диспетчера: от него считаются значения «по данным» и применяются переопределения
при «Пересобрать». Пересборка сама кладёт в Store справочник с переопределениями (derived=True) и исходный
не заменяет.
"""

from __future__ import annotations

import contextlib
import logging
import threading

log = logging.getLogger("rostelecom.reference")

_store = None
_base = None  # ReferenceState последней внешней загрузки
_local = threading.local()


def bind(store) -> None:
    global _store, _base
    _store = store
    _base = store.reference
    store.reference_listeners.append(on_reference_loaded)


def current():
    """Текущий справочник в Store (с переопределениями рюкзаков, если день пересобирался) или None."""
    return _store.reference if _store is not None else None


def base_reference():
    """Исходный справочник (как пришёл) или None, если справочников в Store нет."""
    ref = current()
    if ref is None:
        return None
    return _base if _base is not None and set(_base.staff) == set(ref.staff) else ref


@contextlib.contextmanager
def names_hint(names: dict[str, dict]):
    """ФИО-заготовка для новых записей справочника сотрудников на время загрузки каталога (служебный путь)."""
    _local.names = names
    try:
        yield
    finally:
        _local.names = None


def _hint() -> dict[str, dict]:
    return getattr(_local, "names", None) or {}


def on_reference_loaded(ref, derived: bool = False) -> None:
    """Новые записи справочника сотрудников и аккаунты для staff_id справочника (существующие не трогаются)."""
    global _base
    if not derived:
        _base = ref
    try:
        from .staff_directory import directory
        directory().sync(ref, _hint())
    except Exception:  # справочник сотрудников не должен ломать загрузку справочников
        log.exception("Синхронизация справочника сотрудников не удалась")
    try:
        from . import auth
        if auth.auth_enabled():
            auth.accounts().sync_employees(list(ref.staff))
    except Exception:
        log.exception("Синхронизация аккаунтов сотрудников не удалась")
