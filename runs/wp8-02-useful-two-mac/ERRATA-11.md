# ERRATA-11 — there is no evidence of within-run reconnect churn; the "22 incarnations in one run" was a reading of a 23-round lineage

**Written 2026-09-19 late, at head `222a035`, from `pump/state.json` for all 23 rounds on disk.**
Retracts a statement in `status/decisions.log` and in `GOAL-prompt-finish-pilot-v3.md`'s W5 row:
*"pump/state.json shows membership_generation 23 with member incarnation …r1…r22 inside a single run — the
reconnect churn the earlier RCA made an offline gate."*

## What the file actually holds

`member_incarnation` is a **dotted chain**, not a counter. Read from
`~/mycelium-wp8-02/owner-lhr/local-contributor/managed/<run>/pump/state.json` for every round on disk
(each run dir is one round):

| round | run starts | `membership_generation` | `r`-segments in the chain | chain tail |
|---|---|---|---|---|
| 1 | 2026-09-16T165855Z | 1 | **0** | — |
| 2 | 2026-09-16T173439Z | 2 | 1 | `r1` |
| 3 | 2026-09-16T181442Z | 3 | 2 | `r1.r2` |
| … | | | *(gen = segments + 1, exactly, every row)* | |
| 22 | 2026-09-19T091223Z | 22 | 21 | `r20.r21` |
| 23 | 2026-09-19T120607Z (**p74r1**) | 23 | 22 | `r21.r22` |

Two facts follow, and neither supports the recorded claim:

1. **Exactly one re-admission per round.** `generation` increments by 1 and the chain grows by exactly 1
   segment, per round, across all 23 rounds. Max segments in any single run = `generation - 1`; no run's chain
   is longer than its generation. There is no run in which the chain grew more than once.
2. **The chain is cumulative across rounds, not within one.** Each round has its own run dir and its own
   `state.json`, yet round *N*'s chain carries *N−1* segments — i.e. it holds the whole lineage of prior
   deployments. p74r1's "`…r1…r22`" is p74r1's *position as the 23rd round*, not 22 events inside p74r1.

A representative full chain, verbatim (round 9, gen 9):
`remote-incarnation-08da30e817560b4c.r1.r2.r3.r4.r5.r6.r7.r8`.

## Why this happened (third instance of the same error family)

The string `…r1.r2.r3.r4…` reads visually like a burst: many incarnation suffixes on one line look like many
events. But the *line* is a lineage, and only its **length per run** is evidence of within-run activity — and
that length is 1. I read a cumulative identifier as a per-run event log. Same family as ERRATA-6/8/9: a
summary-shaped object accepted in place of the artifact's own semantics.

## What is retracted, and what is NOT

**Retracted:** that p74r1 (or any round) exhibits within-run reconnect churn; that `membership_generation 23`
with a long incarnation chain is a churn signal. It is the 23rd deployment's lineage position.

**NOT retracted — and this is the part that survives, correctly measured:** the churn *question* is open and
**half-measured**, exactly as `scripts/a13_operator/churn_gate.py` already says in its own output. The gate
passes the owner-side invariant (generation advances by exactly one per round; depth == generation−1 on every
round), then prints:

```
restart_count sightings in the owner tree      : 0
seed_http_unreachable sightings                : 0
-> both live on the TARGET (laptop) side. This gate cannot see them: a round must
   capture the target's last-action-error, or the churn claim stays half-measured.
```

So the owner side is *boring, in the good way*: one re-admission per round, no churn to explain. Whether the
**target** churns (its `restart_count`, its `seed_http_unreachable`) was never measured, because those
signals are not collected into any round artifact. That absence is what W5 must close — and the gate is
already honest that it cannot close it from here.

## Consequence for W5 and the plan

W5's premise changes shape. It is **not** "make the churn stop"; on the owner side there is nothing to stop.
It is:

1. **collect** the target's `last-action-error` (and `restart_count`) into the round dir, so the gate's own
   two blind lines become checkable instead of printed;
2. then decide whether a target-side re-admission per round is normal deployment behaviour or a defect.

Step 1 is offline work that does not need a physical round to write the collector — only to run it — so it
joins the pre-round queue rather than blocking behind it.

## The law this adds

**A cumulative identifier read as an event log invents activity.** Before treating a repeated-suffix chain,
sequence number, or generation counter as evidence of events, establish whether it *resets per unit* or
*accumulates across units*. Here the unit was the round and the chain accumulated; the per-round delta was
the only honest measure, and it was 1.
