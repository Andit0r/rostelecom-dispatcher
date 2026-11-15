#!/usr/bin/env python3
"""Загрузка ТОЛЬКО справочников из каталога data/synth через /api/v1 (без нарядов и дней).

    python tools/sim/seed.py --base-url http://127.0.0.1:8080 [--no-names] [--login L --password P]

1. PUT /api/v1/reference (service_centers, staff, access_nodes, work_types — как в каталоге);
2. PUT /api/v1/reference/travel-matrix;
3. (по умолчанию) ФИО-заготовка data/synth/staff_names.json для записей справочника сотрудников, у которых
   ФИО ещё пустое: PUT /ui/directory/staff/{id} (фамилия, имя, отчество, телефон, должность; табельный номер
   и логин не меняются). Записи с уже введёнными ФИО не трогаются. При AUTH_ENABLED=on нужен вход диспетчера.

После этого в интерфейсе можно нажать «Создать день» и добавлять наряды. Сервер не импортируется и не меняется.
Внимание: PUT reference по контракту удаляет все дни, планы и события на сервере.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from simlib.data import DATA, SynthData  # noqa: E402
from simlib.uiauth import Client, HttpError  # noqa: E402

NAME_FIELDS = ("last_name", "first_name", "middle_name", "phone", "position")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--no-names", action="store_true", help="не заполнять ФИО из staff_names.json")
    ap.add_argument("--login", help="логин диспетчера (иначе env DISPATCHER_LOGIN или ввод)")
    ap.add_argument("--password", help="пароль диспетчера (иначе env DISPATCHER_PASSWORD или ввод)")
    args = ap.parse_args(argv)

    c = Client(args.base_url)
    try:
        c.health()
    except Exception as exc:  # noqa: BLE001
        print(f"[!] Сервер {args.base_url} не отвечает: {exc}")
        return 1

    data = SynthData()
    body = json.dumps(data.reference, ensure_ascii=False).encode("utf-8")
    res = c.call("PUT", "/api/v1/reference", body=body)
    print(f"[i] PUT reference: {res.get('status')} {res.get('counts')}")
    res = c.call("PUT", "/api/v1/reference/travel-matrix", body=data.matrix_raw)
    print(f"[i] PUT travel-matrix: {res.get('status')} {res.get('counts')}")

    if args.no_names:
        return 0
    try:
        names = json.loads((DATA / "staff_names.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print("[i] staff_names.json нет: записи справочника сотрудников остаются «Сотрудник ST-xxx»")
        return 0
    try:
        c.login_dispatcher(args.login, args.password)
        staff = c.call("GET", "/ui/directory/staff").get("staff", [])
    except HttpError as exc:
        print(f"[!] Справочник сотрудников недоступен ({exc}); ФИО не заполнены, справочники загружены")
        return 2
    filled = 0
    for item in staff:
        hint = names.get(item["staff_id"])
        if not hint or not item.get("name_empty", not item.get("last_name")):
            continue
        patch = {k: hint[k] for k in NAME_FIELDS if hint.get(k)}
        if not patch.get("last_name") or not patch.get("first_name"):
            continue
        try:
            c.call("PUT", f"/ui/directory/staff/{item['staff_id']}", data=patch)
            filled += 1
        except HttpError as exc:
            print(f"[!] {item['staff_id']}: {exc}")
    print(f"[i] ФИО из staff_names.json заполнены: {filled} (записи с введёнными ФИО не трогались)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
