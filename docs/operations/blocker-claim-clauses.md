# Blocker claim clauses — how a checklist blocker can be *checked*, not just well-formed

**Audience:** anyone authoring or reviewing a gate blocker in
`docs/handover/mycelium-completion-checklist.v1.json`.
**Authoritative source:** the module docstring and constants in
`scripts/mycelium_completion_audit.py` (`BLOCKER_CLAIM_MARKER`, `BLOCKER_CLAIM_ARITY`, `GIT_CLAIM_KINDS`).
If this page and that script ever disagree, **the script wins** — this page exists so authors can write valid
clauses without reading the implementation.

## Why this exists

The review finding **F1** was that the checklist's *form* being valid did not show the checklist was *true*: a
stale blocker passed the audit because the file was well-formed. The A13 blocker went stale twice the same
way. A blocker may now carry an optional clause naming the artifact it is about, and **a clause that is
written down must parse and must hold.**

## Grammar

```
[[claim:<kind> <argument>[ <argument>]]]
```

| kind | arity | holds when |
|---|---|---|
| `path_exists <path>` | 1 | a directory entry exists at `<path>` |
| `path_absent <path>` | 1 | no directory entry exists at `<path>` |
| `marker_present <path> <token>` | 2 | the regular file at `<path>` contains `<token>` |
| `marker_absent <path> <token>` | 2 | the regular file at `<path>` does not contain `<token>` |
| `commit_present <sha>` | 1 | the commit is in the local object database |
| `commit_reachable <sha>` | 1 | the commit is HEAD or an ancestor of HEAD |
| `commit_unreachable <sha>` | 1 | the commit exists and is neither HEAD nor an ancestor |

## Semantics you must know before writing one

- **A clause is optional.** Blockers are also free prose. **A blocker carrying no clause is not checked at
  all**, so *a green audit does not mean every blocker is true — it means every clause that was written down
  holds.* Write clauses for the claims a reader would otherwise have to take on trust.
- **Contradicted, undecidable and malformed clauses are findings**, reported as
  `gate:<id>:blocker:<index>:claim_contradicted|claim_unresolved|claim_invalid`.
- **`None` is not a pass.** A clause the audit cannot decide is `claim_unresolved`, not satisfied.
- **Paths are repository-relative inside the audited root**, and a claim never reaches outside it.
- **Markers compare against the file's whitespace-normalized text.**
- **Git clauses are answered by the work tree that contains the audited root.** A root that is not a Git work
  tree with a readable HEAD makes them **undecidable rather than true**.

## Limits, stated so nobody over-reads a green run

It catches **contradiction with named artifacts**, not every kind of stale claim · a blocker naming **no**
artifact is uncheckable by construction · and a green audit is still **not a closure record**.
