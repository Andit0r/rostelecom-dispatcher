"""Фоновые «часы»: при включённой настройке CLOCK_FOLLOWS_WALL раз в минуту шлёт активному дню TIME_TICK."""

from __future__ import annotations

import datetime as dt
import json
import logging
import threading

from . import runtime_settings

log = logging.getLogger("rostelecom.clock")

EKB = dt.timezone(dt.timedelta(hours=5))  # Екатеринбург, UTC+5
CHECK_SEC = 30


def clock_tick_once(app, now: dt.datetime | None = None) -> str | None:
    """Одна проверка. Возвращает event_id отправленного тика или None (ничего не сделано)."""
    if not runtime_settings.get_bool("CLOCK_FOLLOWS_WALL"):
        return None
    from starlette.requests import Request

    from .ui_api import active_day

    now = (now or dt.datetime.now(EKB)).astimezone(EKB).replace(second=0, microsecond=0)
    day = active_day(Request({"type": "http", "app": app, "headers": []}))
    if day is None or day != now.date():
        return None
    svc = app.state.service
    with svc.store.lock:
        state = svc.store.days.get(day)
        if state is None or state.plan is None or now <= state.model_time:
            return None
    event_id = f"EV-CLK-{day.isoformat()}-{now:%H%M}"
    raw = json.dumps({"event_id": event_id, "type": "TIME_TICK", "timestamp": now.isoformat()}).encode()
    try:
        res = svc.post_event(day.isoformat(), raw)
    except Exception as exc:  # отклонено/409: не роняем поток
        log.debug("TIME_TICK %s не принят: %s", event_id, exc)
        return None
    return None if res.duplicate else event_id


def start(app) -> threading.Event:
    stop = threading.Event()

    def loop() -> None:
        while not stop.wait(CHECK_SEC):
            try:
                clock_tick_once(app)
            except Exception:
                log.debug("clock_tick_once: сбой", exc_info=True)

    threading.Thread(target=loop, name="plan-clock", daemon=True).start()
    return stop
