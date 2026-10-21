# SPDX-License-Identifier: AGPL-3.0-only
import json, os, time, glob

RUN = 'a13-qwen-20260919T152639Z'
base = os.path.expanduser('~/mycelium-wp8-02/owner-lhr/local-contributor/managed/') + RUN


def ts(v):
    try:
        return time.strftime('%m-%d %H:%M:%S', time.localtime(float(v)))
    except Exception:
        return str(v)


print('=== owner local-contributor managed dir for this run ===')
print('exists:', os.path.isdir(base))
if os.path.isdir(base):
    for name in sorted(os.listdir(base)):
        p = os.path.join(base, name)
        st = os.stat(p)
        print('  %-28s %9d  %s' % (name, st.st_size, ts(st.st_mtime)))

print()
print('=== native-exit.private.json on the OWNER node (the node whose configure went unanswered) ===')
p = os.path.join(base, 'native-exit.private.json')
if os.path.exists(p):
    st = os.stat(p)
    print('  mtime', ts(st.st_mtime), st.st_size, 'bytes')
    print(' ', json.dumps(json.load(open(p)), indent=1)[:900])
else:
    print('  ABSENT')

print()
print('=== native-failure / pump records on the owner node ===')
for pattern in ('native-failure.private.json', 'pump/state.json', 'pump/concurrent.json', 'uncertain-ack.private.json'):
    q = os.path.join(base, pattern)
    if os.path.exists(q):
        print('--', pattern, 'mtime', ts(os.stat(q).st_mtime))
        try:
            print(' ', json.dumps(json.load(open(q)), indent=1)[:700])
        except Exception as e:
            print('  ERR', e)
    else:
        print('--', pattern, 'ABSENT')

print()
print('=== every native-exit record anywhere under the owner root, newest first ===')
hits = []
for q in glob.glob(os.path.expanduser('~/mycelium-wp8-02/owner-lhr/**/native-exit.private.json'), recursive=True):
    hits.append((os.stat(q).st_mtime, q))
for m, q in sorted(hits)[-6:]:
    print('  %s  %s' % (ts(m), q.replace(os.path.expanduser('~/mycelium-wp8-02/owner-lhr/'), '')[:88]))
print('  total:', len(hits))
