# SPDX-License-Identifier: AGPL-3.0-only
"""The public surface probe names itself: Cloudflare's browser integrity check answers 403 to the
default Python-urllib user agent, which made every live check fail (2026-10-20 go-live)."""
from mycelium_swarm_directory import surface_probe


class _Resp:
    status = 404
    headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_probe_requests_carry_an_explicit_user_agent():
    seen = []

    def opener(request, timeout):
        seen.append(request.get_header('User-agent'))
        return _Resp()

    surface_probe._fetch('https://mycelium.now', 'GET', '/anything', None, opener, 5.0)
    assert seen and seen[0] and 'Python-urllib' not in seen[0]
    assert seen[0].startswith('mycelium-surface-probe/')
