# SPDX-License-Identifier: AGPL-3.0-only
import glob, os, re

D = os.path.expanduser('~/Documents/mycelium-core-program/runs/wp8-02-useful-two-mac')
print('%-36s %-8s %-14s %s' % ('round log', 'qual?', 'bootid-refusal', 'cause_chain'))
for f in sorted(glob.glob(os.path.join(D, 'round*owner-daemon.log'))):
    text = open(f, errors='replace').read()
    qual = 'qualified_runtime' in text
    chain = ''
    m = re.search(r'"cause_chain":(\[.*?\])', text)
    if m:
        chain = m.group(1)[:120]
    bid = ''
    m2 = re.search(r'boot_id_observation_unavailable\s+(\S+)', text)
    if m2:
        bid = m2.group(1)[:34]
    print('%-36s %-8s %-14s %s' % (os.path.basename(f)[:36], 'YES' if qual else '-',
                                   bid or '-', chain or '-'))
