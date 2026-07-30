# Mycelium — Continuing From evis-macbook-pro

## STATUS as of 2026-07-22 06:15 (read first)

- **Phase 0: complete.** Docs cleaned, README rewritten, duplicate worktree/branch removed.
- **Phase 1: complete.** Model sources, N-node split, GPT-2 BPE tokenizer, coherent
  generation (verified: "Hello, how are you?" → "Good morning everyone!"), N-way token parity.
- **Phase 2 Task 2.1: complete.** Invite tokens (`mycelium_invite/`). Signed with the
  repo's `Ed25519EvidenceSigner` — `mint_invite(*, signer=...)`, `verify_invite(*, verifier_key_records=...)`.
- **Next up: Tasks 2.2 (node agent), 2.3 (seed coordinator), 2.4 (shard transport), then Phase 3 (NumPy).**
  The plan's interface blocks for 2.2/2.3 are corrected for the signer-object API.
- **HEAD:** `88688f5`, all pushed to `origin/integration/mycelium-live-demo`.
- **Overnight cron is PAUSED** (`hermes cron resume f3744421e1ff` to restart it, but you're
  driving from the laptop now so you likely don't need it).
- **Known flaky test:** `test_router_iroh_transport.py::test_reconnect_retry_uses_original_end_to_end_deadline`
  fails only under full-suite load (timer jitter); passes in isolation. Not a regression.

---


## First: get the work onto this laptop

```bash
# If you don't have the repo yet:
git clone https://github.com/Tranquil-Flow/mycelium.git ~/Projects/mycelium
cd ~/Projects/mycelium

# If you already have it:
cd ~/Projects/mycelium && git fetch origin

# Either way, get the demo branch:
git checkout integration/mycelium-live-demo
git pull origin integration/mycelium-live-demo
```

Then check what the overnight run accomplished:

```bash
git log --oneline -30
cat QUESTIONS.md 2>/dev/null || echo "no open questions"
python3.14 -m pytest -q 2>&1 | tail -5
```

Expected baseline if all went well: `1 failed, 1821+ passed`. The one permitted
failure is `tests/claims/test_claim_boundary_audit.py` — pre-existing, see the plan.

## You also need

- **The plan** — it lives at `~/Desktop/mycelium-demo-plan.md` **on the m4pro**, not
  in git (plans are gitignored). Copy it to this laptop before you leave, or fetch
  it over Tailscale: `scp m4pro:~/Desktop/mycelium-demo-plan.md ~/Desktop/`
- **The DialoGPT snapshot** if you want to run model tests locally:
  `~/.cache/huggingface/hub/models--microsoft--DialoGPT-small/` (~351MB).
  Tests skip cleanly without it, so this is optional for code work.
- **Python 3.14 + MLX** on this laptop for anything touching the runtime.

---

## Prompt for GPT-5.6-sol (Hermes)

Copy everything below into a fresh Hermes session.

---

You are continuing work on Mycelium, a distributed-inference system that shards a
language model across heterogeneous devices and routes activations between them.

**Read `~/Desktop/mycelium-demo-plan.md` in full before doing anything.** It is the
single authoritative plan. It supersedes every document in `docs/automation/` and
`docs/plans/` — those are historical snapshots that contradict each other and each
other's successors. Do not treat them as current.

**Working directory:** `~/Projects/mycelium` on branch `integration/mycelium-live-demo`

**Where things stand:** an overnight run worked through the plan's phases. Start by
reading `git log --oneline -30` and `QUESTIONS.md` to see what got done and what got
stuck. `QUESTIONS.md` holds design questions the overnight agent deliberately refused
to guess at — resolving those is high-value work.

**What this project is:** Mycelium's components (Planner, Router, Gossip, Broadcast,
Provisioner, Runtime) are individually built and well-tested but were never connected
to each other. The work is integration, not construction. The plan's Phase 2 closes
the loop: `announce → GET /nodes → planner → assignment → provisioning → node agent →
Router → tokens`.

**Design authority.** These documents are authoritative for their subsystems, and you
must read the relevant one before touching that subsystem:
- `REQUEST_AND_INTER_LAYER_ROUTER_DESIGN.md` — Router, including 11 non-negotiable invariants
- `FAULT_TOLERANT_LAYER_REPLANNER.md` — failure handling and the five-control-loop authority split
- `GOSSIP_PROTOCOL.md` — evidence plane, identity lifecycle
- `BROADCAST_PROTOCOL.md` — peer join and discovery
- `ALLOCATOR.md` / `LAYER_PLANNER_PRODUCT_V1.md` — capacity-aware layer assignment

Do not invent parallel subsystems. Nearly everything you need already exists.

**Explicitly out of scope:** do not wire `planner_simulator.py` into the runtime path
(the Router doc explains why its `stage_signature` is insufficient). Do not try to
make the qualification machinery gate anything — it stays passing but is not a gate.
Do not attempt to prove inference correctness; we are getting it working.

**How to work:**
- TDD. Write the failing test, run it, confirm it fails, implement, confirm it passes, commit.
- `python3.14 -m pytest -q` before every commit. The known-failing baseline is documented
  in the plan — do not treat those as your regressions.
- `ruff check` on changed files before staging.
- Commit format `type: description`. No parenthetical scope. No Co-Authored-By trailers.
  Never mention agents, plans, or handovers in commit messages.
- Never merge to main. Never force-push. Never rewrite history.
- Verify before claiming. Never say a task is done without showing passing test output.

**If you hit a design question the plan doesn't answer, ask me. Don't guess.**

---

## Note on the two-device demo

Plan Task 2.5 — the actual two-machine run — needs both Macs. The m4pro is at home
and has been left awake on Tailscale (`100.84.252.4`). Once it has internet again,
laptop + m4pro over Tailscale *is* the cross-network two-device demo. Check with:

```bash
tailscale status | grep m4pro
```

If it shows `active`, you can attempt Task 2.5 remotely. If it's offline, everything
up to 2.4 is still buildable on the laptop alone.
