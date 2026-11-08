// SPDX-License-Identifier: AGPL-3.0-only
/* Small blocking initializer prevents a palette flash. Browsing never writes storage. */
(() => {
  let glow = "green";
  try {
    if (localStorage.getItem("mycelium-site-glow-v1") === "blue") glow = "blue";
  } catch {}
  document.documentElement.dataset.glow = glow;
})();
