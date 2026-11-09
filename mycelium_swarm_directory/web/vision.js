// SPDX-License-Identifier: AGPL-3.0-only
/* Vision page: the Simple | Technical switch. Loaded blocking in <head> so the
   chosen panel is set before first paint (no flash, no layout shift). Without
   JavaScript both versions are shown with their own headings. The choice is
   remembered under "vision-mode" and mirrored in the URL (#technical). */
(() => {
  const KEY = "vision-mode";
  const MODES = ["simple", "technical"];
  const root = document.documentElement;
  const fromHash = () => {
    const h = location.hash.slice(1);
    if (h === "technical" || h === "panel-technical") return "technical";
    if (h === "simple" || h === "panel-simple") return "simple";
    return null;
  };
  let stored = null;
  try {
    stored = localStorage.getItem(KEY);
  } catch {}
  let mode = fromHash() || (stored === "technical" ? "technical" : "simple");
  root.dataset.visionMode = mode;
  // Keep the address shareable; never let a no-JS panel anchor scroll the page.
  const syncURL = () => {
    const want = mode === "technical" ? "#technical" : location.hash === "#simple" ? "#simple" : "";
    if (location.hash === want) return;
    try {
      history.replaceState(history.state, "", location.pathname + want);
    } catch {}
  };
  syncURL();

  function apply(next, { persist = false, focus = false } = {}) {
    if (!MODES.includes(next)) return;
    mode = next;
    root.dataset.visionMode = mode;
    for (const tab of document.querySelectorAll('[role="tab"][data-mode]')) {
      const on = tab.dataset.mode === mode;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      if (on && focus) tab.focus();
    }
    if (persist)
      try {
        localStorage.setItem(KEY, mode);
      } catch {}
    syncURL();
  }

  document.addEventListener("DOMContentLoaded", () => {
    const tabs = [...document.querySelectorAll('[role="tab"][data-mode]')];
    apply(mode);
    tabs.forEach((tab, i) => {
      tab.addEventListener("click", () => apply(tab.dataset.mode, { persist: true }));
      tab.addEventListener("keydown", (e) => {
        let j = null;
        if (e.key === "ArrowRight" || e.key === "ArrowDown") j = (i + 1) % tabs.length;
        else if (e.key === "ArrowLeft" || e.key === "ArrowUp") j = (i - 1 + tabs.length) % tabs.length;
        else if (e.key === "Home") j = 0;
        else if (e.key === "End") j = tabs.length - 1;
        if (j === null) return;
        e.preventDefault();
        apply(tabs[j].dataset.mode, { persist: true, focus: true });
      });
    });
    // "Read the technical version →": switch, then bring the switch into view.
    for (const link of document.querySelectorAll("[data-mode-link]"))
      link.addEventListener("click", (e) => {
        e.preventDefault();
        apply(link.dataset.modeLink, { persist: true });
        const box = document.getElementById("mode-switch");
        const header = document.querySelector("header.top");
        const top = box.getBoundingClientRect().top + scrollY - (header ? header.offsetHeight : 0) - 16;
        scrollTo({ top: Math.max(0, top), behavior: "instant" });
        const tab = document.querySelector('[role="tab"][aria-selected="true"]');
        tab?.focus({ preventScroll: true });
      });
  });
  // Back/forward or an edited address: follow the hash, without storing it.
  addEventListener("hashchange", () => {
    const next = fromHash();
    if (next && next !== mode) apply(next);
  });
})();
