"""Laptop-local driver: publish target-side reads WITHOUT ssh (wp8-03 item 1).

Why this exists. The chain reaches the target over ssh for 33 RPC calls per round (measured
2026-09-16 via ssh_audit: 77-83 invocations, all of them setup/observation, none of them the
acceptance). PLAN.md section 3 criterion 3 wants an acceptance run in which the targets are
driven only through their own product UI/API. This driver runs ON THE LAPTOP, asks the app's
own localhost API, and publishes each answer as a bounded atomic JSON file that the owner
collects after the run -- so the mid-run ssh RPCs disappear and nothing inbound to the laptop
is needed.

Contracts:
  * the port comes from the app's own ``native-control.json`` (never guessed, never taken
    from an argument, so the driver cannot be pointed at a foreign service);
  * publishing is atomic (temp file + os.replace) and mode 0600 under a 0700 directory;
  * a FAILED read is published as ``ok: false`` with a bounded error name -- a driver that
    dies on the first refusal tells the owner nothing;
  * the file name carries the round id, so a stale file cannot be mistaken for a fresh one;
  * this module never invokes ssh. That is a claim the rehearsal measures rather than trusts
    (tests/a13_operator/test_wp8_03_laptop_driver.py runs it under the ssh_audit shim and
    requires ssh_calls == 0).
"""
import argparse
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

PROTOCOL = 'mycelium.a13.laptop_driver.v1'
PUBLISH_DIR = 'wp8-03'
PORT_MIN, PORT_MAX = 49152, 65535


def control_port(root):
    """The app's own private port, read from its surface document."""
    doc = json.loads((Path(root) / 'native-control.json').read_text())
    port = doc.get('port')
    if isinstance(port, bool) or not isinstance(port, int) or not PORT_MIN <= port <= PORT_MAX:
        raise ValueError('private_port_invalid')
    return port


def publish(root, round_id, name, value):
    """Write one bounded record atomically; return the path."""
    directory = Path(root) / 'target' / PUBLISH_DIR / round_id
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    target = directory / (name + '.json')
    handle, temporary = tempfile.mkstemp(dir=str(directory))
    try:
        with os.fdopen(handle, 'w') as stream:
            json.dump(value, stream, sort_keys=True, indent=1)
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target


def read(port, path, body=None, timeout=60.0):
    """One product-API read against the app's localhost surface."""
    request = urllib.request.Request(
        'http://127.0.0.1:%d%s' % (port, path),
        data=None if body is None else json.dumps(body).encode(),
        headers={} if body is None else {'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def drive(root, round_id, reads, timeout=60.0):
    """Perform every read and publish the outcome of each. Never raises on a read failure."""
    port = control_port(root)
    published = []
    for name, path, body in reads:
        try:
            value = {'ok': True, 'result': read(port, path, body, timeout=timeout)}
        except Exception as exc:  # noqa: BLE001 - a refusal is data, not a crash
            value = {'ok': False, 'error': type(exc).__name__, 'detail': str(exc)[:200]}
        published.append(str(publish(
            root, round_id, name,
            {'protocol': PROTOCOL, 'round_id': round_id, 'name': name, 'path': path, **value},
        )))
    return published


def _parse_read(spec):
    """``name=path`` or ``name=path=@json-body``."""
    parts = spec.split('=', 2)
    if len(parts) < 2 or not parts[0] or not parts[1].startswith('/'):
        raise argparse.ArgumentTypeError('expected name=/path')
    body = json.loads(parts[2]) if len(parts) == 3 and parts[2] else None
    return (parts[0], parts[1], body)


def main(argv=None):
    parser = argparse.ArgumentParser(description='wp8-03 laptop-local driver (no ssh)')
    parser.add_argument('--root', required=True, help='the target root holding native-control.json')
    parser.add_argument('--round-id', required=True)
    parser.add_argument('--read', action='append', type=_parse_read, required=True,
                        metavar='name=/path', help='repeatable')
    parser.add_argument('--timeout', type=float, default=60.0)
    args = parser.parse_args(argv)
    published = drive(args.root, args.round_id, args.read, timeout=args.timeout)
    print(json.dumps({'published': published}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
