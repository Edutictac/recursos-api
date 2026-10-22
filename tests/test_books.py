"""Publications, resource index and file permissions on a disposable SQLite DB."""
import base64
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from test_play_ingest import load_main

DRAFT="01234567-89ab-cdef-0123-456789abcdef"
ACTOR="synthetic-teacher-one"


def png():
    out=io.BytesIO();Image.new('RGBA',(16,16),(50,150,80,255)).save(out,'PNG');return out.getvalue()


def package(title="Un conte", editable=False, svg=None, extra=None, language="va"):
    image=base64.b64encode(png()).decode()
    scene=svg or '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="1280" height="720" viewBox="0 0 1280 720"><rect width="1280" height="720" fill="#fff"/><text x="20" y="50">Una història</text><image xlink:href="data:image/png;base64,'+image+'" x="40" y="60" width="100" height="100"/></svg>'
    files={'manifest.json':json.dumps(dict(format='llibres-publication',version=1,visibility='public',confirmed=True,title=title,author='Aula de prova',description='Una història compartida.',license='CC-BY-SA-4.0',language=language,pages=['Portada','Final'])).encode(),
           'pages/1.svg':scene.encode(),'pages/2.svg':scene.encode(),'cover.png':png()}
    if editable:
        source=io.BytesIO()
        with zipfile.ZipFile(source,'w') as z:z.writestr('content.xml','<ode/>')
        files['source.elpx']=source.getvalue()
    files.update(extra or {})
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in files.items():z.writestr(name,data)
    return out.getvalue()


@pytest.fixture
def books(tmp_path,monkeypatch):
    monkeypatch.setenv('EDUTICTAC_ID_APP_TOKEN','synthetic-books-token')
    monkeypatch.setenv('LLIBRES_PUBLIC_BASE_URL','http://testserver')
    module=load_main(tmp_path,monkeypatch)
    client=TestClient(module.app)
    client.headers.update({'Authorization':'Bearer synthetic-books-token','X-Llibres-Publisher':ACTOR})
    return module,client


def test_publish_update_withdraw_are_atomic_and_idempotent(books):
    module,client=books;route='/api/internal/llibres/'+DRAFT
    assert client.get(route).json()['published'] is False
    body=package(editable=True)
    first=client.post(route,content=body,headers={'If-Match':'"0"'})
    assert first.status_code==200,first.text
    published=first.json();key=published['id']
    assert client.post(route,content=body,headers={'If-Match':'"0"'}).json()['revision']==1
    public=TestClient(module.app)
    items=public.get('/api/llibres/books').json()['items'];assert len(items)==1
    assert public.get('/api/resources?provider=edutictac-llibres').json()['items'][0]['external_id']==key
    html=public.get('/api/llibres/public/'+key+'/index.html')
    assert html.status_code==200 and 'Una història' in html.text
    assert 'data:image' not in html.text and 'images/' in html.text
    assert '<details><summary>Sobre este lector y licencias' in html.text
    assert html.text.index('Sobre este lector y licencias') < html.text.index('Código fuente del servidor')
    assert 'sandbox allow-downloads' in html.headers['Content-Security-Policy']
    assert 'allow-scripts' not in html.headers['Content-Security-Policy']
    with module.get_conn() as db:
        assert db.execute("SELECT count(*) FROM llibres_files WHERE book=? AND path LIKE 'images/%'",(key,)).fetchone()[0]==1
    source=public.get('/api/llibres/public/'+key+'/source.elpx')
    assert source.headers['Content-Disposition'].startswith('attachment;')
    assert client.post(route,content=package(title='Changed'),headers={'If-Match':'"0"'}).status_code==412
    assert client.get(route).json()['title']=='Un conte'
    updated=client.post(route,content=package(title='Canviat'),headers={'If-Match':'"1"'})
    assert updated.json()['id']==key and updated.json()['revision']==2
    assert public.get('/api/llibres/public/'+key+'/source.elpx').status_code==404
    assert client.delete(route,headers={'If-Match':'"1"'}).status_code==412
    assert client.delete(route,headers={'If-Match':'"2"'}).json()['published'] is False
    assert public.get('/api/llibres/books').json()['items']==[]
    assert public.get('/api/resources?provider=edutictac-llibres').json()['items']==[]
    assert public.get('/api/llibres/public/'+key+'/index.html').status_code==404
    assert client.post(route,content=body,headers={'If-Match':'"3"'}).json()['id']==key


def test_owner_is_server_verified_and_does_not_leak_in_catalogue(books):
    module,client=books;route='/api/internal/llibres/'+DRAFT
    published=client.post(route,content=package(),headers={'If-Match':'"0"'}).json()
    other=TestClient(module.app)
    assert other.post(route,content=package()).status_code==401
    other.headers.update({'Authorization':'Bearer synthetic-books-token','X-Llibres-Publisher':'synthetic-teacher-two'})
    assert other.get(route).json()['published'] is False
    assert other.delete(route,headers={'If-Match':'"1"'}).status_code==404
    public=TestClient(module.app)
    assert ACTOR not in public.get('/api/llibres/books').text
    assert 'owner' not in public.get('/api/resources?provider=edutictac-llibres').text
    assert public.get('/api/llibres/public/'+published['id']+'/../../manifest.json').status_code==404


@pytest.mark.parametrize('attack',[
 '<script>window.__unsafe=1</script>',
 '<foreignObject><div xmlns="http://www.w3.org/1999/xhtml">x</div></foreignObject>',
 '<image href="https://example.invalid/private.png"/>',
 '<rect onload="alert(1)" width="20" height="20"/>',
 '<rect style="fill:url(https://example.invalid/x)"/>',
])
def test_active_svg_is_rejected_before_any_publication(books,attack):
    module,client=books
    svg='<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720">'+attack+'</svg>'
    response=client.post('/api/internal/llibres/'+DRAFT,content=package(svg=svg),headers={'If-Match':'"0"'})
    assert response.status_code==422
    assert client.get('/api/llibres/books').json()['items']==[]


@pytest.mark.parametrize('name',['../evil.js','/tmp/evil','C:/evil','pages/../../x','evil.html'])
def test_zip_paths_and_executable_files_are_rejected(books,name):
    _,client=books
    assert client.post('/api/internal/llibres/'+DRAFT,content=package(extra={name:b'evil'}),headers={'If-Match':'"0"'}).status_code==422


def test_metadata_is_inert_text_and_quota_failure_preserves_previous_publication(books):
    module,client=books;route='/api/internal/llibres/'+DRAFT
    result=client.post(route,content=package(title='<script>alert(1)</script>'),headers={'If-Match':'"0"'}).json()
    html=client.get('/api/llibres/public/'+result['id']+'/index.html').text
    assert '<script>' not in html and '&lt;script&gt;' in html
    module.book_publications.global_limit=1
    assert client.post(route,content=package(title='New'),headers={'If-Match':'"1"'}).status_code==413
    assert client.get(route).json()['revision']==1


@pytest.mark.parametrize("manifest", [None, [], "invalid", 1])
def test_invalid_manifest_types_are_rejected(books, manifest):
    _, client = books
    raw = package()
    entries = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as source:
        entries = {n: source.read(n) for n in source.namelist()}
    entries["manifest.json"] = json.dumps(manifest).encode()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items(): archive.writestr(name, data)
    assert client.post("/api/internal/llibres/" + DRAFT, content=output.getvalue(), headers={"If-Match": '\"0\"'}).status_code == 422


def test_corresponding_source_is_public_without_runtime_data(books):
    module, _ = books
    response = TestClient(module.app).get("/api/llibres/source.zip")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert "recursos-api/app/books.py" in names
        assert "recursos-api/requirements.txt" in names
        assert not any(name.endswith((".db", ".env", ".pyc")) for name in names)


def test_non_ascii_authentication_is_rejected_without_server_error(books):
    module, _ = books
    response = TestClient(module.app).get("/api/internal/llibres/" + DRAFT, headers={b"Authorization": b"Bearer \xe9"})
    assert response.status_code == 401


def test_previously_published_reader_also_hides_source_under_licenses(books):
    module, client = books
    result = client.post('/api/internal/llibres/'+DRAFT, content=package(editable=True), headers={'If-Match':'"0"'}).json()
    route = '/api/llibres/public/'+result['id']+'/index.html'
    current = client.get(route).text
    link = '<p><a href="../../source.zip" download>Codi font del servidor / Código fuente del servidor</a></p>'
    about = '<details><summary>Sobre este lector y licencias / Sobre este lector i llicències</summary>'+link+'</details>'
    previous = current.replace(about, '').replace('<details><summary>Crèdits', link+'<details><summary>Crèdits', 1)
    with module.get_conn() as db:
        db.execute("UPDATE llibres_files SET body=? WHERE book=? AND path='index.html'", (previous.encode(), result['id']))
    assert client.get(route).text == current
    assert client.get('/api/internal/llibres/'+DRAFT).json()['revision'] == result['revision']


@pytest.mark.parametrize(("language", "html_language"), [
    ("es", "es"), ("va", "ca-valencia"), ("ca-valencia", "ca-valencia"),
    ("ca", "ca"), ("en", "en"),
])
def test_publication_preserves_supported_language(books, language, html_language):
    module, client = books
    published = client.post('/api/internal/llibres/' + DRAFT,
        content=package(language=language, editable=True), headers={'If-Match':'"0"'})
    assert published.status_code == 200, published.text
    reader = client.get('/api/llibres/public/' + published.json()['id'] + '/index.html')
    assert '<html lang="' + html_language + '">' in reader.text
    indexed = client.get('/api/resources?provider=edutictac-llibres').json()['items'][0]
    assert indexed['language'] == [language]
    if language == 'en':
        assert 'Credits and licences' in reader.text
        assert 'Download editable file' in reader.text


@pytest.mark.parametrize("language", ["fr", "<script>", "EN", ""])
def test_unsupported_language_still_rejected(books, language):
    _, client = books
    response = client.post('/api/internal/llibres/' + DRAFT,
        content=package(language=language), headers={'If-Match':'"0"'})
    assert response.status_code == 422
    assert client.get('/api/llibres/books').json()['items'] == []
