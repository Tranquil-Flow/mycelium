# v0.1.4 first-run choices and launcher boundary

## Model selection

Fresh app state selects `a9-route`: the existing pinned
`Qwen/Qwen2.5-0.5B-Instruct` contract at
`7ae557604adf67be50417f59c2c2f167def9a775`. This is the tested two-Mac starter.
The snapshot is 999,604,126 bytes (about 1 GB), not a new model or download.
Existing `run-model.json` choices are retained byte-for-byte;
`MYCELIUM_A13_RUN_MODEL` still takes precedence and locks the picker.
The larger Qwen3 choices remain available, labelled not yet tested on two Macs.

Small models use the whole-snapshot path on joined Macs. Slice mode is decided
by `serving.slice_fetch_permitted`, not by an unconditional UI promise.

## Fast mode

The owner-page switch stores a strict boolean in private `fast-mode.json`.
Fresh state is OFF. Precedence is:

1. explicit `MYCELIUM_TOKEN_PATH=on` or `off` (locks the switch);
2. the saved owner preference;
3. OFF.

Invalid explicit values and invalid saved records fail closed. The API rejects
changes while an owner run is running, serving, stopping or failing, and while
an advanced workflow or compile/serving thread is active. Start and preference
writes share `app.guard`; changing the switch cannot silently alter a run.
The UI says: faster answers, but no mid-answer recovery if a Mac drops.

### Propagation trace

The standalone `__main__.main` binds the app with `fast_mode.bind_owner_process`
before opening the browser. It sets a separate process default,
`MYCELIUM_TOKEN_PATH_APP_DEFAULT`, without modifying the operator variable.
This is a launcher implementation detail, not another user setting. All owner
threads read the same default; accepted idle-only changes update it. The binding
is restored after app shutdown, including exceptional exits. In-process hosts
embedding multiple DemoApp instances must explicitly bind only the app that owns
the process; starting an embedded instance alone does not modify global env.

Every production no-argument `token_path_enabled()` consumer is covered:

- `workflow.prepare_serving_workflow`: chooses the token-owner factory;
- `token_owner.ManagedRouteTokenOwner`: enables the managed owner;
- `mycelium_live.token_path_owner.OwnerTokenPath`: marks opt-in required;
- `require_token_path_owner`, called by `PhysicalLiveRoute` constructors:
  retains fail-closed unprovisioned-ON refusal;
- `physical_inference_node`: selects MLX materialization and its observation
  projection in a native child. `worker._ConcurrentNativeSession` uses
  `source_mode_environment`; source children copy the default, frozen children
  inherit it unchanged, and the exec trampoline preserves it.

Other staging/compiler children inherit the same setting; none creates signed
permission from it. Joined Macs need no local switch: the owner signs the run
policy, `TargetTokenPolicy.before` independently admits it, and native
`_install_token_path` verifies it before provisioning participant policy. Tests
exercise signed installation/inference with the joined worker's local flag both
OFF and ON. This proves the authority path with scripted runtime work, not a
fresh physical two-Mac benchmark. Joined Macs without local ON retain per-layer
MLX materialization; that optimization knob is not signed token-path authority.

## Downloaded-copy dialog

A bare frozen macOS launch outside the installed programs root still refuses to
start a session. When the home Applications entry resolves to the installed
executable, the alert instead says:

> This is the downloaded copy. Mycelium is installed: open Mycelium A13 from the Applications folder in your home folder.

Missing/broken entries and entries pointing outside the installed programs root
keep the installer instructions. This checks placement, not code signature or
payload integrity, and does not run the installed app.

## Native Quit: explicitly deferred

This package is a frozen Python launcher opening a browser, not an AppKit shell.
Its main loop waits on `StopRequest`; it has SIGINT/SIGTERM handlers and an
existing authenticated `/api/quit` action, but no NSApplication delegate,
AppleEvent quit handler, or Cocoa event loop. Adding an AppleEvent bridge is a
native lifecycle change requiring packaged Finder/AppleEvent and child-cleanup
qualification, not a safe text-only fix. No such installed-app exercise is
permitted in this lane. Native menu Quit / `osascript quit` remains unqualified
and is not claimed fixed.

Use **Quit demo** under **Pause, restart, or leave** in the private owner/member
page. It invokes `/api/quit` -> `DemoApp.close`, the supported cleanup path;
closing the browser tab alone does not stop Mycelium. SIGTERM remains the
operator fallback. The page now explicitly explains the native-menu limitation.
A follow-up native shell must funnel menu Quit and AppleEvents into this same
stop path and prove owned-child shutdown, not merely process disappearance.
