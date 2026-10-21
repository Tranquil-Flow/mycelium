// SPDX-License-Identifier: AGPL-3.0-only
'use strict';
// Public phonebook data only: no key, storage, prompt, entry request or analytics.
(async () => {
  const label = document.getElementById('apex-fresh');
  try {
    const response = await fetch('/swarms/api/v1/swarms', {credentials:'omit', redirect:'error', referrerPolicy:'no-referrer', cache:'no-store', signal:AbortSignal.timeout(10000)});
    if (!response.ok) throw new Error('unavailable');
    const value = await response.json();
    if (!Array.isArray(value.swarms)) throw new Error('invalid');
    const live = value.swarms.filter(s => ['serving','warming'].includes(s.status));
    if (live.some(s => !Number.isSafeInteger(s.members) || s.members < 0)) throw new Error('invalid');
    document.getElementById('serving-count').textContent = live.filter(s => s.status === 'serving').length;
    document.getElementById('mac-count').textContent = live.reduce((n,s) => n+s.members,0);
    // Match the actual demo destination, never its mutable display name or the
    // directory-wide sum. Offline duplicates do not describe the serving route.
    // Multiple serving listings are ambiguous: retain the neutral HTML fallback.
    const demoOrigin = new URL(document.getElementById('demo-chat').href).origin;
    const demo = value.swarms.filter(s => s.entry_origin === demoOrigin && s.status === 'serving');
    if (demo.length === 1 && Number.isSafeInteger(demo[0].members) && demo[0].members > 0 && demo[0].members <= 64) {
      const count = demo[0].members;
      document.getElementById('demo-route').textContent = (count === 1
        ? 'The demo route runs on one Mac, not a distributed swarm.'
        : `The demo route runs across ${count} Macs.`) + ' Reported by its listing; availability may change.';
    }
    label.textContent = 'Directory snapshot just loaded. Refresh for a new snapshot.';
  } catch (_) {
    label.textContent = 'Status unavailable. Try an entry link to reach a swarm directly.';
  }
})();
