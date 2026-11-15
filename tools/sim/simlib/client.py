"""Клиент API с замером латентности каждого запроса.

Принимает любой объект с интерфейсом httpx.Client (httpx.Client для боевого сервера,
fastapi.testclient.TestClient для pytest-обёртки): сценарии не знают, что под ним.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

PREFIX = "/api/v1"


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


@dataclass
class Resp:
    status: int
    data: Any  # разобранный JSON или None
    text: str
    elapsed: float  # с, реальное время
    label: str
    content_type: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200

    def error_code(self) -> str | None:
        if isinstance(self.data, dict) and isinstance(self.data.get("error"), dict):
            return self.data["error"].get("code")
        return None


@dataclass
class ApiClient:
    http: Any
    prefix: str = PREFIX
    timeout: float = 330.0
    samples: dict[str, list[float]] = field(default_factory=dict)
    statuses: dict[str, dict[int, int]] = field(default_factory=dict)
    server_errors: list[str] = field(default_factory=list)  # 5xx и сетевые сбои
    lock: threading.Lock = field(default_factory=threading.Lock)

    def call(self, label: str, method: str, path: str, *, json_body: Any = None,
             content: bytes | str | None = None, timeout: float | None = None) -> Resp:
        kwargs: dict[str, Any] = {}
        if json_body is not None:
            kwargs["json"] = json_body
        elif content is not None:
            kwargs["content"] = content
            kwargs["headers"] = {"content-type": "application/json"}
        t0 = time.perf_counter()
        try:
            if not hasattr(self.http, "app"):  # у TestClient таймаут не задаётся
                kwargs["timeout"] = timeout or self.timeout
            r = self.http.request(method, self.prefix + path, **kwargs)
        except Exception as exc:  # noqa: BLE001 — сетевой сбой/таймаут тоже результат
            elapsed = time.perf_counter() - t0
            with self.lock:
                self.samples.setdefault(label, []).append(elapsed)
                self.server_errors.append(f"{method} {path}: {type(exc).__name__}: {str(exc)[:160]}")
            return Resp(0, None, f"{type(exc).__name__}: {exc}", elapsed, label)
        elapsed = time.perf_counter() - t0
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = None
        resp = Resp(r.status_code, data, r.text[:2000] if data is None else "", elapsed, label,
                    r.headers.get("content-type", ""))
        with self.lock:
            self.samples.setdefault(label, []).append(elapsed)
            st = self.statuses.setdefault(label, {})
            st[r.status_code] = st.get(r.status_code, 0) + 1
            if r.status_code >= 500:
                self.server_errors.append(f"{method} {path}: HTTP {r.status_code} {r.text[:160]}")
        return resp

    # ---- удобные обёртки -------------------------------------------------
    def health(self) -> Resp:
        return self.call("health", "GET", "/health")

    def put_reference(self, body: Any) -> Resp:
        return self.call("PUT reference", "PUT", "/reference", json_body=body)

    def put_matrix(self, raw: bytes) -> Resp:
        return self.call("PUT travel-matrix", "PUT", "/reference/travel-matrix", content=raw)

    def put_day(self, date: str, body: Any) -> Resp:
        return self.call("PUT day", "PUT", f"/days/{date}", json_body=body)

    def post_plan(self, date: str, time_limit: int | None = None) -> Resp:
        body = {"time_limit_sec": time_limit} if time_limit is not None else None
        return self.call("POST plan", "POST", f"/days/{date}/plan", json_body=body,
                         content=None if body else b"")

    def get_plan(self, date: str) -> Resp:
        return self.call("GET plan", "GET", f"/days/{date}/plan")

    def post_event(self, date: str, body: Any, label: str = "POST event") -> Resp:
        return self.call(label, "POST", f"/days/{date}/events", json_body=body)

    def post_raw_event(self, date: str, raw: str) -> Resp:
        return self.call("POST event (raw)", "POST", f"/days/{date}/events", content=raw)

    # ---- латентности -----------------------------------------------------
    def latency_table(self) -> dict[str, dict]:
        out = {}
        with self.lock:
            for label, v in sorted(self.samples.items()):
                out[label] = {"n": len(v), "p50": round(pct(v, 0.5), 3), "p95": round(pct(v, 0.95), 3),
                              "max": round(max(v), 3)}
        return out

    def snapshot_counts(self) -> dict[str, int]:
        with self.lock:
            return {k: len(v) for k, v in self.samples.items()}
