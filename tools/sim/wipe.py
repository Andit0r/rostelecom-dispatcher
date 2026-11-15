#!/usr/bin/env python3
"""Очистка сервера через POST /ui/day/reset: конкретный день, все дни или всё вместе со справочниками.

    python tools/sim/wipe.py --base-url http://127.0.0.1:8080 [--date YYYY-MM-DD | --all-days | --everything] [--yes]

Без режима — интерактивное меню: 1 — конкретный день (дата, по умолчанию активный день из /ui/plan/status),
2 — все дни (справочники остаются), 3 — всё, включая справочники. Перед удалением печатает, что будет удалено,
и спрашивает подтверждение Y/N (--yes пропускает вопрос).

При AUTH_ENABLED=on нужен вход диспетчера (/ui/auth/login, токен в заголовке): логин/пароль — аргументы,
env DISPATCHER_LOGIN / DISPATCHER_PASSWORD или ввод в консоли. Справочник сотрудников (ФИО, переопределения
рюкзаков) и аккаунты НЕ удаляются: после новой загрузки справочников записи снова привязываются по staff_id.
Архив в БД (days/plan_versions/events/alerts) остаётся. Скрипт ничего не пишет на диск.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from simlib.uiauth import Client, HttpError  # noqa: E402


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _confirm(text: str, yes: bool) -> bool:
    print(text)
    if yes:
        return True
    return _ask("Удалить? [Y/N]: ").lower() in ("y", "yes", "д", "да")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--date", help="очистить только этот день (YYYY-MM-DD)")
    mode.add_argument("--all-days", action="store_true", help="очистить все дни (справочники остаются)")
    mode.add_argument("--everything", action="store_true", help="очистить все дни и справочники")
    ap.add_argument("--login")
    ap.add_argument("--password")
    ap.add_argument("--yes", "-y", action="store_true", help="без подтверждения")
    args = ap.parse_args(argv)

    c = Client(args.base_url)
    try:
        c.health()
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Сервер {args.base_url} не отвечает: {exc}")
        return 1
    try:
        c.login_dispatcher(args.login, args.password)
        status = c.call("GET", "/ui/plan/status")
    except HttpError as exc:
        print(f"[!] Нет доступа к /ui: {exc}")
        return 2
    active = status.get("date") if status.get("loaded") else None

    choice = "1" if args.date else "2" if args.all_days else "3" if args.everything else None
    if choice is None:
        print(f"Сервер: {args.base_url}; активный день: {active or 'нет'}")
        print("  1 - Очистить конкретный день")
        print("  2 - Очистить все дни (справочники остаются)")
        print("  3 - Очистить всё, включая справочники")
        print("  0 - Выход")
        choice = _ask("Выбор: ")
    if choice not in ("1", "2", "3"):
        print("Отменено")
        return 0

    if choice == "1":
        day = args.date or _ask(f"Дата YYYY-MM-DD [{active or 'нет активного дня'}]: ") or active
        if not day:
            print("[!] Дата не указана, активного дня нет")
            return 2
        try:
            dt.date.fromisoformat(day)
        except ValueError:
            print(f"[!] Некорректная дата {day!r}, ожидается YYYY-MM-DD")
            return 2
        what = (f"Будет удалён день {day}: наряды, план, события и отметки этого дня (в памяти и снимок в БД). "
                "Другие дни, справочники, справочник сотрудников и аккаунты останутся.")
        path = f"/ui/day/reset?date={day}"
    elif choice == "2":
        what = ("Будут удалены ВСЕ дни: наряды, планы, события, отметки (в памяти и снимки в БД). "
                "Справочники, матрица, справочник сотрудников и аккаунты останутся.")
        path = "/ui/day/reset"
    else:
        what = ("Будут удалены ВСЕ дни и справочники с матрицей (нужен новый PUT reference или tools\\seed.bat). "
                "Справочник сотрудников с ФИО и аккаунты останутся.")
        path = "/ui/day/reset?with_reference=true"
    if not _confirm(what, args.yes):
        print("Отменено")
        return 0
    try:
        res = c.call("POST", path)
    except HttpError as exc:
        print(f"[!] Очистка не выполнена: {exc}")
        return 2
    print(f"[i] Очищено: дни {res.get('days')}; осталось дней: {res.get('remaining_days')}; "
          f"активный: {res.get('active_date') or 'нет'}; справочники загружены: {res.get('reference_loaded')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
