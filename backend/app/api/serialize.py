"""Сериализация ответов /api/v1 строго по api_spec.yaml.

Необязательное поле, которое в спецификации НЕ nullable, при значении None опускается (а не отдаётся
как null). Поля, помеченные в спецификации `nullable: true`, остаются null. Применяется только к
/api/v1: /ui использует собственные дампы моделей.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

# Поля, которые по спецификации nullable (null отдаётся как есть). Остальные None-поля опускаются.
NULLABLE_FIELDS: dict[str, frozenset[str]] = {
    "Plan": frozenset({"based_on_event_id"}),
    "Route": frozenset({"unavailable_from", "unavailable_until"}),
    "Stop": frozenset({"order_id"}),
    "UnassignedOrder": frozenset({"proposed_slot", "since"}),
    "CancelledOrder": frozenset({"event_id"}),
    "Alert": frozenset({
        "resolved_at", "order_id", "group_id", "staff_id", "forecast_start",
        "lateness_min", "suggested_staff_id", "proposed_slot",
    }),
}


def dump(value: Any) -> Any:
    """JSON-совместимое представление модели/списка/словаря с опущенными None у non-nullable полей."""
    if isinstance(value, BaseModel):
        nullable = NULLABLE_FIELDS.get(type(value).__name__, frozenset())
        out: dict[str, Any] = {}
        for name, info in type(value).model_fields.items():
            item = getattr(value, name)
            if item is None and name not in nullable:
                continue
            out[info.serialization_alias or info.alias or name] = dump(item)
        return out
    if isinstance(value, (list, tuple)):
        return [dump(v) for v in value]
    if isinstance(value, dict):
        return {k: dump(v) for k, v in value.items()}
    return to_jsonable_python(value)


def error_body(body: dict[str, Any]) -> dict[str, Any]:
    """ErrorResponse: details[].field не nullable, поэтому None опускается."""
    err = dict(body.get("error", {}))
    err["details"] = [{k: v for k, v in d.items() if v is not None} for d in err.get("details", [])]
    return {"error": err}
