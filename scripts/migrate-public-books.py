#!/usr/bin/env python3
"""Copy already public books between local SQLite installations.

Dry run by default. Upgrade the target API and make a SQLite backup first.
No credentials, private submissions, preferences or unrelated resources move.
"""
import argparse
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

PROVIDER = 'edutictac-llibres'
LINKS = ('source_url', 'play_url', 'download_url', 'reuse_url', 'thumbnail_url')


class MigrationError(ValueError):
    pass


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def migrate(source, target, public_base, *, apply=False):
    source, target = Path(source).resolve(), Path(target).resolve()
    if source == target:
        raise MigrationError('Source and target must be different databases')
    origin = urlsplit(public_base)
    if origin.scheme not in ('http', 'https') or not origin.hostname or origin.username or origin.password or origin.path not in ('', '/') or origin.query or origin.fragment:
        raise MigrationError('Public base must be an HTTP(S) origin')
    if not source.is_file() or not target.is_file():
        raise MigrationError('Both databases must already exist')
    base = public_base.rstrip('/')
    src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(target.as_uri() + ('?mode=rw' if apply else '?mode=ro'), uri=True, timeout=30)
    src.row_factory = dst.row_factory = sqlite3.Row
    plan, skipped, size = [], 0, 0
    try:
        src.execute('BEGIN')
        dst.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        for table in ('resources', 'llibres_publications', 'llibres_files'):
            source_cols = {r['name'] for r in src.execute(f'PRAGMA table_info({table})')}
            target_cols = {r['name'] for r in dst.execute(f'PRAGMA table_info({table})')}
            if not source_cols or not source_cols.issubset(target_cols):
                raise MigrationError('Missing or incompatible schema; upgrade the target API first')
        for row in src.execute('SELECT * FROM llibres_publications WHERE active=1 ORDER BY id'):
            publication = dict(row)
            resource = src.execute('SELECT * FROM resources WHERE provider=? AND external_id=? AND active=1', (PROVIDER, row['id'])).fetchone()
            if resource is None:
                raise MigrationError('Public book is missing its active catalogue entry')
            resource = {key: resource[key] for key in resource.keys() if key != 'id'}
            prefix = '/api/llibres/public/' + row['id'] + '/'
            for key in LINKS:
                if resource.get(key):
                    parsed = urlsplit(resource[key])
                    if not parsed.path.startswith(prefix) or parsed.query or parsed.fragment:
                        raise MigrationError('Public book has an unexpected catalogue link')
                    resource[key] = base + parsed.path
            files = [dict(item) for item in src.execute('SELECT * FROM llibres_files WHERE book=? ORDER BY path', (row['id'],))]
            if not {'index.html', 'cover.png'}.issubset({item['path'] for item in files}):
                raise MigrationError('Public book has incomplete reader files')
            old = dst.execute('SELECT * FROM llibres_publications WHERE id=?', (row['id'],)).fetchone()
            old_resource = dst.execute('SELECT * FROM resources WHERE provider=? AND external_id=?', (PROVIDER, row['id'])).fetchone()
            if old is not None or old_resource is not None:
                old_files = [dict(item) for item in dst.execute('SELECT * FROM llibres_files WHERE book=? ORDER BY path', (row['id'],))]
                if old is None or old_resource is None or any(old[key] != value for key, value in publication.items()) or any(old_resource[key] != value for key, value in resource.items()) or old_files != files:
                    raise MigrationError('Target contains a different revision or conflicting catalogue entry; nothing was overwritten')
                skipped += 1
                continue
            plan.append((publication, resource, files))
            size += sum(len(item['body']) for item in files)
        # Finish all conflict checks before inserting the first book.
        if apply:
            for publication, resource, files in plan:
                for table, rows in [('llibres_publications', [publication]), ('resources', [resource]), ('llibres_files', files)]:
                    for item in rows:
                        columns = ','.join(map(quote, item))
                        dst.execute(f'INSERT INTO {table} ({columns}) VALUES ({",".join("?" for _ in item)})', tuple(item.values()))
            dst.commit()
        else:
            dst.rollback()
        return {'apply': apply, 'books_to_copy': len(plan), 'already_present': skipped, 'bytes_to_copy': size}
    except BaseException:
        dst.rollback()
        raise
    finally:
        src.close()
        dst.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--target', required=True)
    parser.add_argument('--public-base', required=True)
    parser.add_argument('--apply', action='store_true', help='Apply after making and checking a target backup')
    args = parser.parse_args()
    try:
        print(json.dumps(migrate(args.source, args.target, args.public_base, apply=args.apply)))
    except (MigrationError, sqlite3.Error) as error:
        # Never print database rows, owner identifiers or publication metadata.
        parser.exit(1, 'Public book migration stopped: ' + (str(error) if isinstance(error, MigrationError) else 'database operation failed') + '\n')


if __name__ == '__main__':
    main()
