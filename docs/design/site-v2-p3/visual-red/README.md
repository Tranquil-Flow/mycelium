# Visual RED: held owner decision

At implementation/test revision `d1f0f8fa`, the browser behavior assertions were green, but direct screenshot inspection found that the narrower Undo button overlaid only part of the disabled Approve button. The trailing letter and background of Approve remained visible. `undo-overlap-before.png` preserves the actual observed screenshot, not a mock.

The correction keeps Approve's layout box reserved while hiding it during a held decision. Undo stays at the clicked action. Mobile inspection also found that the hold explanation fell below the request panel's scroll boundary; the mobile panel now reserves enough room and a geometry assertion covers the explanation. `mobile-hold-red.*` / `mobile-hold-green.*` retain that regression.

When the complete browser gate was strengthened to **CLS == 0**, both mobile themes exposed one further shift: the decision-status SPAN vertically recentered when the two-line hold text became one-line “Saved.” after five seconds (CLS `0.0004247234919101847`). `browser-zero-1.*` retains the two failures. Giving the status text its own fixed-height box removes the movement; `owner-zero-green.*` verifies both viewports/themes, including a 700 ms observer-settling wait after the actual saved response. Final screenshots and three complete browser runs are regenerated after these corrections.
