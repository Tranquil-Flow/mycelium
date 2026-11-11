// SPDX-License-Identifier: AGPL-3.0-only
/* Small blocking initializer prevents a palette flash. Browsing never writes
   storage: the stored theme is only read here, and only an explicit click in
   design.js writes it. No stored choice means follow the OS. */
(() => {
  let glow = "green";
  try {
    if (localStorage.getItem("mycelium-site-glow-v1") === "blue") glow = "blue";
  } catch {}
  document.documentElement.dataset.glow = glow;
  let theme = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  try {
    const stored = localStorage.getItem("mycelium-site-theme-v1");
    if (stored === "light" || stored === "dark") theme = stored;
  } catch {}
  document.documentElement.dataset.theme = theme;
})();
