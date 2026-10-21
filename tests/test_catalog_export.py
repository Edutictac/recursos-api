"""Catalogue export uses the current index and requires server authentication."""
import json

import pytest
from fastapi.testclient import TestClient

from test_teacher_favorites import load_main


def test_export_current_index_preserves_tags_without_legacy_input(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    main.import_catalog({"items": [{
        "provider": "commons", "external_id": "story", "title": "Un conte",
        "format": "h5p", "tags": ["lectura", "Valencià"],
        "thumbnail_url": "/images/story.png",
    }]})
    output = tmp_path / "catalogue"
    result = main.export_catalog(out_dir=str(output))
    games = json.loads((output / "games.json").read_text())
    home = json.loads((output / "games-home.json").read_text())
    assert result == {"total": 1, "home": 1, "by_provider": {"commons": 1}}
    assert len(games) == 1 and games[0]["id"] == "story"
    assert games[0]["tags"] == ["lectura", "Valencià"]
    assert home == games


@pytest.mark.parametrize("authorization", [None, "Bearer incorrect"])
def test_export_endpoint_rejects_unauthorized_requests_before_writing(tmp_path, monkeypatch, authorization):
    main = load_main(tmp_path, monkeypatch)
    output = tmp_path / "catalogue"
    monkeypatch.setattr(main, "RESOURCES_IMPORT_TOKEN", "synthetic-export-token")
    monkeypatch.setattr(main, "EXPORT_CATALOG_DIR", str(output))
    headers = {"Authorization": authorization} if authorization else {}
    with TestClient(main.app) as client:
        assert client.post("/api/internal/export-catalog", headers=headers).status_code == 401
    assert not output.exists()


def test_export_endpoint_requires_configuration_then_writes_current_index(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    monkeypatch.setattr(main, "RESOURCES_IMPORT_TOKEN", "synthetic-export-token")
    monkeypatch.setattr(main, "EXPORT_CATALOG_DIR", "")
    headers = {"Authorization": "Bearer synthetic-export-token"}
    with TestClient(main.app) as client:
        assert client.post("/api/internal/export-catalog", headers=headers).status_code == 503
        output = tmp_path / "catalogue"
        monkeypatch.setattr(main, "EXPORT_CATALOG_DIR", str(output))
        response = client.post("/api/internal/export-catalog", headers=headers)
        assert response.status_code == 200 and response.json()["total"] == 0
    assert json.loads((output / "games.json").read_text()) == []
    assert json.loads((output / "games-home.json").read_text()) == []
