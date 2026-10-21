# SPDX-License-Identifier: AGPL-3.0-only
import os
import sqlite3, json, time

MB = os.path.expanduser('~/mycelium-wp8-02/owner-lhr/authority/managed-control/mailbox.sqlite3')
RUN = 'a13-qwen-20260919T152639Z'
con = sqlite3.connect('file:%s?mode=ro' % MB, uri=True)


def ts(v):
    try:
        return time.strftime('%H:%M:%S', time.localtime(float(v)))
    except Exception:
        return str(v)


print('=== every command for this run: what was asked, when, and how it ended ===')
rows = list(con.execute(
    "select command_id, state, envelope_json, claimed_at, updated_at, command_expires_at "
    "from managed_commands where run_id = ? order by coalesce(claimed_at, updated_at), updated_at", (RUN,)))
print('  total rows:', len(rows))
ops = {}
for command_id, state, envelope, claimed, updated, expires in rows:
    op = '?'
    try:
        statement = json.loads(envelope)['statement']
        op = statement.get('native_command', {}).get('command', '?')
    except Exception:
        pass
    ops[op] = ops.get(op, 0) + 1
    print('  %s  op=%-14s state=%-22s claimed=%s updated=%s expires=%s'
          % (command_id[:20], op, state, ts(claimed) if claimed else '-', ts(updated), ts(expires)))
print()
print('=== operation counts for the run ===', ops)

print()
print('=== was a stop ever enqueued to any node for this run? ===')
hits = 0
for command_id, state, envelope, claimed, updated, expires in rows:
    try:
        cmd = json.loads(envelope)['statement']['native_command']['command']
    except Exception:
        continue
    if cmd in ('stop', 'shutdown', 'quit'):
        hits += 1
        print('  STOP: %s state=%s updated=%s' % (command_id[:20], state, ts(updated)))
print('  stop-like commands:', hits)
con.close()
