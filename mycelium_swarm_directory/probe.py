# SPDX-License-Identifier: AGPL-3.0-only
"""Physical-test probe: does a live swarm entry refuse requests without a valid grant?

    python -m mycelium_swarm_directory.probe --entry https://<relay>.trycloudflare.com --swarm sw-...

Sends, from a fresh throwaway key: (1) ``state`` (must answer 200), (2) a send
with a self-signed grant, (3) a send with an empty grant, (4) an unsigned
send.  Prints one JSON line with every status/code; exit 0 only when the
entry is reachable and all three sends are refused (403/400) -- i.e. nothing
without an owner-signed grant reaches the model.  Sends no prompt content
beyond the word ``probe`` and never succeeds in running one.
"""
from __future__ import annotations

from mycelium_http_identity import with_user_agent
import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from . import docs
from .crypto import Ed25519Signer


def _post(origin: str, route: str, document: dict) -> tuple[int, str | None]:
    request = urllib.request.Request(origin + '/swarm-entry/v1/' + route, data=json.dumps(document).encode(),
                                     headers=with_user_agent({'Content-Type': 'application/json'}), method='POST')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=15) as response:
            return response.status, None
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read(4096)).get('error')
        except ValueError:
            return error.code, None
    except OSError:
        return 0, 'unreachable'


def probe(entry: str, swarm_id: str) -> dict:
    key = Ed25519Signer.generate()
    now = int(time.time())
    forged = docs.grant(key, swarm_id=swarm_id, grantee_key=key.public_key, issued_at=now, expires_at=now + 600,
                        max_requests=5, max_tokens=50)
    signed = docs.entry_send(key, swarm_id, forged, 'probe', 4, at=now)
    result = {'state': _post(entry, 'state', {}),
              'self_signed_grant': _post(entry, 'send', signed),
              'empty_grant': _post(entry, 'send', docs.entry_send(key, swarm_id, {}, 'probe', 4, at=now)),
              'unsigned': _post(entry, 'send', {k: v for k, v in signed.items() if k != 'sig'} |
                                {'nonce': docs.new_nonce()})}
    refused = all(result[name][0] in (400, 403) for name in ('self_signed_grant', 'empty_grant', 'unsigned'))
    result['verdict'] = 'refused_without_grant' if result['state'][0] == 200 and refused else 'FAIL'
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog='python -m mycelium_swarm_directory.probe', allow_abbrev=False)
    parser.add_argument('--entry', required=True, help='entry origin, e.g. https://name.trycloudflare.com')
    parser.add_argument('--swarm', required=True, help='swarm id (sw-...)')
    args = parser.parse_args(argv)
    docs.origin(args.entry, allow_loopback=True)
    docs.ident(args.swarm, docs.SWARM_ID, 'swarm_id_invalid')
    result = probe(args.entry, args.swarm)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['verdict'] == 'refused_without_grant' else 1


if __name__ == '__main__':
    sys.exit(main())
