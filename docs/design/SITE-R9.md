# R9 site visual layer

This port uses the approved `mycelium-now-prototype-r9.html` renderer, separated
from its sample-data UI. Production copy and application behavior come from
`86758539b6acb5acb2a33771d70e096b1a8c6574`, not from prototype SOURCE blocks.
No protocol, heartbeat, grant, API, directory state or inference change is intended.

## Asset boundaries

- `theme.js`: blocking, read-only preference bootstrap. Green is the new default.
  Only an explicit choice writes `mycelium-site-glow-v1`; no visitor identity.
- `design.css`: R9 palette/type/background and decorative identity slots. Existing
  `site.css`/`swarms.css` retain their reserved asynchronous content boxes.
  Per-turn chat measurements additionally reserve a keyboard-scrollable six-line
  box before entry responses arrive. Delayed real send/event responses previously
  moved the following served-by line; the new regression holds both responses
  beyond the recent-input window and requires exact-zero CLS in all four cases.
  Owner request summaries likewise reserve two lines and remain keyboard-scrollable;
  a status/long-note update cannot move the held-decision slot. Desktop owner panels
  include the additional reserved line, keeping the complete hold explanation visible.
- `design.js`: owns palette/media changes, renderer disposal and swarm print paints.
  It does not fetch, route, publish, sign or send prompts.
- `mycelium-geometry.js`: deterministic aperiodic walks, guarded bridges and
  swarm-only gills. Prints seed on the stable swarm ID, not an owner's browser key.
- `mycelium-hero.js`: seeded segment graph, multi-hop Dijkstra arrival bands,
  LRU-bounded full-DPR rasters, fractional live tips, visibility/reduced-motion
  lifecycle. The owner delta appends a separately seeded explorer cohort without
  changing the approved geometry prefix or adding colonies.
- `mycelium-field.js`: viewport-sized, node-free field plus cached tile; independent
  from hero intersection. Reduced motion also disables parallax.

Both the directory server and Profile A proxy explicitly name all assets. No
wildcard asset routing, inline script, CDN, external font or history workaround.
Diagnostic `__*` hooks expose only procedural geometry/timing, never app data.
They permit deterministic qualification but are not display-frame telemetry.

## Qualification

Use the existing real loopback directory/entry browser fixtures. Their generation
backend is scripted, not a physical swarm. The requested full suite runs serially
with `env -u PYTHONPATH nice -n 15 <test-python> -m pytest tests/swarm_directory`,
plus Profile A exact asset/closed route tests. Use a lane-unique scratch basetemp.

Private rollout evidence and promotion/rollback instructions belong in
`~/Projects/mycelium-m4-program/site-r9-evidence/` and `SITE-R9-PROMOTION.md`, not in
public site copy. Production promotion requires separate owner approval. Staging
must remain a disposable copy of the directory, never a destination for owners.
