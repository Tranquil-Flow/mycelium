// SPDX-License-Identifier: AGPL-3.0-only
/** Mycelium living background: the same renderer the website uses.
 *  Dark Green only for now: the app keeps one palette, so no hue or theme switch. */
import { createHero } from "./mycelium-hero.js";
import { createField } from "./mycelium-field.js";

const stage = document.body;
const reduce = matchMedia("(prefers-reduced-motion: reduce)");
const state = {glow: "green", theme: "dark", style: "mycelium"};
stage.dataset.theme = "dark";

const hero = document.getElementById("hyphae");
if (hero) {
  try {
    createHero(hero, {stage, state, reduce});
  } catch (error) {
    console.error("hero_failed", error);
  }
}
try {
  createField({stage, state, reduce});
} catch (error) {
  console.error("field_failed", error);
}
