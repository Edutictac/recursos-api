import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_main(tmp_path, monkeypatch):
    monkeypatch.setenv("RECURSOS_DB", str(tmp_path / "recursos.db"))
    monkeypatch.setenv("RECURSOS_SECRET", "test-secret")
    monkeypatch.setenv("RECURSOS_COOKIE_SECURE", "0")
    monkeypatch.setenv("PLAY_INGEST_TOKEN", "play-test-token")
    monkeypatch.setenv("PLAY_ALLOWED_HOSTS", "play.example.test")
    import app.config
    import app.db

    importlib.reload(app.config)
    importlib.reload(app.db)
    import main

    return importlib.reload(main)


def request(token="play-test-token"):
    return SimpleNamespace(headers={"Authorization": f"Bearer {token}"})


def payload(version="v1"):
    return {
        "contract_version": "1",
        "external_id": "edutictac-play:Ab12Cd34",
        "version": version,
        "idempotency_key": f"edutictac-play:Ab12Cd34:{version}",
        "metadata": {
            "title": "Animales",
            "description": "Una actividad de ciencias.",
            "type": "multiple-choice",
            "language": "es",
            "subject": "Ciencias",
            "educational_stage": "Primaria",
            "tags": ["ciencias"],
            "license": "CC BY-SA",
            "author_pseudonym": "Aula 1",
        },
        "visibility": "public",
        "canonical_url": "https://play.example.test/play/Ab12Cd34",
        "download_url": "https://play.example.test/play/api/activities/Ab12Cd34/download",
        "package": {"format": "h5p", "delivery": "download_url"},
    }


def test_play_publish_is_idempotent_and_unpublishable(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    activity = main.PlayResourcePublishIn(**payload())

    first = main.publish_play_activity("Ab12Cd34", activity, request())
    duplicate = main.publish_play_activity("Ab12Cd34", activity, request())

    assert first["status"] == "published"
    assert duplicate["status"] == "published"
    assert duplicate["resource_id"] == "edutictac-play:Ab12Cd34"
    with main.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM resources WHERE provider = 'edutictac-play'").fetchone()["n"] == 1

    current = main.get_play_activity("Ab12Cd34", request())
    assert current["status"] == "published"
    unpublished = main.unpublish_play_activity("Ab12Cd34", request())
    assert unpublished["status"] == "unpublished"
    assert main.get_play_activity("Ab12Cd34", request())["status"] == "unpublished"


def test_play_publish_rejects_wrong_token_and_private_activity(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    activity_data = payload()
    activity_data["visibility"] = "private"
    activity = main.PlayResourcePublishIn(**activity_data)

    try:
        main.publish_play_activity("Ab12Cd34", activity, request("wrong"))
    except main.HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("wrong token was accepted")

    try:
        main.publish_play_activity("Ab12Cd34", activity, request())
    except main.HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("private activity was accepted")
