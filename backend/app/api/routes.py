"""HTTP-маршруты /api/v1 по спецификации. Тела читаются сырыми и разбираются в сервисе,
чтобы любая ошибка JSON или схемы превращалась в ErrorResponse 400."""

from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ..models import Health
from .serialize import dump

API_VERSION = "0.1.0"

router = APIRouter(prefix="/api/v1")


def _service(request: Request):
    return request.app.state.service


def _json(model: BaseModel, status: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status, content=dump(model))


@router.get("/health", tags=["service"])
def health() -> JSONResponse:
    return _json(
        Health(
            status="ok",
            team=os.environ.get("TEAM_NAME", "непалиндром"),
            version=API_VERSION,
        )
    )


@router.put("/reference", tags=["reference"])
async def put_reference(request: Request) -> JSONResponse:
    raw = await request.body()
    result = await run_in_threadpool(_service(request).put_reference, raw)
    return _json(result)


@router.put("/reference/travel-matrix", tags=["reference"])
async def put_travel_matrix(request: Request) -> JSONResponse:
    raw = await request.body()
    result = await run_in_threadpool(_service(request).put_travel_matrix, raw)
    return _json(result)


@router.put("/days/{date}", tags=["day"])
async def put_day(date: str, request: Request) -> JSONResponse:
    raw = await request.body()
    result = await run_in_threadpool(_service(request).put_day, date, raw)
    return _json(result)


@router.post("/days/{date}/plan", tags=["plan"])
async def build_plan(date: str, request: Request) -> JSONResponse:
    raw = await request.body()
    plan = await run_in_threadpool(_service(request).build_plan, date, raw or None)
    return _json(plan)


@router.get("/days/{date}/plan", tags=["plan"])
async def get_plan(date: str, request: Request) -> JSONResponse:
    plan = await run_in_threadpool(_service(request).get_plan, date)
    return _json(plan)


@router.post("/days/{date}/events", tags=["events"])
async def post_event(date: str, request: Request) -> JSONResponse:
    raw = await request.body()
    result = await run_in_threadpool(_service(request).post_event, date, raw)
    return _json(result)
