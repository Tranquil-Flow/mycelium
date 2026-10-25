# SPDX-License-Identifier: AGPL-3.0-only
"""Consumers of relay-originated refusals retain codes and legacy handler errors."""
import io
import json
from pathlib import Path
import shutil
import subprocess
from urllib.error import HTTPError

import pytest

from mycelium_a13_macos.pairing import PairingError
from test_pairing_join_errors import _client, ORIGIN


@pytest.mark.parametrize('inner,expected', [
    ({'code': 'relay_join_failed'}, 'relay_join_failed'),
    ('pairing_window_claimed', 'pairing_window_claimed'),
    ({'code': 'PRIVATE body'}, 'pairing_window_unreachable'),
    ({'code': ['PRIVATE']}, 'pairing_window_unreachable'),
    ({'code': 'relay_join_failed', 'body': 'PRIVATE'}, 'pairing_window_unreachable'),
])
def test_pairing_accepts_canonical_or_legacy_bounded_error(inner, expected):
    error = HTTPError(ORIGIN, 500, 'failure', {'Content-Type': 'application/json'},
                      io.BytesIO(json.dumps({'error': inner}).encode()))
    with pytest.raises(PairingError) as caught:
        _client(error).post('/pairing-window/v1/fixture/request', b'{}')
    assert caught.value.code == expected


@pytest.mark.parametrize('relative,function,args', [
    ('mycelium_a13_macos/web/chat.js', 'call', ['/chat/api/state', {}]),
    ('mycelium_swarm_directory/web/swarms.js', 'entry', ['https://unused.invalid', 'state', {}]),
])
def test_shipped_relay_js_callers_extract_canonical_and_legacy_codes(relative, function, args):
    node = shutil.which('node')
    assert node, 'node is required to exercise shipped relay consumers'
    root = Path(__file__).resolve().parents[2]
    harness = r"""
const fs = require('fs'), vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const name = process.argv[2], args = JSON.parse(process.argv[3]);
// Execute the shipped network function, without page boot/DOM or real fetch.
const fn = source.match(new RegExp('async function ' + name + '\\([^]*?\\n}'))[0];
const cases = [{code:'relay_chat_failed'}, 'chat_busy', null, {code:42}];
(async () => {
  const results = [];
  for (const error of cases) {
    const ctx = {fetch:async () => ({ok:false,json:async () => ({error})})};
    vm.createContext(ctx); vm.runInContext(fn, ctx);
    try { await ctx[name](...args); results.push('unexpected_success'); }
    catch (e) { results.push(e.message); }
  }
  process.stdout.write(JSON.stringify(results));
})().catch(e => {console.error(e); process.exit(1);});
"""
    result = subprocess.run([node, '-e', harness, str(root / relative), function, json.dumps(args)],
                            capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(result.stdout) == ['relay_chat_failed', 'chat_busy', 'request_failed', 'request_failed']
