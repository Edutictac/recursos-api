"""Copy existing public books without replacing an installation's catalogue."""
import importlib.util
from pathlib import Path
import sqlite3

import pytest
from test_books import books, package, DRAFT
from test_book_reviews import student_client

spec = importlib.util.spec_from_file_location('public_book_migration', Path(__file__).parents[1] / 'scripts/migrate-public-books.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


@pytest.fixture
def databases(books, tmp_path):
    module, client = books
    source = Path(module.DB_PATH)
    target = tmp_path / 'target.db'
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
        dst.execute("INSERT INTO resources(provider,external_id,title) VALUES ('existing','preserve','Existing catalogue')")
    published = client.post('/api/internal/llibres/' + DRAFT, content=package(editable=True), headers={'If-Match':'"0"'})
    assert published.status_code == 200
    pupil = student_client(books)
    assert pupil.post('/api/internal/llibres/submissions/' + DRAFT, content=package(), headers={'If-Match':'"0"'}).status_code == 200
    return source, target, client, published.json()['id']


def test_preview_copy_preserve_private_and_idempotency(databases):
    source, target, client, book = databases
    options = {'source':source,'target':target,'public_base':'http://bank.test:8086'}
    assert migration.migrate(**options)['books_to_copy'] == 1
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT count(*) FROM llibres_publications').fetchone()[0] == 0
    assert migration.migrate(**options, apply=True)['books_to_copy'] == 1
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT title FROM resources WHERE provider='existing'").fetchone()[0] == 'Existing catalogue'
        assert db.execute('SELECT count(*) FROM llibres_submissions').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM llibres_review_files').fetchone()[0] == 0
        url = db.execute("SELECT play_url FROM resources WHERE provider='edutictac-llibres'").fetchone()[0]
        assert url == 'http://bank.test:8086/api/llibres/public/' + book + '/index.html'
        assert db.execute("SELECT body FROM llibres_files WHERE path='source.elpx'").fetchone()[0]
    assert migration.migrate(**options, apply=True)['already_present'] == 1
    with sqlite3.connect(source) as db:
        assert db.execute('SELECT count(*) FROM llibres_submissions').fetchone()[0] == 1


def test_conflict_does_not_insert_other_books_or_overwrite_target(databases):
    source, target, client, book = databases
    migration.migrate(source, target, 'http://bank.test', apply=True)
    draft = '11234567-89ab-cdef-0123-456789abcdef'
    assert client.post('/api/internal/llibres/' + draft, content=package(), headers={'If-Match':'"0"'}).status_code == 200
    with sqlite3.connect(target) as db:
        db.execute('UPDATE llibres_publications SET revision=99 WHERE id=?',(book,))
    with pytest.raises(migration.MigrationError, match='different revision'):
        migration.migrate(source, target, 'http://bank.test', apply=True)
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT count(*) FROM llibres_publications').fetchone()[0] == 1
        assert db.execute('SELECT revision FROM llibres_publications').fetchone()[0] == 99


def test_incomplete_files_fail_without_writing(databases):
    source, target, _, _ = databases
    with sqlite3.connect(source) as db:
        db.execute("DELETE FROM llibres_files WHERE path='cover.png'")
    with pytest.raises(migration.MigrationError, match='incomplete'):
        migration.migrate(source, target, 'http://bank.test', apply=True)
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT count(*) FROM llibres_publications').fetchone()[0] == 0


def test_invalid_paths_and_origin_are_rejected(databases):
    source, target, _, _ = databases
    with pytest.raises(migration.MigrationError, match='different databases'):
        migration.migrate(source, source, 'http://bank.test')
    with pytest.raises(migration.MigrationError, match='origin'):
        migration.migrate(source, target, 'http://bank.test/unexpected-path')
