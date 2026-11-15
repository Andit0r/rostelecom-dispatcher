"""Потокобезопасное хранилище состояния (в памяти процесса)."""

from __future__ import annotations

import threading
import datetime as dt

from .state import DayState, MatrixState, ReferenceState


class Store:
    def __init__(self) -> None:
        # Все операции над состоянием выполняются под этой блокировкой,
        # поэтому запросы выполняются последовательно и согласованно.
        self.lock = threading.RLock()
        self.reference: ReferenceState | None = None
        self.matrix: MatrixState | None = None
        self.days: dict[dt.date, DayState] = {}
        # поколение справочников (новое при каждом PUT reference): дни прежнего поколения не восстанавливаются
        self.ref_gen: str | None = None
        # вызываются после загрузки справочников (вне контракта: синхронизация справочника сотрудников /ui)
        self.reference_listeners: list = []

    def notify_reference(self, ref, derived: bool = False) -> None:
        """Сообщить слушателям о загруженных справочниках; ошибки слушателей не ломают загрузку."""
        for listener in list(self.reference_listeners):
            try:
                listener(ref, derived)
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger("rostelecom.reference").exception("Слушатель справочников упал")
