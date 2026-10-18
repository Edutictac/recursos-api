"""Tests de ingesta del catálogo federado (canal resources de Commons)."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("RECURSOS_DB", str(Path(tempfile.mkdtemp()) / "import.db"))

from app.catalog_import import import_catalog  # noqa: E402
from app.db import get_conn, init_index_schema  # noqa: E402
import main  # noqa: E402


class FakeRequest:
    def __init__(self, headers, body):
        self.headers = headers
        self._body = body

    async def json(self):
        return self._body


def _item(external_id, title, **extra):
    base = {
        "provider": "commons",
        "external_id": external_id,
        "title": title,
        "language": ["ca"],
        "license": "CC BY-SA 4.0",
        "format": "h5p",
    }
    base.update(extra)
    return base


def _setup_db(monkeypatch, tmp_path):
    monkeypatch.setattr("app.db.config.DB_PATH", str(tmp_path / "t.db"))
    init_index_schema()


def test_import_catalog_creates_updates_and_deactivates(monkeypatch, tmp_path):
    _setup_db(monkeypatch, tmp_path)

    first = import_catalog({"items": [_item("a", "A"), _item("b", "B")]})
    assert first["created"] == 2
    assert first["providers"] == 1
    assert first["errors"] == 0

    second = import_catalog({
        "items": [_item("a", "A"), _item("b", "B cambiado"), _item("c", "C")]
    })
    assert second["created"] == 1
    assert second["updated"] == 1
    assert second["unchanged"] == 1

    third = import_catalog({"items": [_item("a", "A")]})
    assert third["unchanged"] == 1
    assert third["deactivated"] == 2

    with get_conn() as conn:
        rows = {
            r["external_id"]: r["active"]
            for r in conn.execute("SELECT external_id, active FROM resources")
        }
    assert rows == {"a": 1, "b": 0, "c": 0}


def test_import_catalog_counts_invalid_items(monkeypatch, tmp_path):
    _setup_db(monkeypatch, tmp_path)

    result = import_catalog({"items": [
        _item("ok", "Válido"),
        "no soy un objeto",
        {"provider": "commons", "title": "sin external_id"},
    ]})
    assert result["created"] == 1
    assert result["errors"] == 2


def test_import_catalog_requires_items_array():
    with pytest.raises(ValueError):
        import_catalog({"nope": []})
    with pytest.raises(ValueError):
        import_catalog(None)


def test_endpoint_requires_token(monkeypatch, tmp_path):
    _setup_db(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "RESOURCES_IMPORT_TOKEN", "secreto")

    request = FakeRequest({"Authorization": "Bearer incorrecto"}, {"catalog": {"items": []}})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.import_catalog_endpoint(request))
    assert exc.value.status_code == 401


def test_endpoint_imports_with_valid_token(monkeypatch, tmp_path):
    _setup_db(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "RESOURCES_IMPORT_TOKEN", "secreto")

    request = FakeRequest(
        {"Authorization": "Bearer secreto"},
        {"catalog": {"items": [_item("a", "A")]}},
    )
    result = asyncio.run(main.import_catalog_endpoint(request))
    assert result["created"] == 1

    bad = FakeRequest({"Authorization": "Bearer secreto"}, {"catalog": {"items": [1]}})
    result = asyncio.run(main.import_catalog_endpoint(bad))
    assert result["errors"] == 1
