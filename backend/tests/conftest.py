"""Общие настройки тестов: авторизация /ui по умолчанию выключена (тесты, где она нужна, включают её сами)."""

import os

os.environ["AUTH_ENABLED"] = "off"


def load_catalog_reference(client) -> None:
    """Справочники и матрица из служебного каталога через /api/v1 (как «Создать день» без справочников)."""
    from app import catalog

    r = client.put("/api/v1/reference", content=catalog.reference_bytes(), headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    r = client.put("/api/v1/reference/travel-matrix", content=catalog.matrix_bytes(),
                   headers={"Content-Type": "application/json"})
    assert r.status_code == 200, r.text
