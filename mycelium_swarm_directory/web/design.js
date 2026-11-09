// SPDX-License-Identifier: AGPL-3.0-only
/** Visual layer only: no directory API, keys, grants, prompts or route writes. */
import { drawSpore } from "./spore-print.js";
import { createHero } from "./mycelium-hero.js";
import { createField } from "./mycelium-field.js";
import { INTRO_MS, STAGING_INTRO_KEY } from "./mycelium-geometry.js";

const stage = document.body;
const reduce = matchMedia("(prefers-reduced-motion: reduce)");
const scheme = matchMedia("(prefers-color-scheme: light)");
const state = {
  glow: document.documentElement.dataset.glow || "green",
  theme: scheme.matches ? "light" : "dark",
  style: "mycelium",
};
// STAGING ONLY: the server injects [data-bg-choice] buttons only on
// next.mycelium.now with staging samples; without them the r9 "electric"
// renderer is never imported (and not served).
const STYLE_KEY = "mycelium-staging-bg-style-v1";
const styleButtons = [...document.querySelectorAll("[data-bg-choice]")];
if (styleButtons.length)
  try {
    if (localStorage.getItem(STYLE_KEY) === "electric") state.style = "electric";
  } catch {}
let electric = null,
  generation = 0;
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
function mount(renderer) {
  if (hero) disposeHero = renderer.createHero(hero, { stage, state, reduce });
  disposeField = renderer.createField({ stage, state, reduce });
  paintPrints();
}
function rebuild() {
  // Exactly one renderer at a time: tear down before (possibly async) mounting.
  disposeHero();
  disposeField();
  disposeHero = disposeField = () => {};
  // The r9 (staging) hero leaves helper layers beside #hyphae; drop any leftovers.
  for (const c of document.querySelectorAll(".hero-art canvas:not(#hyphae)")) c.remove();
  const gen = ++generation;
  window.__myceliumScene = undefined; // no renderer may attach to a disposed scene
  stage.dataset.theme = state.theme;
  for (const button of document.querySelectorAll("[data-glow-choice]"))
    button.setAttribute(
      "aria-pressed",
      String(button.dataset.glowChoice === state.glow),
    );
  for (const button of styleButtons)
    button.setAttribute("aria-pressed", String(button.dataset.bgChoice === state.style));
  if (styleButtons.length && state.style === "electric") {
    document.documentElement.dataset.bgStyle = "electric";
    electric ??= Promise.all([import("./mycelium-hero-electric.js"), import("./mycelium-field-electric.js")]).then(([h, f]) => ({
      createHero: h.createHero,
      createField: f.createField,
    }));
    electric.then((renderer) => gen === generation && mount(renderer)).catch(() => {
      electric = null;
      if (gen === generation) {
        state.style = "mycelium";
        rebuild();
      }
    });
    return;
  }
  delete document.documentElement.dataset.bgStyle;
  mount({ createHero, createField });
}
// STAGING ONLY: load-in duration comparison (6 / 9 / 12 s). The server injects
// [data-intro-choice] only on the staging home page. A choice is stored under
// the staging-only key and replays the load-in at its exact ?intro= target;
// the duration itself is read once, in mycelium-geometry.js.
for (const button of document.querySelectorAll("[data-intro-choice]")) {
  const ms = +button.dataset.introChoice * 1000;
  button.setAttribute("aria-pressed", String(ms === INTRO_MS));
  button.addEventListener("click", () => {
    try {
      localStorage.setItem(STAGING_INTRO_KEY, String(ms));
    } catch {}
    location.assign("/?intro=" + button.dataset.introChoice);
  });
}
for (const button of styleButtons)
  button.addEventListener("click", () => {
    if (state.style === button.dataset.bgChoice) return;
    state.style = button.dataset.bgChoice === "electric" ? "electric" : "mycelium";
    try {
      localStorage.setItem(STYLE_KEY, state.style);
    } catch {}
    rebuild();
  });
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
