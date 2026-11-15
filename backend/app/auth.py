"""Аккаунты, токены и права для /ui (вне контракта /api/v1; /api/v1 остаётся открытым).

Только stdlib: пароли — hashlib.scrypt, токены — HMAC-SHA256 (`base64url(payload).base64url(sig)`).
Аккаунты — таблица accounts в PostgreSQL (если БД доступна при старте; запись синхронная с таймаутом,
кэш в памяти), иначе JSON-файл рядом со справочником (UI_DATA_DIR/auth_accounts.json, переопределяется
AUTH_ACCOUNTS_FILE), запись атомарная под блокировкой. При первом старте с пустой таблицей файлы
(аккаунты и auth_secret.key) один раз импортируются в БД, сами файлы не меняются. Секрет подписи —
env AUTH_SECRET, иначе settings[auth_secret] в БД или файл `auth_secret.key` рядом с аккаунтами
(токены переживают рестарт).

Роли: DISPATCHER (все /ui/*) и EMPLOYEE (только свой /ui/employee/{staff_id}/*).
AUTH_ENABLED=off отключает проверки (тесты, локальная отладка). Права проверяются при каждом
запросе по актуальному аккаунту (отключённый или сброшенный пароль → токен сразу недействителен).
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from fastapi import Request

from .errors import ApiError
from .storage import repos

log = logging.getLogger("rostelecom.auth")

DISPATCHER = "DISPATCHER"
EMPLOYEE = "EMPLOYEE"
COOKIE_NAME = "rt_session"
DEMO_DISPATCHER_LOGIN = "demo-dispatcher"
MIN_PASSWORD_LEN = 8
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
_TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без неоднозначных 0/O/1/l/I


def _env_on(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "on", "true", "yes")


def auth_enabled() -> bool:
    """AUTH_ENABLED=on|off, по умолчанию on. Читается при каждом вызове (удобно для тестов)."""
    return _env_on("AUTH_ENABLED", True)


def demo_enabled() -> bool:
    return _env_on("DEMO_ACCOUNTS", False)


def demo_password() -> str:
    return os.environ.get("DEMO_PASSWORD") or "demo"


def token_ttl_sec() -> int:
    try:
        hours = float(os.environ.get("AUTH_TOKEN_TTL_HOURS", "12"))
    except ValueError:
        hours = 12.0
    return int(max(0.01, hours) * 3600)


def random_password(length: int = 12) -> str:
    return "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(length))


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ---------------------------------------------------------------- принципал

@dataclass(frozen=True)
class Principal:
    login: str
    role: str
    staff_id: Optional[str] = None
    display_name: Optional[str] = None
    must_change_password: bool = False
    jti: Optional[str] = None
    exp: Optional[int] = None

    def author(self) -> dict:
        return {"login": self.login, "role": self.role, "staff_id": self.staff_id}


ANONYMOUS = Principal(login="anonymous", role=DISPATCHER, display_name="Без авторизации (AUTH_ENABLED=off)")


# ---------------------------------------------------------------- хранилище аккаунтов

def _accounts_path() -> Path:
    env = os.environ.get("AUTH_ACCOUNTS_FILE")
    if env:
        return Path(env)
    from .ui_api import DATA  # ui_api не импортирует auth на уровне модуля
    return DATA / "auth_accounts.json"


class AccountStore:
    """Аккаунты по логину. Все операции под self.lock; запись атомарная (tmp + os.replace)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.persisted = True
        self.backend = "file"  # "postgres", если при первом обращении БД была доступна
        self._pg: repos.PgRepo | None = None
        self._accounts: dict[str, dict] | None = None
        self._secret_cache: bytes | None = None

    # ---- хранилище
    @staticmethod
    def _valid(items) -> dict[str, dict]:
        return {a["login"]: a for a in items if isinstance(a, dict) and a.get("login") and a.get("role")}

    def _read_file(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return self._valid(raw.get("accounts", []) if isinstance(raw, dict) else [])
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError):
            return {}  # битый файл: стартуем с пустого, bootstrap создаст аккаунты заново

    def _load(self) -> dict[str, dict]:
        if self._accounts is not None:
            return self._accounts
        pg = repos.pg_repo("accounts")
        if pg is not None:
            self._pg, self.backend = pg, "postgres"
            accounts = self._valid(pg.all().values())
            if not accounts:  # пустая таблица: однократный импорт файла (идемпотентно, файл не меняется)
                from_file = self._read_file()
                if from_file:
                    repos.import_missing(pg, from_file, "аккаунты")
                    accounts = self._valid(pg.all().values())
        else:
            accounts = self._read_file()
        self._accounts = accounts
        return accounts

    def _save(self) -> None:
        """Под self.lock. БД: в очередь только изменившиеся записи (подтверждение — _commit() вне lock)."""
        assert self._accounts is not None
        if self._pg is not None:
            repos.sync_repo(self._pg, self._accounts)
            return
        data = {"version": 1, "accounts": [self._accounts[k] for k in sorted(self._accounts)]}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)
            self.persisted = True
        except OSError:
            self.persisted = False  # каталог только для чтения: аккаунты живут в памяти процесса

    def _commit(self) -> None:
        """Вне self.lock: дождаться записи в БД (до 3 с). БД недоступна — работаем по кэшу, запись догонит."""
        if self._pg is not None:
            self.persisted = repos.flush()

    # ---- секрет подписи
    def secret(self) -> bytes:
        env = os.environ.get("AUTH_SECRET")
        if env:
            return env.encode("utf-8")
        with self.lock:
            if self._secret_cache is not None:
                return self._secret_cache
            self._load()
            key_path = self.path.with_name("auth_secret.key")
            try:
                file_value = key_path.read_text(encoding="utf-8").strip()
            except OSError:
                file_value = ""
            settings = repos.pg_repo("settings") if self._pg is not None else None
            if settings is not None:
                rec = settings.get("auth_secret")
                value = str(rec.get("secret", "")) if isinstance(rec, dict) else ""
                if len(value) < 32:
                    imported = len(file_value) >= 32
                    value = file_value if imported else secrets.token_hex(32)
                    settings.put("auth_secret", {"secret": value})
                    repos.flush(10.0)
                    log.info("auth_secret: %s в БД", "импортировано из файла" if imported else "создано")
                    print(f"[storage] auth_secret: {'импортировано 1 из файла' if imported else 'создан новый'} в БД",
                          flush=True)
            else:
                value = file_value
                if len(value) < 32:
                    value = secrets.token_hex(32)
                    try:
                        key_path.parent.mkdir(parents=True, exist_ok=True)
                        tmp = key_path.with_name(key_path.name + ".tmp")
                        tmp.write_text(value, encoding="utf-8")
                        os.replace(tmp, key_path)
                    except OSError:
                        pass  # только память: токены не переживут рестарт
            self._secret_cache = value.encode("utf-8")
            return self._secret_cache

    # ---- создание/изменение
    def _new(self, login: str, role: str, password: str, *, staff_id: str | None, source: str,
             must_change: bool = False) -> dict:
        salt = os.urandom(16)
        return {
            "login": login, "role": role, "staff_id": staff_id,
            "password_hash": _hash(password, salt).hex(), "salt": salt.hex(),
            "disabled": False, "must_change_password": must_change,
            "created_at": _now_iso(), "token_epoch": 0, "pw_source": source,
        }

    def _set_password(self, acct: dict, password: str, *, source: str, must_change: bool) -> None:
        salt = os.urandom(16)
        acct["salt"] = salt.hex()
        acct["password_hash"] = _hash(password, salt).hex()
        acct["pw_source"] = source
        acct["must_change_password"] = must_change
        acct["token_epoch"] = int(acct.get("token_epoch", 0)) + 1  # старые токены недействительны

    def get(self, login: str) -> dict | None:
        with self.lock:
            a = self._load().get(login)
            return dict(a) if a else None

    def by_staff(self, staff_id: str) -> dict | None:
        with self.lock:
            return next((dict(a) for a in self._load().values() if a.get("staff_id") == staff_id), None)

    def list(self) -> list[dict]:
        with self.lock:
            return [dict(a) for _, a in sorted(self._load().items())]

    def check_password(self, login: str, password: str) -> dict | None:
        """Аккаунт, если пароль верен. Для несуществующего логина всё равно считает хэш (одинаковое время)."""
        with self.lock:
            a = self._load().get(login)
            a = dict(a) if a else None
        salt = bytes.fromhex(a["salt"]) if a else b"\x00" * 16
        digest = _hash(password, salt)
        ok = a is not None and hmac.compare_digest(digest, bytes.fromhex(a["password_hash"]))
        return a if ok else None

    def change_password(self, login: str, password: str, *, source: str, must_change: bool) -> dict:
        with self.lock:
            a = self._load().get(login)
            if a is None:
                raise ApiError(404, "ACCOUNT_NOT_FOUND", "Аккаунт не найден")
            self._set_password(a, password, source=source, must_change=must_change)
            self._save()
            out = dict(a)
        self._commit()
        return out

    def set_disabled(self, login: str, disabled: bool) -> dict:
        with self.lock:
            a = self._load().get(login)
            if a is None:
                raise ApiError(404, "ACCOUNT_NOT_FOUND", "Аккаунт не найден")
            a["disabled"] = disabled
            self._save()
            out = dict(a)
        self._commit()
        return out

    def create_employee(self, staff_id: str, personnel_number: str | None, password: str, *,
                        source: str = "random") -> dict:
        with self.lock:
            accounts = self._load()
            existing = next((a for a in accounts.values() if a.get("staff_id") == staff_id), None)
            if existing is not None:
                return dict(existing)
            login = (personnel_number or "").strip() or staff_id
            if login in accounts or login.lower() == DEMO_DISPATCHER_LOGIN or login == _dispatcher_login():
                login = staff_id
            acct = self._new(login, EMPLOYEE, password, staff_id=staff_id, source=source)
            accounts[login] = acct
            self._save()
            out = dict(acct)
        self._commit()
        return out

    # ---- сотрудники из справочника
    def _add_employees(self, accounts: dict[str, dict], staff_ids: list[str]) -> int:
        """Под self.lock: аккаунты для staff_id без аккаунта. Логин — табельный номер из справочника сотрудников
        (по умолчанию = staff_id), при занятости — staff_id. Пароль случайный (не показывается; выдача —
        «Сбросить пароль»), при DEMO_ACCOUNTS=on — демо. Существующие и отключённые не трогаются."""
        have = {a.get("staff_id") for a in accounts.values() if a.get("staff_id")}
        missing = [sid for sid in staff_ids if sid not in have]
        if not missing:
            return 0
        numbers: dict[str, str | None] = {}
        for sid in missing:
            try:
                from .staff_directory import directory
                numbers[sid] = directory().get(sid).get("personnel_number")
            except Exception:  # нет записи или справочник недоступен: логин = staff_id
                numbers[sid] = None
        demo = demo_enabled()
        created = 0
        for sid in missing:
            login = (numbers.get(sid) or "").strip() or sid
            if login in accounts or login.lower() == DEMO_DISPATCHER_LOGIN or login == _dispatcher_login():
                login = sid
            if login in accounts:
                continue
            pw, src = (demo_password(), "demo") if demo else (random_password(), "random")
            accounts[login] = self._new(login, EMPLOYEE, pw, staff_id=sid, source=src)
            created += 1
        if created and not demo:
            print(f"[auth] Созданы аккаунты сотрудников: {created} (логин — табельный номер; пароли случайные, "
                  "выдать: «Сбросить пароль» / POST /ui/directory/accounts/{staff_id}/reset-password)", flush=True)
        return created

    def sync_employees(self, staff_ids: list[str]) -> int:
        """После загрузки справочников: недостающие аккаунты сотрудников (только добавление)."""
        with self.lock:
            created = self._add_employees(self._load(), staff_ids)
            if created:
                self._save()
        if created:
            self._commit()
        return created

    # ---- bootstrap
    def bootstrap(self) -> None:
        """Диспетчер; аккаунты сотрудников — из справочника в Store, если он загружен (идемпотентно).
        Пароли — только в лог/ответ."""
        demo = demo_enabled()
        with self.lock:
            accounts = self._load()
            dirty = False
            # ---- диспетчер
            dlogin = DEMO_DISPATCHER_LOGIN if demo else _dispatcher_login()
            env_pw = os.environ.get("DISPATCHER_PASSWORD") or ""
            acct = accounts.get(dlogin)
            if acct is None:
                if demo:
                    pw, src = demo_password(), "demo"
                elif env_pw:
                    pw, src = env_pw, "env"
                else:
                    pw, src = random_password(14), "random"
                    print(f"[auth] Создан аккаунт диспетчера: логин «{dlogin}», пароль «{pw}» "
                          "(показан один раз; задайте DISPATCHER_PASSWORD, чтобы указать свой)", flush=True)
                accounts[dlogin] = self._new(dlogin, DISPATCHER, pw, staff_id=None, source=src)
                dirty = True
            else:
                want = demo_password() if demo else env_pw
                src = "demo" if demo else "env"
                if want and acct.get("pw_source") in ("demo", "env", "random") and \
                        not self._matches(acct, want):
                    self._set_password(acct, want, source=src, must_change=False)
                    dirty = True
                if acct.get("disabled") and acct.get("pw_source") == "demo" and demo:
                    acct["disabled"] = False
                    dirty = True
            if not demo:  # демо выключили: известные демо-пароли заменяем случайными
                for a in accounts.values():
                    if a.get("pw_source") == "demo":
                        self._set_password(a, random_password(16), source="random", must_change=False)
                        dirty = True
            # ---- демо: известные демо-пароли для всех имеющихся аккаунтов сотрудников
            if demo:
                for a in accounts.values():
                    if (a.get("role") == EMPLOYEE and a.get("pw_source") in ("random", "env")
                            and not self._matches(a, demo_password())):
                        self._set_password(a, demo_password(), source="demo", must_change=False)
                        dirty = True
            # ---- сотрудники из справочника в Store (если он уже загружен)
            from . import reference_sync
            ref = reference_sync.current()
            if ref is not None and self._add_employees(accounts, list(ref.staff)):
                dirty = True
            if dirty:
                self._save()
        self._commit()

    @staticmethod
    def _matches(acct: dict, password: str) -> bool:
        digest = _hash(password, bytes.fromhex(acct["salt"]))
        return hmac.compare_digest(digest, bytes.fromhex(acct["password_hash"]))


def _dispatcher_login() -> str:
    return (os.environ.get("DISPATCHER_LOGIN") or "dispatcher").strip() or "dispatcher"


_store: AccountStore | None = None
_store_lock = threading.Lock()


def accounts() -> AccountStore:
    """Общее хранилище процесса (путь берётся при первом обращении, как у справочника сотрудников)."""
    global _store
    with _store_lock:
        path = _accounts_path()
        if _store is None or _store.path != path:
            _store = AccountStore(path)
        return _store


def bootstrap_accounts() -> None:
    if auth_enabled():
        accounts().bootstrap()


def display_name_for(acct: dict) -> str | None:
    if acct["role"] == EMPLOYEE and acct.get("staff_id"):
        from .staff_directory import display_name
        return display_name(acct["staff_id"], acct["staff_id"])
    return "Диспетчер"


# ---------------------------------------------------------------- токены

_revoked: dict[str, int] = {}  # jti -> exp (отозванные при выходе, в памяти)
_revoked_lock = threading.Lock()


def issue_token(acct: dict) -> tuple[str, int]:
    exp = int(time.time()) + token_ttl_sec()
    payload = {"sub": acct["login"], "role": acct["role"], "sid": acct.get("staff_id"),
               "ep": int(acct.get("token_epoch", 0)), "jti": uuid.uuid4().hex, "iat": int(time.time()), "exp": exp}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64(hmac.new(accounts().secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}", exp


def _decode(token: str) -> dict | None:
    try:
        body, sig = token.split(".", 1)
        expect = hmac.new(accounts().secret(), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(expect, _unb64(sig)):
            return None
        payload = json.loads(_unb64(body))
        return payload if isinstance(payload, dict) else None
    except (ValueError, UnicodeError, TypeError):
        return None


def revoke(principal: Principal) -> None:
    if not principal.jti:
        return
    now = int(time.time())
    with _revoked_lock:
        for k in [k for k, e in _revoked.items() if e < now]:
            _revoked.pop(k, None)
        _revoked[principal.jti] = principal.exp or now


def _unauthorized(message: str = "Требуется авторизация") -> ApiError:
    return ApiError(401, "UNAUTHORIZED", message, headers={"WWW-Authenticate": "Bearer"})


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header[:7].lower() == "bearer " and header[7:].strip():
        return header[7:].strip()
    return request.cookies.get(COOKIE_NAME) or None


def authenticate(request: Request) -> Principal:
    """Принципал по токену (заголовок Bearer или cookie) с проверкой аккаунта при каждом запросе."""
    token = _extract_token(request)
    if not token:
        raise _unauthorized()
    payload = _decode(token)
    if payload is None or int(payload.get("exp", 0)) <= time.time():
        raise _unauthorized("Сессия недействительна или истекла: войдите заново")
    with _revoked_lock:
        if payload.get("jti") in _revoked:
            raise _unauthorized("Сессия завершена: войдите заново")
    acct = accounts().get(str(payload.get("sub")))
    if (acct is None or acct.get("disabled") or acct["role"] != payload.get("role")
            or int(acct.get("token_epoch", 0)) != int(payload.get("ep", -1))):
        raise _unauthorized("Сессия недействительна или аккаунт отключён: войдите заново")
    return Principal(login=acct["login"], role=acct["role"], staff_id=acct.get("staff_id"),
                     display_name=display_name_for(acct), must_change_password=bool(acct.get("must_change_password")),
                     jti=payload.get("jti"), exp=int(payload["exp"]))


# ---------------------------------------------------------------- защита от перебора

class LoginLimiter:
    """5 неудач по паре (логин, IP) → пауза 60 с. В памяти процесса."""

    MAX_FAILS = 5
    LOCK_SEC = 60
    WINDOW_SEC = 300  # давность неудач, после которой счётчик забывается

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[tuple[str, str], list[float]] = {}  # key -> [fails, last_fail, locked_until]

    @staticmethod
    def _key(login: str, ip: str) -> tuple[str, str]:
        return (login.strip().lower()[:128], ip)

    def check(self, login: str, ip: str) -> None:
        now = time.time()
        with self._lock:
            st = self._state.get(self._key(login, ip))
            if st and st[2] > now:
                wait = max(1, int(st[2] - now + 0.999))
                raise ApiError(429, "TOO_MANY_ATTEMPTS",
                               f"Слишком много неудачных попыток входа. Повторите через {wait} с",
                               headers={"Retry-After": str(wait)})

    def fail(self, login: str, ip: str) -> None:
        now = time.time()
        with self._lock:
            if len(self._state) > 10000:  # не растём бесконечно
                self._state = {k: v for k, v in self._state.items() if v[2] > now or now - v[1] < self.WINDOW_SEC}
            st = self._state.setdefault(self._key(login, ip), [0, now, 0.0])
            if now - st[1] > self.WINDOW_SEC and st[2] <= now:
                st[0] = 0
            st[0] += 1
            st[1] = now
            if st[0] >= self.MAX_FAILS:
                st[2] = now + self.LOCK_SEC
                st[0] = 0

    def success(self, login: str, ip: str) -> None:
        with self._lock:
            self._state.pop(self._key(login, ip), None)

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


limiter = LoginLimiter()


# ---------------------------------------------------------------- зависимости FastAPI

def current_principal(request: Request) -> Principal:
    """Принципал запроса (кэшируется в request.state); при AUTH_ENABLED=off — ANONYMOUS-диспетчер."""
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    principal = authenticate(request) if auth_enabled() else ANONYMOUS
    request.state.principal = principal
    return principal


def require_dispatcher(request: Request) -> Principal:
    p = current_principal(request)
    if p.role != DISPATCHER:
        raise ApiError(403, "FORBIDDEN", "Недостаточно прав: раздел доступен только диспетчеру")
    return p


def employee_scope(request: Request) -> Principal:
    """Для /ui/employee/*: диспетчер — любой маршрут; сотрудник — только свой staff_id из токена."""
    p = current_principal(request)
    if p.role == EMPLOYEE:
        sid = request.path_params.get("staff_id")
        if sid is not None and sid != p.staff_id:
            raise ApiError(403, "FORBIDDEN", "Недостаточно прав: доступен только свой маршрут")
    return p


def api_key_guard(request: Request) -> None:
    """Необязательная защита /api/v1 ключом (env API_KEY, заголовок X-API-Key); по умолчанию выключена.
    /api/v1/health открыт всегда (проверка живости контейнера)."""
    key = os.environ.get("API_KEY")
    if not key or request.url.path.rstrip("/").endswith("/health"):
        return
    got = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(got.encode("utf-8"), key.encode("utf-8")):
        raise ApiError(401, "UNAUTHORIZED", "Требуется заголовок X-API-Key", headers={"WWW-Authenticate": "ApiKey"})
