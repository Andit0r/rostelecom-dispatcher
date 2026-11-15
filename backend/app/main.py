"""Точка входа FastAPI: uvicorn app.main:app --port 8080."""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .api import router
from .api.serialize import error_body
from .errors import ApiError
from .journal import journal
from .planner import Planner, make_planner
from .service import MAX_BODY_BYTES, DispatchService
from .store import Store


_MUTATING = ("POST", "PUT", "PATCH", "DELETE")


def _record_action(request: Request, status: int) -> None:
    """Журнал действий диспетчера (таблица actions, write-behind). Тела запросов не пишутся (пароли)."""
    from .storage import db, state_repo

    if not db.enabled:
        return
    principal = getattr(request.state, "principal", None)
    route = request.scope.get("route")
    state_repo.record_action(
        getattr(principal, "login", None) or getattr(request.state, "actor", None),
        f"{request.method} {getattr(route, 'path', request.url.path)}",
        request.url.path,
        {"status": status, "query": dict(request.query_params) or None,
         "role": getattr(principal, "role", None)},
    )


def _preload_storage() -> None:
    """С БД загрузить справочник сотрудников и настройки при старте (импорт файлов, ожидание БД) — не в первом
    запросе. Без БД ничего не делает (всё лениво, как раньше)."""
    from .storage import db

    if not db.enabled:
        return
    try:
        from . import runtime_settings
        from .staff_directory import directory

        runtime_settings.source("TRAFFIC_FACTORS")
        directory().list()
    except Exception:  # хранилище не должно ломать запуск
        import logging
        logging.getLogger("rostelecom.storage").exception("Предзагрузка справочника/настроек не удалась")


def _error_content(request: Request, exc: ApiError) -> dict:
    """/api/v1 — строго по контракту (без null в details[].field); /ui — как раньше."""
    body = exc.body()
    return error_body(body) if request.url.path.startswith("/api/") else body


def create_app(planner: Planner | None = None, store: Store | None = None) -> FastAPI:
    app = FastAPI(
        title="Умная диспетчеризация выездных бригад — API решения",
        version="0.1.0",
    )
    app.state.service = DispatchService(store or Store(), planner or make_planner())
    from . import reference_sync
    reference_sync.bind(app.state.service.store)  # /ui-справочники берут данные из Store этого приложения
    from .auth import api_key_guard, bootstrap_accounts  # noqa: E402
    app.include_router(router, dependencies=[Depends(api_key_guard)])  # API_KEY: по умолчанию выключено
    bootstrap_accounts()  # аккаунты /ui (при AUTH_ENABLED=on); /api/v1 остаётся открытым
    _preload_storage()
    journal.start()  # фоновая запись истории в PostgreSQL; без DATABASE_URL ничего не делает
    from .ui_api import install_ui; install_ui(app)  # noqa: E702 — /ui для интерфейса диспетчера
    from .storage.restore import restore
    app.state.restore_report = restore(app)  # состояние из PostgreSQL после рестарта; без БД — пустой старт

    from . import clock
    clock_stop = clock.start(app)  # daemon-поток «время плана по часам»; без настройки ничего не делает
    inner_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a):
        try:
            async with inner_lifespan(a):
                yield
        finally:
            clock_stop.set()

    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > MAX_BODY_BYTES:
            return JSONResponse(
                status_code=400,
                content=_error_content(request, ApiError(400, "BAD_REQUEST", "Тело запроса больше 100 МБ")),
            )
        response = await call_next(request)
        path = request.url.path
        if path.startswith("/ui/") and (request.method in _MUTATING or path == "/ui/demo/load"):
            _record_action(request, response.status_code)
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content=_error_content(request, exc), headers=exc.headers or None)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content=_error_content(request, ApiError(500, "INTERNAL_ERROR", f"Внутренняя ошибка: {exc.__class__.__name__}")),
        )

    return app


app = create_app()
