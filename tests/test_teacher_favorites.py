import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load_main(tmp_path, monkeypatch):
    monkeypatch.setenv("RECURSOS_DB", str(tmp_path / "recursos.db"))
    monkeypatch.setenv("RECURSOS_SECRET", "test-secret")
    monkeypatch.setenv("RECURSOS_COOKIE_SECURE", "0")
    import app.config
    import app.db

    importlib.reload(app.config)
    importlib.reload(app.db)
    import main

    return importlib.reload(main)


def test_teacher_favorites_resolve_to_indexed_resources(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    teacher_id = "oidc:teacher-sub"
    with main.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO resources (provider, external_id, title, source_url, active)
            VALUES (?, ?, ?, ?, 1)
            """,
            ("edutictac-play", "edutictac-play:activity-1", "Repaso", "https://play.example/activity-1"),
        )
        conn.execute(
            "INSERT INTO favorites (user_id, game_key, created_at) VALUES (?, ?, ?)",
            (teacher_id, "https://play.example/activity-1", "2026-09-17T10:00:00+00:00"),
        )

    request = SimpleNamespace(
        cookies={main.SESSION_COOKIE: main.make_session(teacher_id)},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    result = main.list_teacher_favorites(request)

    assert result["total"] == 1
    assert result["items"][0]["title"] == "Repaso"
    assert result["items"][0]["external_id"] == "edutictac-play:activity-1"


def test_teacher_favorites_require_oidc_session(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    request = SimpleNamespace(cookies={}, client=SimpleNamespace(host="127.0.0.1"))

    try:
        main.list_teacher_favorites(request)
    except main.HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("anonymous access was accepted")


def test_teacher_favorites_include_dynamic_eduhoot_resources(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    from app.models import Resource

    teacher_id = "oidc:teacher-sub"
    game_url = "https://eduhoot.example/solo/?id=42"
    with main.get_conn() as conn:
        conn.execute(
            "INSERT INTO favorites (user_id, game_key, created_at) VALUES (?, ?, ?)",
            (teacher_id, game_url, "2026-09-17T11:00:00+00:00"),
        )
    monkeypatch.setattr(
        main,
        "_load_eduhoot_resources",
        lambda sub: [Resource(provider="eduhoot", external_id="42", title="Quiz privado", source_url=game_url, play_url=game_url, format="eduhoot")],
    )

    request = SimpleNamespace(
        cookies={main.SESSION_COOKIE: main.make_session(teacher_id)},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    result = main.list_teacher_favorites(request)

    assert result["total"] == 1
    assert result["items"][0]["title"] == "Quiz privado"
