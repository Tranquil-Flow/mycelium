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
    const reporting = live.filter(s => s.status === 'serving' && s.stats?.stale === false && s.stats.values);
    const rates = reporting.map(s => s.stats.values.decode_millitokens_s).filter(m => m?.n > 0 && Number.isSafeInteger(m.p50));
    if (rates.length) document.getElementById('fastest-speed').textContent = (Math.max(...rates.map(m => m.p50))/1000).toFixed(1);
    if (reporting.length) document.getElementById('served-count').textContent = String(reporting.reduce((n,s) => n+s.stats.values.requests_24h,0));
    document.getElementById('stats-coverage').textContent = `${reporting.length} reporting swarm${reporting.length === 1 ? '' : 's'}. Self-reported app-chat counts; partial 24 h coverage after restart. Listings may overlap. No visitor counts.`;
    // Match the actual demo destination, never its mutable display name or the
    // directory-wide sum. Offline duplicates do not describe the serving route.
    // Multiple serving listings are ambiguous: retain the neutral HTML fallback.
    const demoChat = document.getElementById('demo-chat');
    const demoURL = 'https://demo.mycelium.now/chat';
    const demo = value.swarms.filter(s => s.entry_origin === new URL(demoURL).origin && s.status === 'serving');
    if (demo.length === 1) {
      demoChat.href = demoURL;
      demoChat.textContent = 'Try the demo chat';
      document.getElementById('demo-route').textContent = 'The demo is volunteer-hosted and may be unavailable. Check its listing for current serving devices.';
    }
    if (demo.length === 1 && Number.isSafeInteger(demo[0].members) && demo[0].members > 0 && demo[0].members <= 64) {
      const count = demo[0].members;
      document.getElementById('demo-route').textContent = (count === 1
        ? 'The listing reports one device; a swarm needs at least two devices.'
        : `The demo route runs across ${count} devices.`) + ' Reported by its listing; availability may change.';
    }
    label.textContent = 'Directory snapshot just loaded. Refresh for a new snapshot.';
  } catch (_) {
    label.textContent = 'Status unavailable. Try an entry link to reach a swarm directly.';
  }
})();
