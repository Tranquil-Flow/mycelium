# SPDX-License-Identifier: AGPL-3.0-only
"""Plain-English findings from a diagnostics document. Pure: no I/O, no clocks, no app access.

Each finding is ``{id, severity, peer, title, detail, fix}``.  The wording is for a person who is not an
engineer: what is wrong, why it matters, what to do.
"""
from __future__ import annotations

from typing import Any, Mapping

SNTP = "sudo sntp -sS time.apple.com"
CLOCK_FIX = (f"Run {SNTP} on both Macs (or switch on \"Set time and date automatically\" in System Settings → "
             "General → Date & Time), then check again.")
SKEW_WARN_SECONDS = 1.0
SKEW_CRITICAL_SECONDS = 4.0  # the shortest cleanup command lives about 4.5 s
MIN_COMMANDS_FOR_A_TREND = 5
TREND_SHARE = 0.5
SLOW_CLAIM_SECONDS = 5.0
RELAY_SOON_SECONDS = 300.0
LOW_DISK_BYTES = 5 * 10**9
MDS_BUSY_PERCENT = 40.0
_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _item(ident: str, severity: str, title: str, detail: str, fix: str, peer: str | None = None) -> dict[str, Any]:
    return {"id": ident, "severity": severity, "peer": peer, "title": title, "detail": detail, "fix": fix}


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _skew(clock: Mapping[str, Any]) -> tuple[float, str] | None:
    """(size in seconds, 'ahead' | 'behind') when the remote clock is off by more than the noise, else None."""
    if not clock.get("available"):
        return None
    offset = _num(clock.get("offset_s"))
    if offset is None:
        return None
    size, uncertainty = abs(offset), _num(clock.get("uncertainty_s")) or 0.0
    if size < SKEW_WARN_SECONDS or size <= uncertainty:
        return None
    return size, "ahead" if offset > 0 else "behind"


def _seconds_ago(value: float | None) -> str:
    return "a while ago" if value is None else f"{value:.0f} s ago"


def _peer_findings(peer: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    node = peer.get("node_id")
    name = str(peer.get("name") or "The joined Mac")
    skew = _skew(peer.get("clock") or {})
    if skew is not None:
        size, word = skew
        out.append(_item(
            "peer_clock_skew", "critical" if size >= SKEW_CRITICAL_SECONDS else "warning",
            f"{name}'s clock is {size:.1f} s {'ahead of' if word == 'ahead' else 'behind'} this Mac",
            "Commands between the two Macs live only for a short time (some half a second to a few seconds). "
            f"With the clocks {size:.1f} s apart, short-lived commands expire before they are accepted, so health "
            "checks, cleanup and chats fail even though both Macs are running.",
            CLOCK_FIX, node))
    mailbox = peer.get("mailbox") or {}
    if mailbox.get("available"):
        counts = mailbox.get("counts") or {}
        terminal = sum(int(counts.get(k, 0)) for k in ("completed", "expired", "reconciliation_required", "failed"))
        expired, recon = int(counts.get("expired", 0)), int(counts.get("reconciliation_required", 0))
        if terminal >= MIN_COMMANDS_FOR_A_TREND and expired / terminal >= TREND_SHARE:
            out.append(_item(
                "peer_commands_expire", "critical", f"Most commands to {name} expire before it claims them",
                f"{expired} of {terminal} recent commands to {name} ran out of time unclaimed.",
                "The clock difference above is the likely cause: fix the clocks first." if skew is not None else
                f"Check that Mycelium is open and the Mac is awake on {name}, that the public relay is running and "
                "that both Macs are online. If it keeps happening, stop and start the demo.", node))
        if terminal >= MIN_COMMANDS_FOR_A_TREND and recon / terminal >= TREND_SHARE:
            out.append(_item(
                "peer_commands_unconfirmed", "critical", f"Most commands to {name} are claimed but never confirmed",
                f"{recon} of {terminal} recent commands were picked up by {name} but its answer came too late to "
                "count, so the owner had to ask it to reconcile them.",
                "Check the clocks first (see the clock finding). Otherwise check the network between the Macs.", node))
        latency = _num(mailbox.get("median_claim_latency_s"))
        if latency is not None and latency >= SLOW_CLAIM_SECONDS:
            out.append(_item(
                "peer_slow_claims", "warning", f"{name} takes {latency:.1f} s to claim a command",
                "Healthy Macs claim a command in well under a second. Slow claims make chats stall or time out.",
                "Check the network and relay between the Macs, and that the Mac is not busy or asleep.", node))
    live = peer.get("liveness") or {}
    if live.get("available"):
        label, seen = live.get("label"), _num(live.get("last_seen_seconds_ago"))
        if label == "quarantined":
            out.append(_item(
                "peer_quarantined", "critical", f"Chats are refused because {name} is quarantined",
                f"{name} stopped answering health checks ({live.get('consecutive_misses')} missed in a row, last "
                f"heard from {_seconds_ago(seen)}). While it is quarantined the router refuses every chat instead "
                "of sending it there.",
                f"Check that Mycelium is open and awake on {name}, that the relay is up and that the clocks agree. "
                "It is let back in after two answered health checks in a row; if it is not, stop and start the demo.",
                node))
        elif label == "stale":
            out.append(_item(
                "peer_stale", "warning", f"{name} has gone quiet",
                f"{name} missed its recent health checks (last heard from {_seconds_ago(seen)}).",
                f"Check that {name} is awake and online. If it stays quiet it will be quarantined and chats will "
                "be refused.", node))
    membership = peer.get("membership") or {}
    if membership.get("liveness_status") == "dead" or membership.get("liveness_dead") is True:
        out.append(_item(
            "peer_not_heard_from", "critical", f"{name} has not been heard from",
            f"The swarm has not received a heartbeat from {name} for long enough to consider it gone.",
            f"Open Mycelium on {name} and check it is online; if it was restarted it may need to rejoin.", node))
    return out


def _chat_findings(diag: Mapping[str, Any]) -> list[dict[str, Any]]:
    recent = list((diag.get("chats") or {}).get("recent") or [])
    failed = [c for c in recent if c.get("terminal") == "failed" and c.get("error_code")]
    out: list[dict[str, Any]] = []
    quarantined = any((p.get("liveness") or {}).get("label") == "quarantined" for p in diag.get("peers") or [])
    named = [c for c in failed if "quarantin" in str(c.get("error_code"))]
    if named and not quarantined:
        out.append(_item(
            "chats_refused_quarantine", "critical", "Chats are refused because a joined Mac is quarantined",
            "The router reported that a Mac on the route stopped answering health checks, so it refuses chats "
            "instead of sending them there.",
            "Check that Mycelium is open and awake on the joined Mac, that the relay is up and that the clocks agree "
            f"({SNTP} on both Macs). It is let back in after two answered health checks."))
    generic = [c for c in failed if c.get("error_code") == "backend_failed"]
    if len(generic) >= 2 or (generic and len(recent) <= 2):
        extra = " A joined Mac is quarantined, which is the likely reason." if quarantined else ""
        out.append(_item(
            "chats_generic_failure", "warning",
            f"{len(generic)} recent chats failed with the generic code backend_failed",
            "backend_failed is a catch-all: the real reason was dropped before it reached the chat." + extra,
            "Look at the peers below (clock, liveness, command health) for the cause; send the support bundle if "
            "it is not obvious."))
    return out


def _relay_findings(diag: Mapping[str, Any]) -> list[dict[str, Any]]:
    relay = diag.get("relay") or {}
    remote = [p for p in diag.get("peers") or [] if not p.get("is_this_mac")]
    if not remote or diag.get("role") != "owner":
        return []
    if relay.get("state") != "relay_process_running":
        code = relay.get("error_code")
        return [_item(
            "relay_down", "critical", "The public relay is not running",
            "Joined Macs reach this Mac through the relay, so they cannot serve or answer health checks."
            + (f" The relay reported {code}." if code else ""),
            "Start the public relay again (Invite or join with a code → Start public relay).")]
    left = _num(relay.get("expires_in_seconds"))
    if left is not None and left < RELAY_SOON_SECONDS:
        return [_item(
            "relay_expiring", "warning", f"The public relay expires in {max(0, round(left / 60.0))} min",
            "When it expires joined Macs lose contact and chats stop.",
            "Restart the relay for a longer period before it expires.")]
    return []


def _host_findings(host: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    level = host.get("memory_pressure")
    if level in ("warn", "critical"):
        out.append(_item(
            "host_memory_pressure", "critical" if level == "critical" else "warning",
            f"This Mac is under {level} memory pressure",
            "When memory is short macOS slows or stops apps, which delays commands and can stop the model.",
            "Quit heavy apps (browsers, video, other models) and try again."))
    mds = _num(host.get("spotlight_mds_cpu_percent"))
    if mds is not None and mds >= MDS_BUSY_PERCENT:
        out.append(_item(
            "host_spotlight_busy", "warning", f"Spotlight (mds) is using {mds:.0f}% of a CPU",
            "Spotlight indexing competes with Mycelium and delays short-lived commands.",
            "Wait for indexing to finish, or exclude the Mycelium folders in System Settings → Siri & Spotlight → "
            "Spotlight Privacy."))
    disk = _num(host.get("disk_free_bytes"))
    if disk is not None and disk < LOW_DISK_BYTES:
        out.append(_item(
            "host_disk_low", "warning", f"Only {disk / 1e9:.1f} GB of disk space is free",
            "Model files and caches need space; downloads and runs fail when the disk fills.",
            "Free some disk space."))
    load, cpus = host.get("load_average"), _num(host.get("cpu_count"))
    first = _num(load[0]) if isinstance(load, (list, tuple)) and load else None
    if first is not None and cpus and first > 2 * cpus:
        out.append(_item(
            "host_load_high", "warning", f"This Mac is overloaded (load {first:.1f} on {int(cpus)} cores)",
            "A busy CPU delays every control command and the model.",
            "Close other heavy apps, or wait for background work (indexing, backups) to finish."))
    return out


def _member_findings(view: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    skew = _skew(view.get("owner_clock") or {})
    if skew is not None:
        size, word = skew
        out.append(_item(
            "owner_clock_skew", "critical" if size >= SKEW_CRITICAL_SECONDS else "warning",
            f"The owner's clock is {size:.1f} s {'ahead of' if word == 'ahead' else 'behind'} this Mac",
            "Commands from the owner carry a short lifetime. With the clocks this far apart they arrive already "
            "expired (or too early), so this Mac cannot serve.", CLOCK_FIX))
    loop = view.get("claim_loop") or {}
    errors = loop.get("errors") or {}
    calls = int(loop.get("polls") or 0) + int(loop.get("acks") or 0)
    failed = sum(int(v) for v in errors.values())
    if failed and calls and failed / calls >= 0.1:
        top = max(errors, key=lambda k: errors[k])
        out.append(_item(
            "claim_loop_errors", "warning", f"This Mac's calls to the owner are failing ({failed} recent, mostly {top})",
            "This Mac asks the owner for work over the relay; failed calls mean it misses commands.",
            "Check this Mac's internet connection and that the owner's relay is still running."))
    poll = _num(loop.get("median_poll_seconds"))
    if poll is not None and poll >= 3.0:
        out.append(_item(
            "claim_loop_slow", "warning", f"Each call to the owner takes {poll:.1f} s",
            "Healthy calls take well under a second. Slow calls make commands arrive late.",
            "Check this Mac's connection (try a different network) and the owner's relay."))
    command = loop.get("last_command") or {}
    remaining, ttl = _num(command.get("remaining_on_this_clock_seconds")), _num(command.get("ttl_seconds"))
    if remaining is not None and ttl is not None and ttl >= 1.0 and remaining < 0.5:
        out.append(_item(
            "claim_loop_late", "warning", "Commands from the owner arrive with almost no time left",
            f"The last command had a {ttl:.1f} s lifetime and {max(0.0, remaining):.1f} s remained on this Mac's clock.",
            f"Check the clocks ({SNTP}) and the network to the owner."))
    return out


def derive_findings(diag: Mapping[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for peer in diag.get("peers") or []:
        findings.extend(_peer_findings(peer))
    findings.extend(_chat_findings(diag))
    findings.extend(_relay_findings(diag))
    findings.extend(_host_findings(diag.get("host") or {}))
    if diag.get("member_view"):
        findings.extend(_member_findings(diag["member_view"]))
    findings.sort(key=lambda f: _ORDER.get(f["severity"], 3))
    return findings
