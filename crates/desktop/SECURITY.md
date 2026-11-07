# Desktop window channel security design (L11)

Status: preparatory implementation, tested with the real macOS WebKit adapter and
an unavailable-core fake. **Not a production service or release security approval.**
Oracle: `f4e366e98a1f705dfaf182897044500d290dbd7f`.

## Transport decision

The app window loads `mycelium://localhost/`, served by a Tauri 2 asynchronous
custom-protocol handler in the same process. Relative fetches in the unchanged
HTML/JS retain their paths and bodies. There is **no localhost TCP listener in
this shell**. The L10 HTTP service remains separately responsible for browser,
chat/relay, remote-peer and directory listeners. Do not expose this native adapter
on one of those listeners or disable their cookie/Origin checks.

This is a deliberate alternative to opening a browser session in WKWebView. It
avoids TCP port theft, DNS rebinding, loopback CSRF, persistent browser cookies,
launch-file exchange and native-session bootstrap for the window channel. It
also avoids shipping a second JS/native RPC API. L10 must adapt its service layer
to `Core::submit`, not feed custom-scheme requests through its browser-session
constructor and invent credentials to satisfy that constructor.

## Authority, origin and tokens

- Only the native protocol context's `webview_label() == "main"` is accepted.
  No HTTP header, JS argument, JSON field or referrer can choose that label.
- Requests target the exact `mycelium://localhost/` authority. Reject userinfo,
  alternate scheme/authority, queries, fragments, percent encoding, backslashes,
  dot segments and legacy `/session` and `/native-session` bootstrap routes.
- The native webview is nonpersistent/incognito. There is **no bearer token** in
  a URL, JavaScript, localStorage, cookie jar, file, command line, environment,
  keychain or log for this channel. Native window identity, pinned document and
  channel lifetime are the capability. Core/peer credentials stay in Rust.
- When supplied, Origin must be a single exact `mycelium://localhost`; `null`,
  foreign and duplicate Origin values refuse. The strict standalone guard
  rejects missing Origin on POST.
- **Observed WebKit behavior:** on macOS 26.5.1 with Wry 0.57.0, same-window
  custom-protocol POST omits Origin. The first native smoke failed 403 for this
  reason. The adapter now independently reads the actual native WebviewWindow
  URL for POST and passes it to the separate `authorize_native_document` path.
  Only `/` or `/chat` at the pinned scheme/authority grants the missing-header
  case. It never accepts a supplied `null` Origin and never synthesizes a
  browser cookie. This path is inappropriate for a TCP HTTP server.
- POST additionally requires exactly one `X-Mycelium-Action: 1`, except the exact
  `/chat/api/state`, `/chat/api/send`, `/chat/api/events` paths require exactly one
  `X-Mycelium-Chat: 1` instead (oracle `chat.py:562–567`). Action cannot substitute
  for Chat; Chat grants no authority on any other POST, including Quit/Stop.
  Both families require one `Content-Type: application/json`.
  The channel body ceiling preserves the oracle app's 65,536-byte
  bound. The core owns strict JSON parsing, route/schema allowlisting and all
  actual user/peer authority checks, including chat's separate 16,384-byte bound;
  the shell only parses its empty Quit body.
- Quit revokes channel access before invoking cleanup. Later requests refuse.
  L6 must fence already-enqueued actions, cancel/drain outstanding callbacks,
  and prove no side effect escapes its revoked authority. A pre-dispatch UI
  guard is **not** final core admission or a proof against queue/quit races.

## Window containment

The window refuses navigation outside exactly the two document URLs and never
creates new webviews/popups. The unchanged `/chat` link's `_blank` request reuses
the existing pinned window. HTTP(S) links with a host and no URL userinfo open via
AppKit `NSWorkspace.openURL:` in the system browser; the native window stays put.
All other schemes/paths refuse. The host is intentionally not a fixed allowlist:
the oracle exposes runtime-selected local/relay chat links. No request headers,
cookies or native authority accompany the URL, and errors log a code, not a URL.
There is no shell interpolation, `open` child process or opener plugin/IPC grant.

CSP is a response header on the unchanged document bytes, not just a Tauri config
setting. It explicitly denies `frame-src`, `child-src`, `object-src`, `base-uri`,
`frame-ancestors`, `form-action` and `worker-src`. No inline script or remote
connection is allowed. The configuration and emitted-header policy are identical.
No Tauri plugins, commands or IPC capabilities are granted; `withGlobalTauri` is
false and devtools are disabled. Native-only APIs remain unavailable to the UI.

### CSP comparison with the frozen v0.1.4 oracle

`app.py:1878` uses default/script/style/connect `'self'`, object/base/frame-ancestors
`'none'`, form-action `'self'`. `chat.py:57–58` additionally specifies img-src
`'self'` and form-action `'none'`. Compared to those exact policies:

- script/style/connect/object/base/frame-ancestors are unchanged;
- `default-src` tightens from `'self'` to `'none'`;
- img-src is explicitly `'self'` (equivalent to oracle desktop fallback and chat);
- frame-src, child-src and worker-src explicitly tighten to `'none'`;
- form-action tightens desktop `'self'` to `'none'`, unchanged for chat. JS already
  prevents form navigation and uses same-origin fetch;
- the prior L11-only `img-src data:` allowance is removed. No UI bytes change.

### Executed adversarial matrix (development shell)

The native smoke waits for positive WebKit CSP or native navigation-denial events,
not a timeout interpreted as success. Same-path, different-path, redirect-source,
remote, data and blob iframe loads produce frame-src violations. WebKit routes
`srcdoc` through native `about:` navigation first; its positive native denial is
required instead. Top-level different-path, redirect-source, data and blob
navigation refuse; external navigation stays native-denied and calls the browser
seam. External/data/blob/different-path popups never create a native webview.
The local chat popup reuses the pinned window and proves its DOM and native POST.
Both pinned documents still accept the missing-Origin POST. A supplied Origin
explicitly goes through `Channel::authorize`, including null/foreign/duplicate
refusals; foreign labels/documents cannot obtain the exception.

Redirect sources are rejected **before loading** their 302 fixture; production
pinned documents are immutable 200 responses and cannot redirect. This is not an
HTTP redirect-chain network test. Any dev attack route reaching the protocol
handler is a hard failed receipt. The browser seam is a recording fake in the
native smoke (two HTTP(S) opens), so no user browser or external network is opened.
AppKit dispatch is compiled, not an exercised launch of the user's browser.
The injected probe and attack fixtures require the non-release development runner;
they are not frozen assets or a production command surface. With `dev-shell` AND
debug assertions, trusted composition code may call `run_development_with_probe`
with its own script string. The API, script storage and built-in script payload
are compiled out when debug assertions are disabled, including release builds
with `dev-shell` enabled. `run_packaged` never enables any probe. No web request,
environment variable or release API can inject a script. Probes retain every
native guard and capture external browser intents rather than launching browsers.
The two opt-in GUI tests are serialized and unfocused. The additive matrix covers
Chat-only admission from both documents, Action-only chat refusal, chat-header
non-chat refusal, the `/chat` subframe and chat-API popup refusal. Foreign native
labels and supplied null/foreign/duplicate Origin remain independent unit cases.

## Threats and residual risks

Protected boundaries: unrelated browser origins and tabs, forged/missing browser
Origin, foreign Tauri window, stale channel after Quit, path confusion, bootstrap
replay, and accidental native API exposure. Negative unit tests cover those
refusals. Real WebKit smoke checks unchanged DOM/script/style loading, positive
native POST, refused native-session POST and missing-action POST.

Not claimed: isolation from arbitrary same-UID native malware/debuggers, an XSS
already executing in trusted local assets, or compromised signed application
bytes. Such XSS has the UI's core authority; it must not gain filesystem/process
IPC. Bundle sealing and same-byte provenance are L8/L12 release gates. WebKit's
body allocation occurs before the Rust byte-limit check, so this is not a total
WebKit memory cap. `services::adapt` bounds outstanding callbacks through delivery,
revokes local submission on Quit, and refuses to count a dropped callback as
cleanup. Its underlying `Services` implementation still must enqueue promptly,
serialize final effect admission with shutdown, and supply real resource proof.
The native smoke exercises the concrete adapters over an inert service; no real
NodeService cleanup is wired here. See README S12/S13 for the exact contract.
Revalidate native URL/callback semantics, subframe/CSP containment, queue/quit
races and origin behavior on the supported minimum OS before release.

## Quit and C-1

Native `aevt/quit` registration is explicit, not assumed from Tauri ExitRequested.
The retained Rust NSObject target is installed before building the app, re-armed
at Tauri Ready (after AppKit initialization), and only advertised ready after a
subsequent native event-loop barrier. Its callback sets an atomic flag and never
runs cleanup inside Objective-C dispatch. The handler is unregistered before the
target is dropped. The macOS FFI module documents each unsafe ABI use.

Menu Quit/Cmd-Q, AppleEvent, window close and `/api/quit` enter the same `Shutdown`
state machine. `Cleanup::begin` and `poll` are nonblocking seams, not permission
to run slow backend work on the GUI thread. Begin is invoked at most once. Only
Verified permits process exit; an error or monotonic deadline expiry remains an
explicit failure and keeps the process/resources owned. An owned joinable wake
thread supplies event-loop progress while idle. It is stopped/joined on exit.
The future L6 implementation must satisfy the nonblocking trait contract and
cover build/startup failure, OS logout/SIGTERM and queue draining; those are not
proven by the fake-core smoke.
