"""Мини-клиент для служебных скриптов (seed/wipe): HTTP без прокси, вход диспетчера в /ui при AUTH_ENABLED=on.

Только stdlib (urllib), чтобы скрипты работали и системным python. Сервер не импортируется.
Логин/пароль диспетчера: аргументы, env DISPATCHER_LOGIN / DISPATCHER_PASSWORD или ввод в консоли.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
import urllib.error
import urllib.request

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class HttpError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


class Client:
    def __init__(self, base_url: str, timeout: float = 120.0) -> None:
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.token: str | None = None

    def call(self, method: str, path: str, *, body: bytes | None = None, data=None) -> dict:
        if data is not None:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(self.base + path, data=body, method=method, headers=headers)
        try:
            with _OPENER.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise HttpError(exc.code, exc.read().decode("utf-8", "replace")) from None
        return json.loads(raw) if raw else {}

    def health(self) -> None:
        self.call("GET", "/api/v1/health")

    def login_dispatcher(self, login: str | None = None, password: str | None = None) -> None:
        """Вход диспетчера, если /ui требует авторизацию (иначе ничего не делает)."""
        try:
            me = self.call("GET", "/ui/auth/me")
            if me.get("auth_enabled") is False or me.get("role") == "DISPATCHER":
                return
        except HttpError as exc:
            if exc.status != 401:
                raise
        login = login or os.environ.get("DISPATCHER_LOGIN") or ""
        password = password or os.environ.get("DISPATCHER_PASSWORD") or ""
        if not login:
            login = input("Логин диспетчера: ").strip() if sys.stdin.isatty() else "dispatcher"
        if not password:
            if not sys.stdin.isatty():
                raise SystemExit("Нужен пароль диспетчера: задайте DISPATCHER_PASSWORD")
            password = getpass.getpass("Пароль диспетчера: ")
        res = self.call("POST", "/ui/auth/login", data={"login": login, "password": password})
        self.token = res["token"]
