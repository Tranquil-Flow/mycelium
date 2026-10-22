# Demo-route copy inspection

These are **loopback fixtures**, not observations of the public demo. The shipped page reads a real local directory containing owner-signed records/heartbeats. A second, unrelated serving listing deliberately contributes seven Macs to the global count, so a demo count cannot accidentally come from the directory-wide sum. No demo entry is contacted.

All twelve full-page screenshots were inspected directly with vision:

- `apex-one-mac-{1440,390}-{dark,light}.png`
- `apex-two-mac-{1440,390}-{dark,light}.png`
- `apex-unavailable-{1440,390}-{dark,light}.png`

Each PNG has a same-stem JSON with the rendered note, bounding box and layout-shift observation. All twelve saved CLS values are exactly **0**, with empty source-attribution lists. The browser test also checks the route-note box is identical across one, two, 64, offline, warming, unknown-count, ambiguous and unavailable states, with delayed real server responses. No threshold relaxation or omitted dark/mobile cases.

Visual findings:

- One Mac says it is not a distributed swarm; two Macs says it runs across two. Both identify the listing as the report source and warn that availability may change.
- Unavailable uses neutral hosting copy and dashes for unavailable global counts, not a fabricated zero or one-Mac claim.
- Desktop route copy fits above a stable Right now panel. Mobile wraps naturally inside the reserved slot; neither text nor buttons clip or overlap.
- The three hero actions remain visible (Download wraps to a second row on mobile), and the status band, explainer cards, privacy copy and footer retain consistent positioning across states.
- Light and dark contrast remains legible. The neutral note does not claim the demo is currently serving. Global totals of eight/nine in the one/two-Mac fixtures are deliberate, not a mismatch: the band is explicitly labelled as the sum across all listings.

The older apex screenshots one directory up are retained historical evidence from the preceding P3 qualification, not the updated copy.
