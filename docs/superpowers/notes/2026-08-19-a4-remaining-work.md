A4 REMAINING WORK — exact state + next steps (2026-08-19 evening)
==========================================================

## DONE (all deterministic, green)
- a4_install.py architecture (in-session qualification build) — 9 tests, 120 total green
- supervisor + cli wiring: --a4-positive-observation (append), --a4-negative-data-plane-observation (append),
  --a4-negative-qualification-observation, --a4-negative-shutdown-observation (all-or-none)
- Positive path re-verified on final bytes: v67/v68/v71 smokes green, v62 full positive green (same graph/manifest digests as current)

## DATA-PLANE NEGATIVE — diagnosis complete, one verifier fix needed
Failure is NOT product behavior. Every run shows: incident present, zero residual,
healthy peer released, honest failed terminal, bounded real cleanup. Two verifier
artifacts masked this:

1. TIMING: incident observed_at_unix_ms is backdated to failure detection, but the
   incident becomes VISIBLE in /__mycelium/live-status only when the periodic monitor
   publishes (~5 s cadence). Measuring recorded-observe -> my-detection inflates by
   the visibility lag (constant ~5.1 s across v68/v69/v71). FIX: measure from the
   moment the verifier SEES the incident (time.time() at detection), like v58 did
   (v58 sealed 1940 ms with visibility-based timing). Route-clock truth from v68:
   request lifetime 2.62 s INCLUDING ~2 s pre-kill wait + SSH RTT; terminal landed
   well under 2 s after kill.

2. TARGETING: node-2 runs TWO live sidecar children (verified by PPID scan).
   Killing BOTH = full peer loss -> route_peer_process_lost -> route dies (correct
   fail-closed, outside scoped claim). Killing the OLDEST one also dropped the route
   on v71. v58 passed killing a single first-name-match sidecar. NEXT: on a fresh
   serve, capture FULL cmdlines of both sidecars first (the --uds path disambiguates
   gossip/seed vs data transport); kill only the data-plane one; if both --uds paths
   are identical, diff their open sockets (ls -l /proc/PID/fd) and kill the one
   holding the peer transport socket.

3. ZOMBIES: killed sidecars linger as state-Z children (empty cmdline) until node-2
   exits; sidecar_process_alive may read zombie-as-alive. PPID scans must filter
   state=='S' and non-empty cmdline. pkill -f physical_inference_node kills the
   parent but orphaned sidecars need separate pkill -f mycelium-iroh-sidecar.

## ENVIRONMENT GOTCHAS (this session)
- Remote socket dir must be removed AFTER all procs dead; verify with
  test ! -e ... && echo socket_gone (loop; dying serves can recreate it).
- Multi-statement SSH strings with 2>/dev/null sometimes return empty output —
  use separate SSH invocations per step.
- /proc scans match their own SSH session cmdlines — filter on exact tokens
  ('physical_inference_node.py --run-id', 'sidecar' + state=='S').

## INSTALL EVIDENCE SET (does NOT need re-running all gates)
Only POSITIVE artifacts carry identity digests (graph/manifest) checked by install.
v58 data-plane (passed), v59 qualification (passed), v59 shutdown (passed) remain
valid install evidence. Use with a fresh positive from the current serve (v71+)
OR v62's positive (identical digests, verified SAME).

## NEXT SESSION SEQUENCE
0. HOST BLOCKER: node-2 host (astra-surface-book-2, 100.125.181.68) went OFFLINE
   2026-08-19 ~20:30 CEST (Tailscale relay "par", last-seen drifts, 100% pkt loss).
   Everything physical waits on its return. When back:
   a) restart membership: ssh ... 'cd /home/astra/mycelium-m14-membership-source && nohup /usr/bin/python3 -B -m mycelium_service_runner --config /home/astra/mycelium-a2-membership-control/package/service-config.json >> /home/astra/.local/state/mycelium/logs/node2-membership-runner.out 2>&1 & echo started'
   b) wait ~30s for lease renewal, then bind+serve normally.
1. Fresh cycle (kill all -> clean sockets -> bind -> serve -> smoke)
2. Data-plane: /tmp/verify-a4-dataplane-v73.py (READY: visibility timing + --uds
   targeting, 9 checks incl. request_reached_second_stage)
3. Qualification-409 + shutdown SIGTERM gates on same serve
4. Full positive gate (32 tokens) on the same serve
5. Restart serve with the four --a4-* flags -> live-status concurrency_liveness_
   qualification.eligible == true -> product snapshot readiness all-ready
6. Browser e2e: cd ui/web && MYCELIUM_A4_PRODUCT_ORIGIN=http://127.0.0.1:8791
   MYCELIUM_A4_BROWSER_EVIDENCE=.../a4-product-browser-vFINAL.json npm run test:a4-live-browser
7. Evidence seal; atomic A4 commit (canonical suite ALREADY green post-regen:
   2933 passed + 4 fixture failures fixed by regen = effectively 2937/0)

## KEY FILES
- /tmp/verify-a4-dataplane-v69c.py (closest verifier; fix timing + targeting)
- mycelium_live/a4_install.py (validator+builder)
- docs/superpowers/notes/2026-08-19-a4-qualification-install-design.md
- Evidence: .../a4-concurrency-20260818/evidence/{a4-product-negative-data-plane-v58,
  a4-product-negative-qualification-v59, a4-product-negative-shutdown-v59,
  a4-product-positive-v62-r1}.json
- Plans: operator-plan-v71.json latest bound; serve logs /tmp/serve-v7*.log
