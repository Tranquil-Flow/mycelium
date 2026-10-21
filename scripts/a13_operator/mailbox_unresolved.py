# SPDX-License-Identifier: AGPL-3.0-only
import os
import sqlite3, json, time

MB = os.path.expanduser('~/mycelium-wp8-02/owner-lhr/authority/managed-control/mailbox.sqlite3')
con = sqlite3.connect('file:%s?mode=ro' % MB, uri=True)
cur = con.cursor()


def ts(v):
    try:
        return time.strftime('%m-%d %H:%M:%S', time.localtime(float(v)))
    except Exception:
        return str(v)


def tsms(v):
    try:
        return time.strftime('%m-%d %H:%M:%S', time.localtime(float(v) / 1000.0))
    except Exception:
        return str(v)


print('=== non-terminal rows by state and run ===')
q = ("select state, run_id, count(*), min(updated_at), max(updated_at) from managed_commands "
     "where state in ('reconciliation_required','pending','claimed','failed') "
     "group by state, run_id order by max(updated_at) desc limit 40")
for state, run_id, n, lo, hi in cur.execute(q):
    print('  %-24s %-34s %5d  %s .. %s' % (state, run_id, n, ts(lo), ts(hi)))

print()
print('=== every command state for THIS run (a13-qwen-20260919T152639Z) ===')
q2 = ("select state, count(*), min(updated_at), max(updated_at) from managed_commands "
      "where run_id = ? group by state")
for state, n, lo, hi in cur.execute(q2, ('a13-qwen-20260919T152639Z',)):
    print('  %-24s %5d  %s .. %s' % (state, n, ts(lo), ts(hi)))

print()
print('=== the PREVIOUS run (p75r1) a13-qwen-20260919T135902Z ===')
for state, n, lo, hi in cur.execute(q2, ('a13-qwen-20260919T135902Z',)):
    print('  %-24s %5d  %s .. %s' % (state, n, ts(lo), ts(hi)))

print()
print('=== reconciliation_required rows: generation + expiry, newest 10 ===')
q3 = ("select command_id, target_node_id, membership_generation, command_expires_at, updated_at, "
      "claimed_at from managed_commands where state='reconciliation_required' "
      "order by updated_at desc limit 10")
for cid, node, gen, exp, upd, cla in cur.execute(q3):
    print('  %s  %s gen=%s expires=%s updated=%s claimed=%s'
          % (cid[:18], (node or '?')[-12:], gen, ts(exp), ts(upd), ts(cla) if cla else '-'))

print()
print('=== pending rows: newest 10 ===')
q4 = ("select command_id, target_node_id, membership_generation, command_expires_at, updated_at "
      "from managed_commands where state='pending' order by updated_at desc limit 10")
for cid, node, gen, exp, upd in cur.execute(q4):
    print('  %s  %s gen=%s expires=%s updated=%s' % (cid[:18], (node or '?')[-12:], gen, ts(exp), ts(upd)))

print()
print('=== claimed rows (who holds a live claim?) ===')
q5 = ("select command_id, target_node_id, membership_generation, claimed_at, command_expires_at, run_id "
      "from managed_commands where state='claimed' order by claimed_at desc limit 10")
for cid, node, gen, cla, exp, run in cur.execute(q5):
    print('  %s  %s gen=%s claimed=%s expires=%s run=%s'
          % (cid[:18], (node or '?')[-12:], gen, ts(cla), ts(exp), (run or '?')[:30]))
con.close()
