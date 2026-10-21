# PV25 - idle weight unload, plugged-in-only sharing, energy estimate

## Idle unload (member stage worker)
- Setting: `<app root>/idle-unload-policy.json` (`unload_after_minutes`: 1..1440 or `"never"`, default 10), set from the
  app (`POST /api/idle/policy {"unload_after_minutes": 15|"never"}`) and published by the app into each managed run's
  private root. A node with no published policy never unloads (gate harnesses and tools are unaffected).
- Node (`physical_inference_node.py` + `mycelium_idle.node_link`): after N idle minutes with no request holding KV, the
  runtime port drops its tensors (`MLXRuntimePort.release_weights`). The process, sidecar, signer, router, membership and
  route registration stay up; `health` keeps answering with `details.idle_residency` (`state`: loaded | idle | warming |
  failed). Member status line: `idle (weights unloaded)`.
- Reload: `idle_reload` (owner, over the managed channel) or the first `infer_*` command reloads from the same assignment
  and refuses weights whose authenticated tensor digest / proof differ (`weights_restore_mismatch`). Reload time and RSS
  before/after unload are recorded in `idle-status.json` and the app status (`idle_energy.member`).
- Route (`PhysicalLiveRoute._warm_idle_peers`, before admission): reads only the last verified health; an idle/warming
  member is woken and waited for (bounded, 25 s). Past the deadline: `member_warming` (retriable, no command issued, no
  keepalive miss, no replica loss). A member that stops answering gets no immunity: keepalive misses, quarantine and the
  A5/A6 loss path are unchanged. A reload that errors or exceeds its hard deadline (180 s) closes the node and terminates
  the process (fail closed -> existing loss path).
- Known edge: a request that arrives within one keepalive interval of an unload may hit a not-yet-woken *downstream*
  member; that hop fails closed (`weights_unloaded`) like any hop failure. Not handled specially.

## Plugged in only
- `POST /api/adaptive/policy {"adaptive_sharing", "pause_sharing", "plugged_in_only", "acknowledged"}`; stored as the
  optional `plugged_in_only` key of the adaptive policy (forces the controller to exist, not the automatic one).
- On battery (`resource_observation.power == "battery"`) the controller drains with reason `host_on_battery`; status line
  `Adaptive sharing: paused - on battery`; recovery uses the normal healthy window. `unknown` power never pauses.

## Energy estimate
- `mycelium_idle.energy`: worker CPU time (`proc_pid_rusage`) x (chip CPU watts / cores) + worker GPU time (I/O Registry
  `accumulatedGPUTime` per client) x chip GPU watts. Per-chip table is rounded ballpark full-load figures, labelled an
  estimate everywhere; no powermetrics, no sudo; idle baseline of the Mac is not attributed.

## Linux / Windows
- Same interfaces; `LinuxProcessTimeStub` / `WindowsProcessTimeStub` report unavailable (PV29 replaces them). Power uses
  the existing platform `observe_policy`. The NumPy port has no `release_weights`, so no idle link attaches there.
