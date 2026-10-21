#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""A9 two-session browser withdrawal harness (live run happens later, on devices).

Two clean Chromium contexts open the same live origin's Nodes workspace.  The
grant-gated A9 authority fault publisher (``--a9-authority-fault-control-port``)
then injects, for one current member, a stage withdrawal, a codec withdrawal and a
registered-unknown runtime value, each followed by ``restore``.  Both sessions must
show the failed capacity withdrawn (``Activation eligible`` = No, authority
generation advanced, the other devices untouched) *without a reload*, and the
``/events`` stream must have delivered messages after the publish.

The product CSP is ``script-src 'self'``: the harness reads the page only through
Playwright locators (no ``page.evaluate``, ``wait_for_function``, init scripts or
``bypass_csp``).  Chromium keeps no response body for an EventSource, so stream
activity is collected from CDP ``Network.eventSourceMessageReceived`` (event name,
id, size and a digest only).  The publisher's acknowledgement means *published*; only
what the browsers then render counts.

The grant is read from ``MYCELIUM_A9_FLEET_GRANT`` and never defaulted here.  Port
8787 is refused.  The receipt (``30-withdrawal-browser.json``) states its claim
boundary: the Nodes workspace in two sessions, not all eight workspaces.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

PROTOCOL = "mycelium.a9_withdrawal_browser.v1"
GRANT_ENV = "MYCELIUM_A9_FLEET_GRANT"
FAULT_PATH = "/a9/authority-fault"
GRANT_HEADER = "x-mycelium-a9-fleet-grant"
TOKEN_HEADER = "x-mycelium-a9-authority-fault-token"
TABLE_TITLE = "Durable membership and capability"
WITHHELD_TEXT = "Live claim withheld"
SEQUENCE: tuple[tuple[str, str | None], ...] = (
    ("stage_withdrawal", None),
    ("codec_withdrawal", None),
    ("unknown_registry_value", "future-npu-9"),
)
REFUSED_PORTS = frozenset({8787})
CLAIM_BOUNDARY = (
    "two clean browser sessions on one origin rendered the fault-injected capacity as withdrawn in the Nodes "
    "workspace without a reload; the publisher acknowledgement is not evidence, and the other seven workspaces "
    "and a physical A9 run are separate"
)


class HarnessRefused(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# -- publisher ------------------------------------------------------------------


def publish_fault(*, port: int, token: str, grant: str, kind: str, node_id: str, value: str | None = None,
                  timeout_s: float = 10.0) -> dict[str, Any]:
    """POST one fault to the direct-loopback door; no Origin or forwarding headers are sent."""
    body: dict[str, Any] = {"kind": kind, "node_id": node_id}
    if value is not None:
        body["value"] = value
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{FAULT_PATH}", data=json.dumps(body).encode(), method="POST",
        headers={GRANT_HEADER: grant, TOKEN_HEADER: token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            doc = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            code = json.load(exc).get("code", "unknown")
        except ValueError:
            code = "unknown"
        raise HarnessRefused(f"publisher_refused:{exc.code}:{code}") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise HarnessRefused("publisher_unreachable") from None
    published = doc.get("published") if isinstance(doc, dict) else None
    if not isinstance(published, dict) or published.get("kind") != kind or published.get("node_id") != node_id:
        raise HarnessRefused("publisher_ack_invalid")
    return published


# -- reading the page (locators only) -------------------------------------------


class Page(Protocol):
    def goto(self, url: str) -> Any: ...
    def locator(self, selector: str) -> Any: ...
    def get_by_role(self, role: str) -> Any: ...


def read_nodes(page: Page) -> dict[str, Any]:
    """The Nodes workspace's device table: ``{withheld, rows: {entity_id: {column: text}}}``."""
    section = page.locator("section.panel").filter(has_text=TABLE_TITLE)
    headers = [h.strip() for h in section.locator("thead th").all_inner_texts()]
    body = section.locator("tbody tr")
    rows: dict[str, dict[str, str]] = {}
    for index in range(body.count()):
        cells = [c.strip() for c in body.nth(index).locator("th, td").all_inner_texts()]
        if len(cells) == len(headers) and cells[0]:
            rows[cells[0]] = dict(zip(headers, cells))
    withheld = page.get_by_role("status").filter(has_text=WITHHELD_TEXT).count() > 0
    return {"withheld": withheld, "rows": rows}


def _generation(row: dict[str, str]) -> int | None:
    text = row.get("Authority generation", "")
    return int(text) if text.isdigit() else None


def _changed(before: dict[str, dict[str, str]], after: dict[str, dict[str, str]]) -> list[str]:
    return sorted(e for e in set(before) | set(after) if before.get(e) != after.get(e))


# -- sessions -------------------------------------------------------------------


class Session:
    """One browser context: its page, the stream log and the main-frame navigation count."""

    def __init__(self, name: str, page: Page, stream: list[dict[str, Any]], navigations: list[str]) -> None:
        self.name, self.page, self.stream, self.navigations = name, page, stream, navigations


def record_event_streams(cdp: Any, stream: list[dict[str, Any]]) -> None:
    """Record ``/events`` deliveries into ``stream``, whichever way the page reads them.

    * ``Network.eventSourceMessageReceived`` - a real ``EventSource`` (the observatory path).
    * ``fetch(url, {headers: {Accept: 'text/event-stream'}})`` read through ``response.body`` (the Nodes
      workspace): CDP never reports those as EventSource messages, so remember the request on
      ``requestWillBeSent`` (path ends ``/events``, ``Accept`` names ``text/event-stream``) and count
      ``dataReceived`` with ``dataLength > 0`` - or, when no data event was seen, ``loadingFinished``
      with ``encodedDataLength > 0`` - for that request id.  Only CDP network events; nothing runs in the page.
    """
    tracked: dict[str, bool] = {}  # request id -> a delivery was already counted

    def record(params: dict[str, Any]) -> None:
        data = str(params.get("data", ""))
        stream.append({"event": params.get("eventName"), "id": params.get("eventId"), "bytes": len(data),
                       "digest": "sha256:" + hashlib.sha256(data.encode()).hexdigest()})

    def will_send(params: dict[str, Any]) -> None:
        request = params.get("request") or {}
        accept = next((str(v) for k, v in (request.get("headers") or {}).items() if str(k).lower() == "accept"), "")
        path = urlsplit(str(request.get("url", ""))).path
        if params.get("requestId") and path.endswith("/events") and "text/event-stream" in accept.lower():
            tracked[str(params["requestId"])] = False

    def count(request_id: str, size: Any) -> None:
        if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0:
            tracked[request_id] = True
            stream.append({"event": "fetch-stream", "id": request_id, "bytes": int(size)})

    def data_received(params: dict[str, Any]) -> None:
        request_id = str(params.get("requestId"))
        if request_id in tracked:
            count(request_id, params.get("dataLength"))

    def finished(params: dict[str, Any]) -> None:
        request_id = str(params.get("requestId"))
        if request_id in tracked and not tracked.pop(request_id):
            count(request_id, params.get("encodedDataLength"))

    def failed(params: dict[str, Any]) -> None:
        tracked.pop(str(params.get("requestId")), None)

    cdp.on("Network.eventSourceMessageReceived", record)
    cdp.on("Network.requestWillBeSent", will_send)
    cdp.on("Network.dataReceived", data_received)
    cdp.on("Network.loadingFinished", finished)
    cdp.on("Network.loadingFailed", failed)


@contextmanager
def playwright_sessions(*, engine: str = "chromium", headless: bool = True,
                        ignore_https_errors: bool = False) -> Iterator[list[Session]]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as driver:
        browser = getattr(driver, engine).launch(headless=headless)
        try:
            sessions: list[Session] = []
            for name in ("session-a", "session-b"):
                context = browser.new_context(viewport={"width": 1280, "height": 900}, reduced_motion="reduce",
                                              ignore_https_errors=ignore_https_errors)
                page = context.new_page()
                stream: list[dict[str, Any]] = []
                navigations: list[str] = []
                page.on("framenavigated", lambda frame, log=navigations, page=page: log.append(frame.url)
                        if frame == page.main_frame else None)
                cdp = context.new_cdp_session(page)
                cdp.send("Network.enable")

                record_event_streams(cdp, stream)
                sessions.append(Session(name, page, stream, navigations))
            yield sessions
        finally:
            browser.close()


# -- the run --------------------------------------------------------------------


def _poll(read: Callable[[], Any], done: Callable[[Any], bool], *, timeout_s: float, interval_s: float,
          clock: Callable[[], float], sleep: Callable[[float], None]) -> tuple[bool, Any]:
    deadline = clock() + timeout_s
    while True:
        value = read()
        if done(value):
            return True, value
        if clock() >= deadline:
            return False, value
        sleep(interval_s)


def run_withdrawal(
    sessions: Sequence[Session], publisher: Callable[..., dict[str, Any]], *, base_url: str, node_id: str,
    sequence: Sequence[tuple[str, str | None]] = SEQUENCE, timeout_s: float = 30.0, interval_s: float = 0.25,
    clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
    pinned_entity: str | None = None,
) -> dict[str, Any]:
    if len(sessions) != 2 or sessions[0] is sessions[1]:
        raise HarnessRefused("two_distinct_sessions_required")
    blockers: list[str] = []
    steps: list[dict[str, Any]] = []
    poll = lambda read, done: _poll(read, done, timeout_s=timeout_s, interval_s=interval_s, clock=clock, sleep=sleep)  # noqa: E731

    baseline: dict[str, dict[str, Any]] = {}
    for session in sessions:
        session.page.goto(f"{base_url.rstrip('/')}/#nodes")
        ok, view = poll(lambda s=session: read_nodes(s.page), lambda v: bool(v["rows"]) and not v["withheld"])
        baseline[session.name] = view
        if not ok:
            blockers.append(f"{session.name}:nodes_not_live_before_fault")
    if blockers:
        return _receipt(base_url, node_id, baseline, steps, blockers)
    names = [s.name for s in sessions]
    if baseline[names[0]]["rows"] != baseline[names[1]]["rows"]:
        blockers.append("sessions_disagree_before_fault")
        return _receipt(base_url, node_id, baseline, steps, blockers)
    current = {name: baseline[name]["rows"] for name in names}
    outstanding = False  # a published fault that has not been restored

    try:
        for kind, value in sequence:
            step: dict[str, Any] = {"kind": kind, "value": value, "sessions": {}, "checks": {}}
            steps.append(step)
            seen_before = {s.name: (len(s.stream), len(s.navigations)) for s in sessions}
            try:
                step["ack"] = publisher(kind=kind, node_id=node_id, value=value)
            except HarnessRefused as refused:
                blockers.append(f"{kind}:{refused.code}")
                break
            outstanding = True
            targets: dict[str, str] = {}
            for session in sessions:
                ok, view = poll(lambda s=session: read_nodes(s.page),
                                lambda v, s=session: not v["withheld"] and _changed(current[s.name], v["rows"]) != [])
                changed = _changed(current[session.name], view["rows"])
                observed = {"changed_entities": changed, "stream_messages_after_publish":
                            len(session.stream) - seen_before[session.name][0],
                            "navigations_after_publish": len(session.navigations) - seen_before[session.name][1]}
                step["sessions"][session.name] = observed
                if not ok:
                    blockers.append(f"{kind}:{session.name}:withdrawal_not_observed")
                    continue
                if len(changed) != 1 or (pinned_entity is not None and changed != [pinned_entity]):
                    blockers.append(f"{kind}:{session.name}:not_exactly_the_target_capacity")
                    continue
                entity = changed[0]
                before, after = current[session.name][entity], view["rows"][entity]
                targets[session.name] = entity
                before_gen, after_gen = _generation(before), _generation(after)
                observed.update(entity=entity, eligible_before=before.get("Activation eligible"),
                                eligible_after=after.get("Activation eligible"),
                                authority_generation_before=before_gen, authority_generation_after=after_gen)
                if not (before.get("Activation eligible") == "Yes" and after.get("Activation eligible") == "No"):
                    blockers.append(f"{kind}:{session.name}:capacity_not_withdrawn")
                if before_gen is None or after_gen is None or after_gen <= before_gen:
                    blockers.append(f"{kind}:{session.name}:authority_generation_not_advanced")
                if value is not None and value not in after.get("Runtime", ""):
                    blockers.append(f"{kind}:{session.name}:unknown_runtime_value_not_shown")
                if observed["navigations_after_publish"] != 0:
                    blockers.append(f"{kind}:{session.name}:page_reloaded_after_publish")
                if observed["stream_messages_after_publish"] < 1:
                    blockers.append(f"{kind}:{session.name}:no_events_stream_message_after_publish")
                current[session.name] = view["rows"]
            step["checks"]["same_capacity_in_both_sessions"] = len(set(targets.values())) == 1 and len(targets) == 2
            if not step["checks"]["same_capacity_in_both_sessions"]:
                blockers.append(f"{kind}:sessions_name_different_capacities")
            # Restore, and require both sessions to show the capacity back before the next fault.
            if _restore_ack(publisher, node_id, step, blockers):
                outstanding = False
            if _restore(sessions, step, current, targets, poll, blockers):
                continue
            break
    finally:
        if outstanding:
            try:
                publisher(kind="restore", node_id=node_id, value=None)
            except HarnessRefused:
                blockers.append("final_restore_failed")
    return _receipt(base_url, node_id, baseline, steps, blockers)


def _restore_ack(publisher, node_id, step, blockers) -> bool:
    try:
        step["restore_ack"] = publisher(kind="restore", node_id=node_id, value=None)
    except HarnessRefused as refused:
        blockers.append(f"{step['kind']}:restore:{refused.code}")
        return False
    return True


def _restore(sessions, step, current, targets, poll, blockers) -> bool:
    """Both sessions must show the capacity eligible again before the next fault."""
    kind = step["kind"]
    if "restore_ack" not in step:
        return False
    restored = True
    step["restored"] = {}
    for session in sessions:
        entity = targets.get(session.name)
        if entity is None:
            restored = False
            continue
        ok, view = poll(lambda s=session: read_nodes(s.page),
                        lambda v, e=entity: v["rows"].get(e, {}).get("Activation eligible") == "Yes")
        step["restored"][session.name] = {"entity": entity, "eligible": view["rows"].get(entity, {}).get("Activation eligible")}
        if not ok:
            blockers.append(f"{kind}:{session.name}:restore_not_observed")
            restored = False
        else:
            current[session.name] = view["rows"]
    return restored


def _receipt(base_url: str, node_id: str, baseline: dict[str, Any], steps: list[dict[str, Any]],
             blockers: list[str]) -> dict[str, Any]:
    return {
        "protocol": PROTOCOL, "kind": "a9_two_session_withdrawal", "recorded_at": time.time(),
        "claim_boundary": CLAIM_BOUNDARY, "origin": base_url, "node_id": node_id,
        "baseline_devices": sorted(next(iter(baseline.values()))["rows"]) if baseline else [],
        "steps": steps, "blockers": blockers, "passed": not blockers and bool(steps),
        "csp_safe_reads": "locators and CDP network events only; no page.evaluate or script injection",
    }


# -- CLI ------------------------------------------------------------------------


def _write_receipt(out_dir: Path, document: dict[str, Any]) -> Path:
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    out_dir.chmod(0o700)
    path = out_dir / "30-withdrawal-browser.json"
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", required=True, help="the live product origin both sessions open")
    parser.add_argument("--fault-port", type=int, required=True, help="--a9-authority-fault-control-port")
    parser.add_argument("--token-file", required=True, help="<product_state_root>/a9-authority-fault.token")
    parser.add_argument("--node-id", required=True, help="the current member whose capacity is failed")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--engine", choices=("chromium", "firefox", "webkit"), default="chromium")
    parser.add_argument("--timeout-s", type=float, default=30.0)
    parser.add_argument("--pin-entity", help="expected device row (pseudonymous id) of the failed capacity")
    parser.add_argument("--ignore-https-errors", action="store_true")
    return parser


def main(argv: list[str] | None = None, *, sessions_factory: Callable[..., Any] = playwright_sessions) -> int:
    args = build_parser().parse_args(argv)
    grant = os.environ.get(GRANT_ENV)
    try:
        if not grant:
            raise HarnessRefused("explicit_fleet_grant_required")
        port_in_url = int(args.base_url.rsplit(":", 1)[-1].split("/")[0]) if args.base_url.rsplit(":", 1)[-1].split("/")[0].isdigit() else None
        if args.fault_port in REFUSED_PORTS or port_in_url in REFUSED_PORTS:
            raise HarnessRefused("port_8787_is_the_production_seed")
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
        if not token:
            raise HarnessRefused("fault_token_empty")
    except (HarnessRefused, OSError) as refused:
        print(f"a9 withdrawal harness refused: {getattr(refused, 'code', refused)}", file=sys.stderr)
        return 2

    def publisher(*, kind: str, node_id: str, value: str | None) -> dict[str, Any]:
        return publish_fault(port=args.fault_port, token=token, grant=grant, kind=kind, node_id=node_id, value=value)

    with sessions_factory(engine=args.engine, ignore_https_errors=args.ignore_https_errors) as sessions:
        receipt = run_withdrawal(sessions, publisher, base_url=args.base_url, node_id=args.node_id,
                                 timeout_s=args.timeout_s, pinned_entity=args.pin_entity)
    path = _write_receipt(Path(args.out_dir).resolve(), receipt)
    print(json.dumps({"passed": receipt["passed"], "blockers": receipt["blockers"], "receipt": str(path)}))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
