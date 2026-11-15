"""Эндпоинты авторизации: /ui/auth/* (открытые) и /ui/directory/accounts/* (только диспетчер)."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from . import auth
from .auth import Principal
from .errors import ApiError
from .service import _parse

router = APIRouter(prefix="/ui/auth", tags=["ui-auth"])
admin_router = APIRouter(prefix="/ui/directory/accounts", tags=["ui-auth"],
                         dependencies=[Depends(auth.require_dispatcher)])

BAD_CREDENTIALS = "Неверный логин или пароль"


class LoginIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    login: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class ChangePasswordIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


def _cookie_secure(request: Request) -> bool:
    if auth._env_on("AUTH_COOKIE_SECURE", False):
        return True
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def _set_cookie(response: Response, request: Request, token: str) -> None:
    response.set_cookie(auth.COOKIE_NAME, token, max_age=auth.token_ttl_sec(), httponly=True,
                        samesite="lax", secure=_cookie_secure(request), path="/")


def _me(p: Principal) -> dict:
    return {"login": p.login, "role": p.role, "staff_id": p.staff_id, "display_name": p.display_name,
            "must_change_password": p.must_change_password, "auth_enabled": auth.auth_enabled()}


def _session_response(request: Request, acct: dict) -> JSONResponse:
    token, exp = auth.issue_token(acct)
    body = {"token": token, "role": acct["role"], "staff_id": acct.get("staff_id"),
            "display_name": auth.display_name_for(acct), "must_change_password": bool(acct.get("must_change_password")),
            "login": acct["login"], "expires_in": max(0, exp - int(time.time()))}
    resp = JSONResponse(body)
    _set_cookie(resp, request, token)
    return resp


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


@router.post("/login", summary="Вход: токен в теле и httpOnly-cookie",
             description="Одинаковый ответ 401 для неизвестного логина и неверного пароля. "
                         "5 неудач подряд по паре логин+IP → 429 на 60 с.")
async def login(request: Request) -> JSONResponse:
    if not auth.auth_enabled():
        raise ApiError(409, "AUTH_DISABLED", "Авторизация отключена (AUTH_ENABLED=off)")
    body: LoginIn = _parse(LoginIn.model_validate_json, await request.body())
    login_, ip = body.login.strip(), _ip(request)
    auth.limiter.check(login_, ip)
    store = auth.accounts()
    acct = store.check_password(login_, body.password)
    if acct is None or acct.get("disabled"):
        auth.limiter.fail(login_, ip)
        raise ApiError(401, "UNAUTHORIZED", BAD_CREDENTIALS)
    auth.limiter.success(login_, ip)
    request.state.actor = acct["login"]  # актор в журнале действий (пароль и тело не пишутся)
    return _session_response(request, acct)


@router.post("/logout", summary="Выход: токен отзывается, cookie удаляется")
def logout(request: Request) -> JSONResponse:
    if auth.auth_enabled():
        try:
            auth.revoke(auth.current_principal(request))
        except ApiError:
            pass  # уже без сессии: просто чистим cookie
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@router.get("/me", summary="Текущий пользователь")
def me(principal: Principal = Depends(auth.current_principal)) -> dict:
    return _me(principal)


@router.post("/change-password", summary="Смена своего пароля (выдаёт новый токен)")
async def change_password(request: Request, principal: Principal = Depends(auth.current_principal)):
    if not auth.auth_enabled():
        raise ApiError(409, "AUTH_DISABLED", "Авторизация отключена (AUTH_ENABLED=off)")
    body: ChangePasswordIn = _parse(ChangePasswordIn.model_validate_json, await request.body())
    ip = _ip(request)
    auth.limiter.check(principal.login, ip)
    store = auth.accounts()
    if store.check_password(principal.login, body.old_password) is None:
        auth.limiter.fail(principal.login, ip)
        raise ApiError(401, "UNAUTHORIZED", "Неверный текущий пароль")
    if len(body.new_password) < auth.MIN_PASSWORD_LEN:
        raise ApiError(422, "WEAK_PASSWORD", f"Новый пароль короче {auth.MIN_PASSWORD_LEN} символов",
                       [{"field": "new_password", "message": f"минимум {auth.MIN_PASSWORD_LEN} символов"}])
    if body.new_password == body.old_password:
        raise ApiError(422, "WEAK_PASSWORD", "Новый пароль совпадает с текущим",
                       [{"field": "new_password", "message": "должен отличаться"}])
    auth.limiter.success(principal.login, ip)
    acct = store.change_password(principal.login, body.new_password, source="user", must_change=False)
    return _session_response(request, acct)


@router.get("/demo-hint", summary="Подсказка демо-входа (без авторизации; только при DEMO_ACCOUNTS=on)")
def demo_hint() -> dict:
    if not (auth.demo_enabled() and auth.auth_enabled()):
        return {"enabled": False}
    logins = [a["login"] for a in auth.accounts().list() if a["role"] == auth.EMPLOYEE
              and a.get("pw_source") == "demo" and not a.get("disabled")][:3]
    return {"enabled": True, "dispatcher_login": auth.DEMO_DISPATCHER_LOGIN, "employee_sample_logins": logins}


# ---------------------------------------------------------------- админ-действия диспетчера

def _public(a: dict) -> dict:
    return {"login": a["login"], "role": a["role"], "staff_id": a.get("staff_id"),
            "display_name": auth.display_name_for(a), "disabled": bool(a.get("disabled")),
            "must_change_password": bool(a.get("must_change_password")), "created_at": a.get("created_at")}


def _employee_account(staff_id: str) -> dict:
    store = auth.accounts()
    acct = store.by_staff(staff_id)
    if acct is None:
        store.bootstrap()  # справочник в Store мог пополниться, пока авторизация была выключена
        acct = store.by_staff(staff_id)
    if acct is None:
        raise ApiError(404, "ACCOUNT_NOT_FOUND", f"Аккаунт сотрудника {staff_id} не найден")
    return acct


@admin_router.get("", summary="Список аккаунтов (без хэшей)")
def list_accounts() -> dict:
    items = [_public(a) for a in auth.accounts().list()]
    return {"accounts": items, "count": len(items)}


@admin_router.post("/{staff_id}/reset-password",
                   summary="Новый временный пароль сотрудника (показывается один раз)")
def reset_password(staff_id: str) -> dict:
    acct = _employee_account(staff_id)
    temp = auth.random_password()
    new = auth.accounts().change_password(acct["login"], temp, source="reset", must_change=True)
    return {**_public(new), "temporary_password": temp,
            "message": "Пароль показан один раз; прежние сессии сотрудника завершены"}


@admin_router.post("/{staff_id}/disable", summary="Отключить аккаунт сотрудника")
def disable_account(staff_id: str) -> dict:
    acct = _employee_account(staff_id)
    return _public(auth.accounts().set_disabled(acct["login"], True))


@admin_router.post("/{staff_id}/enable", summary="Включить аккаунт сотрудника")
def enable_account(staff_id: str) -> dict:
    acct = _employee_account(staff_id)
    return _public(auth.accounts().set_disabled(acct["login"], False))


def create_for_new_staff(item: dict) -> dict | None:
    """Аккаунт новому сотруднику из POST /ui/directory/staff: {login, initial_password} или None (auth off)."""
    if not auth.auth_enabled():
        return None
    store = auth.accounts()
    store.bootstrap()  # гарантирует диспетчера и аккаунты сотрудников справочника (идемпотентно)
    acct = store.by_staff(item["staff_id"])
    demo = auth.demo_enabled()
    if acct is None:  # сотрудник добавлен вручную (его нет в справочнике Store)
        pw = auth.demo_password() if demo else auth.random_password()
        acct = store.create_employee(item["staff_id"], item.get("personnel_number"), pw,
                                     source="demo" if demo else "random")
        return {"login": acct["login"], "initial_password": pw}
    if acct.get("pw_source") in ("random", "demo"):
        pw = auth.demo_password() if demo else auth.random_password()
        if not demo:
            acct = store.change_password(acct["login"], pw, source="random", must_change=False)
        return {"login": acct["login"], "initial_password": pw}
    return None
