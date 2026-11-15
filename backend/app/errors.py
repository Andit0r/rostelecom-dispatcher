"""Ошибки API в формате ErrorResponse."""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """Ошибка, которую видит клиент: HTTP-статус, машинный код, текст и детали."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or []
        self.headers = headers or {}

    def body(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }
