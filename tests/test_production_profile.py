"""Validate the actual Compose parser output, including tmpfs mount syntax."""
import json
from pathlib import Path
import os
import shutil
import subprocess

import pytest


def test_production_profile_only_uses_absolute_tmpfs_mounts(tmp_path):
    if not shutil.which('docker'):
        pytest.skip('Docker Compose required for profile validation')
    source = Path(__file__).parents[1] / 'compose.production.yml'
    profile = tmp_path / 'compose.yml'; profile.write_bytes(source.read_bytes())
    (tmp_path / 'service.env').write_text('RECURSOS_SEED=0\n')
    result = subprocess.run(['docker','compose','-f',str(profile),'config','--format','json'],
        capture_output=True,check=True,env={'PATH':os.environ['PATH']})
    service = json.loads(result.stdout)['services']['recursos-api']
    assert service['tmpfs']
    assert all(mount.startswith('/') for mount in service['tmpfs'])
    assert service['read_only'] is True
    assert service['command'][-1] == '8004'
    assert service['command'][service['command'].index('--host')+1] == '127.0.0.1'
