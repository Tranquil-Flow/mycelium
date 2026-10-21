# SPDX-License-Identifier: AGPL-3.0-only
import sqlite3, json, os, time

MB = os.path.expanduser('~/mycelium-wp8-02/owner-lhr/authority/managed-control/mailbox.sqlite3')
print('mailbox:', MB, os.path.getsize(MB), 'bytes')
con = sqlite3.connect('file:%s?mode=ro' % MB, uri=True)
cur = con.cursor()

tables = [r[0] for r in cur.execute(
    "select name from sqlite_master where type='table' order by name")]
print('TABLES:', tables)

for t in tables:
    print('=' * 20, t)
    try:
        cols = [r[1] for r in cur.execute('pragma table_info(%s)' % t)]
        print('  cols:', cols)
        n = cur.execute('select count(*) from %s' % t).fetchone()[0]
        print('  rows:', n)
        for row in cur.execute('select * from %s limit 6' % t):
            d = dict(zip(cols, row))
            out = {}
            for k, v in d.items():
                if isinstance(v, (bytes, bytearray)):
                    out[k] = '<%d bytes>' % len(v)
                elif isinstance(v, str) and len(v) > 90:
                    out[k] = v[:90] + '...'
                else:
                    out[k] = v
            print('   ', json.dumps(out)[:420])
    except Exception as e:
        print('  ERR', e)

# the shape of what qualification checks: any row that is not settled?
for t in tables:
    try:
        cols = [r[1] for r in cur.execute('pragma table_info(%s)' % t)]
        for key in ('state', 'status', 'acked', 'acknowledged', 'settled', 'phase', 'reason'):
            if key in cols:
                rows = list(cur.execute('select %s, count(*) from %s group by %s' % (key, t, key)))
                print('  %s.%s breakdown: %s' % (t, key, rows[:8]))
    except Exception as e:
        pass
con.close()

print('=' * 20, 'owner control-sequence.json')
print(open(os.path.expanduser('~/mycelium-wp8-02/owner-lhr/local-contributor/control-sequence.json')).read()[:600])

print('=' * 20, 'owner own pump state for this run')
p = os.path.expanduser('~/mycelium-wp8-02/owner-lhr/local-contributor/managed/a13-qwen-20260919T152639Z/pump/state.json')
print(open(p).read()[:900] if os.path.exists(p) else 'ABSENT')
print('=' * 20, 'owner managed dir for this run')
d = os.path.dirname(p)
print(sorted(os.listdir(d)) if os.path.isdir(d) else 'ABSENT')
