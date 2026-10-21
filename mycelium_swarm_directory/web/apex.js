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
    label.textContent = 'Directory snapshot just loaded. Refresh for a new snapshot.';
  } catch (_) {
    label.textContent = 'Status unavailable. Swarms still work if you have their entry link.';
  }
})();
