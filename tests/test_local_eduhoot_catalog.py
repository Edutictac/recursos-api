from types import SimpleNamespace

from test_teacher_favorites import load_main
from app.models import Resource


def test_local_catalog_uses_local_ids_without_hiding_other_providers(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    monkeypatch.setenv('EDUHOOT_LOCAL_CATALOG', '1')
    with main.get_conn() as conn:
        for provider in ('eduhoot', 'jclic'):
            conn.execute('INSERT INTO resources (provider,external_id,title,active) VALUES (?,?,?,1)',
                         (provider, '1', 'Remote ' + provider))
    monkeypatch.setattr(main, '_load_eduhoot_resources', lambda sub: [Resource(
        provider='eduhoot', external_id='1', title='Local quiz',
        play_url='http://centre.local:8090/solo/?id=1', format='eduhoot')])
    request = SimpleNamespace(cookies={}, client=SimpleNamespace(host='127.0.0.1'))
    result = main.list_resources(request)
    assert result['total'] == 2
    quiz = next(item for item in result['items'] if item['provider'] == 'eduhoot')
    assert quiz['title'] == 'Local quiz'
    assert quiz['play_url'].startswith('http://centre.local:8090/')
    result = main.list_resources(request, provider='eduhoot')
    assert result['total'] == 1


def test_local_catalog_does_not_fall_back_to_wrong_remote_ids(tmp_path, monkeypatch):
    main = load_main(tmp_path, monkeypatch)
    monkeypatch.setenv('EDUHOOT_LOCAL_CATALOG', '1')
    with main.get_conn() as conn:
        conn.execute("INSERT INTO resources (provider,external_id,title,active) VALUES ('eduhoot','1','Remote',1)")
    monkeypatch.setattr(main, '_load_eduhoot_resources', lambda sub: [])
    request = SimpleNamespace(cookies={}, client=SimpleNamespace(host='127.0.0.1'))
    assert main.list_resources(request, provider='eduhoot')['total'] == 0
