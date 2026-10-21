# SPDX-License-Identifier: AGPL-3.0-only
"""wp8-03 item 2: attribute each target call to its calling site, measured from real runs.

The receipt for item 2 must list every replacement as `file:line`, and the rounds measured on
2026-09-16 showed one 1 s poll loop accounting for most of the 33 calls. Hand-counting that from
a grep is how an inventory goes stale the moment someone edits a line. Attribution makes the
list a by-product of a real run.

Constraints pinned here: it is OPT-IN, it must never raise (a broken audit must not break a
round), and it must never touch the network -- a local append only.
"""
import importlib.util
import json
import pathlib
import sys

import pytest

OPERATOR = pathlib.Path(__file__).resolve().parents[2] / 'scripts/a13_operator'
if str(OPERATOR) not in sys.path:
    sys.path.insert(0, str(OPERATOR))


def _load(monkeypatch, tmp_path):
    """Import the RPC module fresh: it demands four A13_* env vars at import time."""
    owner = tmp_path / 'owner-lhr'
    owner.mkdir()
    monkeypatch.setenv('A13_OWNER_ROOT', str(owner))
    monkeypatch.setenv('A13_REMOTE_ROOT', '/Users/example/remote')
    monkeypatch.setenv('A13_TARGET_PREVIEW', 'preview70')
    monkeypatch.setenv('A13_RPC_ROUND', 'rpc-round70')
    name = 'round14_rpc_attribution_probe'
    spec = importlib.util.spec_from_file_location(name, OPERATOR / 'round14_rpc.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_call_is_attributed_to_its_calling_site(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path)
    audit = tmp_path / 'target-calls.jsonl'
    monkeypatch.setenv('A13_TARGET_CALL_AUDIT', str(audit))

    def probe_site():
        module._attribute_call('/api/staging/check')   # attribution, not an RPC
        return 1

    probe_site()
    entries = [json.loads(line) for line in audit.read_text().splitlines()]
    assert len(entries) == 1
    assert entries[0]['path'] == '/api/staging/check'
    site_file, line = entries[0]['site'].rsplit(':', 1)
    assert site_file == pathlib.Path(__file__).name
    assert int(line) > 0


def test_attribution_is_off_unless_asked(monkeypatch, tmp_path):
    module = _load(monkeypatch, tmp_path)
    monkeypatch.delenv('A13_TARGET_CALL_AUDIT', raising=False)
    module._attribute_call('/api/staging/check')
    assert list(tmp_path.glob('*.jsonl')) == []


def test_attribution_never_raises_on_an_unwritable_destination(monkeypatch, tmp_path):
    """A broken audit must never be the reason a round fails."""
    module = _load(monkeypatch, tmp_path)
    monkeypatch.setenv('A13_TARGET_CALL_AUDIT', '/definitely-not-here/nested/x.jsonl')
    module._attribute_call('/api/status')          # must not raise


def test_target_attributes_before_it_does_anything_else(monkeypatch, tmp_path):
    """The attribution must sit at the top of target(), before the surface guard and the probe."""
    module = _load(monkeypatch, tmp_path)
    import inspect
    body = inspect.getsource(module.target)
    first_statement = [line.strip() for line in body.splitlines()[1:]
                       if line.strip() and not line.strip().startswith('"""')][0]
    assert first_statement == '_attribute_call(path)'
