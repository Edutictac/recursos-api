"""Sessió central d'EduTicTac: el Banc reconeix Commons sense validar-ne les galetes."""
import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEACHER_SUB = 'hash-sub-laia'


class FakeResolve:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


def load(tmp_path, monkeypatch, central='1'):
    monkeypatch.setenv('RECURSOS_DB', str(tmp_path / 'recursos.db'))
    monkeypatch.setenv('RECURSOS_SECRET', 'test-secret')
    monkeypatch.setenv('RECURSOS_COOKIE_SECURE', '0')
    monkeypatch.setenv('RECURSOS_SEED', '0')
    monkeypatch.setenv('RECURSOS_CENTRAL_SESSION', central)
    monkeypatch.setenv('EDUTICTAC_ID_API_URL', 'http://id-api.internal')
    monkeypatch.setenv('EDUTICTAC_ID_APP_TOKEN', 'synthetic-app-token')
    monkeypatch.setenv('OIDC_ADMIN_SUBS', 'hash-sub-admin')
    import app.config
    import app.db
    import app.oidc
    importlib.reload(app.config)
    importlib.reload(app.db)
    importlib.reload(app.oidc)
    import main
    importlib.reload(main)
    main.init_db()
    return main


@pytest.fixture
def banc(tmp_path, monkeypatch):
    main = load(tmp_path, monkeypatch)
    calls = []
    identities = {
        'teacher-cookie': {'kind': 'oidc', 'sub': TEACHER_SUB, 'role': 'profesor', 'display_name': 'Laia'},
        'admin-cookie': {'kind': 'oidc', 'sub': 'other', 'role': 'administrador', 'display_name': 'Dir'},
        'subadmin-cookie': {'kind': 'oidc', 'sub': 'hash-sub-admin', 'role': 'profesor', 'display_name': 'A'},
        'pupil-cookie': {'kind': 'student', 'id': 'ident-1', 'public_code': 'k7m'},
    }

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers))
        assert url == 'http://id-api.internal/api/internal/session/resolve'
        assert headers['Authorization'] == 'Bearer synthetic-app-token'
        value = headers['Cookie'].split('=', 1)[1].split(';')[0]
        return FakeResolve(identities.get(value, {'kind': 'anonymous'}))

    monkeypatch.setattr(main.httpx, 'get', fake_get)
    main._central_cache.clear()
    return main, calls


def client(main, **cookies):
    c = TestClient(main.app)
    for name, value in cookies.items():
        c.cookies.set(name, value)
    return c


def test_teacher_central_session_is_the_same_uid_as_before(banc):
    main, calls = banc
    with main.get_conn() as db:
        db.execute("INSERT INTO favorites VALUES (?, 'game-a', '2026')", (f'oidc:{TEACHER_SUB}',))
    me = client(main, edutictac_oidc='teacher-cookie').get('/api/auth/me').json()
    assert me['logged_in'] and me['sub'] == TEACHER_SUB and not me['admin'] and me['central']
    favorites = client(main, edutictac_oidc='teacher-cookie').get('/api/preferences').json()
    assert 'game-a' in str(favorites)


def test_admin_by_commons_role_or_configured_sub(banc):
    main, _ = banc
    assert client(main, edutictac_oidc='admin-cookie').get('/api/auth/me').json()['admin']
    assert client(main, edutictac_oidc='subadmin-cookie').get('/api/auth/me').json()['admin']


def test_pupil_central_session(banc):
    main, _ = banc
    me = client(main, edutictac_id='pupil-cookie').get('/api/auth/me').json()
    assert me['student_logged_in'] and me['student_code'] == 'K7M' and not me['logged_in']
    assert client(main).post('/api/student/login', json={'public_code': 'K7M', 'pin': '1234'}).status_code == 409


def test_old_local_teacher_session_no_longer_counts(banc):
    main, _ = banc
    old = main.make_session(f'oidc:{TEACHER_SUB}', admin=True)
    c = client(main, recursos_session=old)
    response = c.get('/api/auth/me')
    assert not response.json()['logged_in'] and not response.json()['admin']
    assert 'recursos_session=""' in response.headers.get('set-cookie', '') or 'Max-Age=0' in response.headers.get('set-cookie', '')


def test_forged_or_unknown_central_cookie_is_anonymous(banc):
    main, _ = banc
    assert not client(main, edutictac_oidc='forged').get('/api/auth/me').json()['logged_in']


def test_anonymous_data_moves_to_the_account_once_and_keeps_summaries(banc):
    main, _ = banc
    anon = 'a' * 32
    teacher = f'oidc:{TEACHER_SUB}'
    with main.get_conn() as db:
        db.executemany("INSERT INTO favorites VALUES (?, ?, '2026')", [(anon, 'g1'), (anon, 'g2'), (teacher, 'g2')])
        db.executemany("INSERT INTO ratings VALUES (?, ?, ?, '2026')", [(anon, 'g1', 4), (anon, 'g2', 2), (teacher, 'g2', 5)])
        db.execute("INSERT INTO rating_summary VALUES ('g1', 4, 1, 4.0, '2026')")
        db.execute("INSERT INTO rating_summary VALUES ('g2', 7, 2, 3.5, '2026')")
        db.executemany("INSERT INTO reports VALUES (?, ?, '2026')", [(anon, 'g3'), (anon, 'g4'), (teacher, 'g4')])
        db.execute("INSERT INTO broken_reports VALUES ('g3', 1, 0, '2026')")
        db.execute("INSERT INTO broken_reports VALUES ('g4', 2, 0, '2026')")
    c = client(main, edutictac_oidc='teacher-cookie', recursos_session=main.make_session(anon))
    response = c.get('/api/auth/me')
    assert response.json()['logged_in']
    assert 'Max-Age=0' in response.headers.get('set-cookie', '')
    with main.get_conn() as db:
        assert db.execute('SELECT count(*) FROM favorites WHERE user_id = ?', (anon,)).fetchone()[0] == 0
        assert sorted(r[0] for r in db.execute('SELECT game_key FROM favorites WHERE user_id = ?', (teacher,))) == ['g1', 'g2']
        assert dict(db.execute('SELECT game_key, value FROM ratings WHERE user_id = ?', (teacher,)).fetchall()) == {'g1': 4, 'g2': 5}
        assert tuple(db.execute("SELECT sum, count, avg FROM rating_summary WHERE game_key = 'g2'").fetchone()) == (5, 1, 5.0)
        assert tuple(db.execute("SELECT sum, count FROM rating_summary WHERE game_key = 'g1'").fetchone()) == (4, 1)
        assert sorted(r[0] for r in db.execute('SELECT game_key FROM reports WHERE user_id = ?', (teacher,))) == ['g3', 'g4']
        assert db.execute("SELECT count FROM broken_reports WHERE game_key = 'g4'").fetchone()[0] == 1
    # Repetir no canvia res.
    c.get('/api/auth/me')
    with main.get_conn() as db:
        assert db.execute("SELECT count FROM broken_reports WHERE game_key = 'g4'").fetchone()[0] == 1


def test_central_login_goes_through_commons(banc):
    main, _ = banc
    response = client(main).get('/api/auth/login?next=https://edutictac.es/tauler-professorat.html', follow_redirects=False)
    assert response.headers['location'] == 'https://commons.edutictac.es/api/auth/oidc/login?next=https%3A%2F%2Fedutictac.es%2Ftauler-professorat.html'
    response = client(main).get('/api/auth/login?next=https://evil.test/', follow_redirects=False)
    assert response.headers['location'].endswith('next=https%3A%2F%2Frecursos.edutictac.es%2F')


def test_id_api_down_means_nobody_is_signed_in(tmp_path, monkeypatch):
    main = load(tmp_path, monkeypatch)
    def broken(*args, **kwargs):
        raise main.httpx.ConnectError('down')
    monkeypatch.setattr(main.httpx, 'get', broken)
    main._central_cache.clear()
    assert not client(main, edutictac_oidc='teacher-cookie').get('/api/auth/me').json()['logged_in']


def test_switch_off_restores_the_own_login(tmp_path, monkeypatch):
    main = load(tmp_path, monkeypatch, central='0')
    monkeypatch.setattr(main.httpx, 'get', lambda *a, **k: (_ for _ in ()).throw(AssertionError('no central call')))
    old = main.make_session(f'oidc:{TEACHER_SUB}')
    me = client(main, recursos_session=old, edutictac_oidc='teacher-cookie').get('/api/auth/me').json()
    assert me['logged_in'] and me['sub'] == TEACHER_SUB and not me['central']
