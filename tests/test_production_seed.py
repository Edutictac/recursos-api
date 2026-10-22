"""Production upgrades can keep catalogue seeding disabled without changing defaults."""
import json
from pathlib import Path
from test_play_ingest import load_main


def test_production_start_does_not_add_demonstration_rows(tmp_path, monkeypatch):
    monkeypatch.setenv('RECURSOS_SEED', '0')
    module = load_main(tmp_path, monkeypatch)
    with module.get_conn() as db:
        for table in ['rating_summary', 'broken_reports', 'submissions']:
            assert db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0


def test_default_start_keeps_existing_seed_behaviour(tmp_path, monkeypatch):
    monkeypatch.delenv('RECURSOS_SEED', raising=False)
    module = load_main(tmp_path, monkeypatch)
    seed = json.loads((Path(module.__file__).parent / 'seed.json').read_text())
    with module.get_conn() as db:
        for table in ['rating_summary', 'broken_reports', 'submissions']:
            assert db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == len(seed.get(table, []))
