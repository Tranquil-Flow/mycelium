// SPDX-License-Identifier: AGPL-3.0-only
/** Visual layer only: no directory API, keys, grants, prompts or route writes. */
import { drawSpore } from "./spore-print.js";
import { createHero } from "./mycelium-hero.js";
import { createField } from "./mycelium-field.js";

const stage = document.body;
const reduce = matchMedia("(prefers-reduced-motion: reduce)");
const scheme = matchMedia("(prefers-color-scheme: light)");
const state = {
  glow: document.documentElement.dataset.glow || "green",
  theme: scheme.matches ? "light" : "dark",
};
let disposeHero = () => {},
  disposeField = () => {},
  timer,
  size = "";
const hero = document.getElementById("hyphae");

function paintPrints() {
  for (const canvas of document.querySelectorAll("canvas[data-print]")) {
    const rect = canvas.getBoundingClientRect();
    const key = [
      canvas.dataset.print,
      rect.width,
      rect.height,
      devicePixelRatio,
    ].join("|");
    if (!rect.width || !rect.height || canvas.dataset.painted === key) continue;
    drawSpore(canvas, canvas.dataset.print, {
      size: Math.min(rect.width, rect.height),
    });
    canvas.dataset.painted = key;
  }
}
function rebuild() {
  disposeHero();
  disposeField();
  stage.dataset.theme = state.theme;
  for (const button of document.querySelectorAll("[data-glow-choice]"))
    button.setAttribute(
      "aria-pressed",
      String(button.dataset.glowChoice === state.glow),
    );
  if (hero) disposeHero = createHero(hero, { stage, state, reduce });
  disposeField = createField({ stage, state, reduce });
  paintPrints();
}
for (const button of document.querySelectorAll("[data-glow-choice]"))
  button.addEventListener("click", () => {
    state.glow = button.dataset.glowChoice;
    document.documentElement.dataset.glow = state.glow;
    try {
      localStorage.setItem("mycelium-site-glow-v1", state.glow);
    } catch {}
    rebuild();
  });
scheme.addEventListener("change", () => {
  state.theme = scheme.matches ? "light" : "dark";
  rebuild();
});
window.addEventListener("storage", (e) => {
  if (e.key !== "mycelium-site-glow-v1") return;
  state.glow = e.newValue === "blue" ? "blue" : "green";
  document.documentElement.dataset.glow = state.glow;
  rebuild();
});
const observer = new MutationObserver(paintPrints);
observer.observe(document.getElementById("main"), {
  childList: true,
  subtree: true,
  attributes: true,
  attributeFilter: ["data-print", "hidden"],
});
window.addEventListener("resize", () => {
  clearTimeout(timer);
  timer = setTimeout(() => {
    const rect = hero?.getBoundingClientRect();
    const next = [innerWidth, innerHeight, rect?.width, rect?.height, devicePixelRatio].join("|");
    if (next !== size) {
      size = next;
      rebuild();
    } else paintPrints();
  }, 180);
});
window.addEventListener("pagehide", () => {
  disposeHero();
  disposeField();
  clearTimeout(timer);
});
window.addEventListener("pageshow", (e) => {
  if (e.persisted) rebuild();
});
rebuild();
