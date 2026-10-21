# SPDX-License-Identifier: AGPL-3.0-only
"""Cleanly stop both packaged apps through their normal shutdown path."""
from round14_rpc import owner, target

for label, fn in (('TARGET', target), ('OWNER', owner)):
    try:
        print(label, fn('/api/quit', {}), flush=True)
    except Exception as exc:
        print(label, 'quit_error', repr(exc)[:300], flush=True)
