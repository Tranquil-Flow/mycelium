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

## Native Quit: shared graceful shutdown

The frozen macOS launcher now bundles `native/quit_bridge.m`, compiled offline
against system AppKit by `scripts/build_a13_macos.py`. No PyObjC or new runtime
installation is needed. The bridge installs the standard `kAEQuitApplication`
handler and a **Quit Mycelium A13** menu item with ⌘Q. A nonblocking main-thread
AppKit pump runs between the launcher's existing 250 ms stop checks; it drains
at most 32 events per poll. Headless certification uses the same bridge without
activation, windows, or a browser. Source-mode Python and non-macOS launchers
keep their existing signal/page shutdown behavior.

The native handler only requests `StopRequest`. It never calls `exit`, kills a
child, or replaces the cleanup path. Menu Quit, AppleEvent Quit, and SIGTERM all
reach `DemoApp.close()`, the same cleanup used by the authenticated **Quit demo**
button: stop the run/contributor, close acquisition and relay, stop authority,
close HTTP, remove private launch/control files, and release the instance lock.
Existing owned-process shutdown deadlines and cleanup proofs are unchanged.
A recorded cleanup failure now produces `launcher_cleanup_failed` /
`cleanup_incomplete` and exit status 3 rather than a false successful exit.
There is no new force-exit timer that could skip cleanup proofs.

Do not implement this by returning `NSTerminateLater` from a delegate in the
Python-polled loop: AppKit nests a termination loop there, preventing Python
from reaching cleanup. The explicit AppleEvent/menu handlers return immediately;
a defensive direct `terminate:` delegate requests cleanup and cancels AppKit's
own termination. Python exits normally after cleanup. The bounded native pump
is not a hard wall-clock bound on arbitrary pre-existing cleanup failures; the
existing component cleanup budgets remain authoritative.

Closing the browser tab does not stop Mycelium. Use the Mycelium app’s menu Quit
(⌘Q, with Mycelium rather than the browser focused), or **Quit demo** under
**Pause, restart, or leave**. Private identity and evidence remain on disk.

### Qualification and parent device round

Source tests compile the shipped bridge with the installed SDK, send a real
PID-targeted AppleEvent to a test-owned headless process, exercise the native
menu action and repeated Quit, and verify the launcher's shared cleanup/lock
release and failure status. `tests/a13_macos/test_package.py` adds a headless
built-executable AppleEvent gate: it targets only its child PID and verifies
exit, socket closure, private control-file removal, lock reacquisition and
retained state. It requires `A13_PACKAGE_EXECUTABLE` from the package pipeline;
source tests do not substitute for that gate.

After building/installing the final candidate, the parent must test the actual
installed app during the device round (not from an agent lane):

```sh
osascript -e 'tell application id "org.mycelium.a13-macos-demo" to quit'
```

Repeat with menu **Quit Mycelium A13**, ⌘Q while Mycelium is focused, and a
parent-controlled logout. Exercise both idle and active owner/member runs with
a relay. Check run/owned-child cleanup evidence, relay closure, released locks,
and successful reopen with the same identity, not merely process disappearance.
Do not report a nonzero cleanup result or a forced OS logout as graceful cleanup.
Finder/logout behavior and active two-Mac cleanup remain device qualification,
not claims made by the headless fixture tests.
