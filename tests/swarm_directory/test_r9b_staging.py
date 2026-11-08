"""Staging disclosure is synchronous, explicit opt-in and hostname fenced."""
import urllib.request
import pytest
from mycelium_swarm_directory.server import DirectoryServer
from test_site_v2_browser import local

@pytest.mark.parametrize('origin,flag,visible', [
    ('https://next.mycelium.now', None, False),
    ('https://next.mycelium.now', '1', True),
    ('https://mycelium.now', '1', False),
])
def test_staging_banner_requires_both_flag_and_staging_origin(local, monkeypatch, origin, flag, visible):
    if flag is None: monkeypatch.delenv('MYCELIUM_STAGING_SAMPLES', raising=False)
    else: monkeypatch.setenv('MYCELIUM_STAGING_SAMPLES', flag)
    server = DirectoryServer(local['directory'].directory, public_origin=origin).start()
    try:
        for path in ['/', '/swarms/']:
            with urllib.request.urlopen(server.origin + path) as response:
                html = response.read().decode()
                assert ('Staging · sample data' in html) is visible
                assert "script-src 'self'" in response.headers['Content-Security-Policy']
    finally:
        server.close()
