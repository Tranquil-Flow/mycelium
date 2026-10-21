#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""A12 route session: node-0 and node-2 as TEMPORARY members of the A12 gate seed.

Owner decision 2026-10-13 (seed authority = a).  The route Mac node-0 (entry) ->
phone (middle) -> laptop node-2 (final) needs all three placements in ONE seed
membership: the builder's phone path (``mycelium_seed.phone_preparation``)
projects offers only for current members of the seed that holds the phone, and
requires each member's seed endpoint id to equal the topology's data-plane
endpoint id.  The phone is a member of the throwaway A12 gate seed
(swarm ``a12-gate``), so the Mac and the laptop take a second, temporary
membership there:

* invites come from the gate seed (``scripts/mint_swarm_invites.py``);
* each member runs a membership-only agent (``python -m mycelium_node
  --membership-control-only``) whose data dir and identity are scoped to the gate
  dir, and whose seed endpoint id is the topology's data-plane endpoint id;
* the route topology is bound to the gate seed's current generations;
* teardown stops both agents, revokes both gate memberships and deletes unused
  invites, at the end of the session and on every failure path of enrolment.

The fleet seed (seed-r7) is never a target.  Its data dir, URL, swarm id and
membership roots come from the config AND from its launchd agent, and every
configured seed path, member dir and URL that is (or is inside, or contains) one
of them is refused before any command runs.

Reachability: one seed has one advertised URL, carried by every invite and
verified by every member.  With the public tunnel off, the gate seed binds the
Mac's LAN address (the Pixel hotspot subnet) and advertises
``http://<that address>:<port>``; the laptop and the phone reach it over the
hotspot LAN.  The https proxy pattern (loopback bind + ``--trusted-https-proxy``)
remains possible when a proxy is up.  A loopback http seed is refused: the
laptop could not reach it.

The I/O seams (``read_gate_seed``, ``revoke``, ``spawn_local``, ``stop_local``,
``ssh``, the clock) are module attributes so tests replace them; nothing here
contacts a device by itself.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit

GATE_SEED_FIELDS = frozenset({"swarm_id", "bind", "port", "advertised_url", "trusted_https_proxy"})
GATE_MEMBERS_FIELDS = frozenset({"invite_ttl_seconds", "wait_seconds", "entry", "final"})
ENTRY_FIELDS = frozenset({"node_id", "data_dir", "advertise", "incarnation"})
FINAL_FIELDS = frozenset({"node_id", "ssh_target", "python", "checkout", "data_dir", "advertise", "incarnation"})
FLEET_FIELDS = frozenset({"launch_agent", "data_dir", "url", "protected_roots"})
LOOPBACK_BINDS = frozenset({"127.0.0.1", "::1", "localhost"})
WILDCARD_BINDS = frozenset({"0.0.0.0", "::"})
MEMBER_PEER_CLASS = "mac_mlx_iroh"
MEMBER_LIFECYCLE = "RUNNING"
REVOKE_REASON = "a12_route_session_teardown"
INVITE_COUNT = 2
#: A foreign membership state in a member dir (ssh exit code of the copy script).
FOREIGN_STATE_EXIT = 97
_SEGMENT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
_SSH_TARGET = re.compile(r"[A-Za-z0-9._@-]{1,128}\Z")
_SSH = ("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
_SCRUBBED_ENV = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")

# Replaceable seams (tests) -------------------------------------------------------------
monotonic: Callable[[], float] = time.monotonic
sleep: Callable[[float], None] = time.sleep
now: Callable[[], float] = time.time


class MembershipError(RuntimeError):
   """A coded refusal; the harness re-raises it as its own HarnessError."""


def _require(ok: object, code: str) -> None:
   if not ok:
      raise MembershipError(code)


# -- config ---------------------------------------------------------------------------------


def _origin(url: str) -> tuple[str, int] | None:
   try:
      parts = urlsplit(url)
      port = parts.port or {"http": 80, "https": 443}.get(parts.scheme)
   except ValueError:
      return None
   if not parts.hostname or port is None:
      return None
   return parts.hostname.lower(), int(port)


def _is_http_url(value: Any) -> bool:
   return isinstance(value, str) and urlsplit(value).scheme in ("http", "https") and _origin(value) is not None


def validate_gate_seed(document: Any, *, seed_url: Any) -> dict[str, Any]:
   """Bind/URL rules of ``python -m mycelium_seed``, plus reachability from the laptop."""

   _require(isinstance(document, Mapping) and set(document) == GATE_SEED_FIELDS, "config_gate_seed_invalid")
   swarm, bind, port, url, proxy = (document[k] for k in ("swarm_id", "bind", "port", "advertised_url", "trusted_https_proxy"))
   _require(isinstance(swarm, str) and _SEGMENT.fullmatch(swarm), "config_gate_seed_invalid")
   _require(type(port) is int and 1024 < port < 65536 and type(proxy) is bool, "config_gate_seed_invalid")
   _require(isinstance(bind, str) and bind and bind not in WILDCARD_BINDS and isinstance(url, str), "config_gate_seed_invalid")
   parts = urlsplit(url)
   _require(parts.path in ("", "/") and not parts.query and not parts.fragment, "config_gate_seed_invalid")
   if proxy:
      # The loopback HTTPS proxy pattern (tunnel): the seed refuses it with any other bind.
      _require(bind in LOOPBACK_BINDS and parts.scheme == "https" and _origin(url), "config_gate_seed_invalid")
   else:
      _require(parts.scheme == "http" and _origin(url) == (bind.lower(), port), "config_gate_seed_invalid")
      # node-2 is another host: a loopback http seed is unreachable from it.
      _require(bind not in LOOPBACK_BINDS, "config_gate_seed_unreachable_from_laptop")
   _require(seed_url == url, "config_seed_url_not_gate_advertised")
   return dict(document)


def _inside(path: str, root: str) -> bool:
   return path == root or path.startswith(root.rstrip("/") + "/")


def _absolute(value: Any) -> bool:
   return isinstance(value, str) and value.startswith("/") and "\x00" not in value


def validate_gate_members(document: Any, *, seed_state_root: str) -> dict[str, Any]:
   """Two members, each with a data dir scoped to the gate dir (the seed dir's parent)."""

   _require(isinstance(document, Mapping) and set(document) == GATE_MEMBERS_FIELDS, "config_gate_members_invalid")
   ttl, wait = document["invite_ttl_seconds"], document["wait_seconds"]
   _require(type(ttl) is int and 30 <= ttl <= 3600 and type(wait) is int and 10 <= wait <= 1800, "config_gate_members_invalid")
   entry, final = document["entry"], document["final"]
   _require(isinstance(entry, Mapping) and set(entry) == ENTRY_FIELDS, "config_gate_members_invalid")
   _require(isinstance(final, Mapping) and set(final) == FINAL_FIELDS, "config_gate_members_invalid")
   for member in (entry, final):
      _require(isinstance(member["node_id"], str) and _SEGMENT.fullmatch(member["node_id"]), "config_gate_members_invalid")
      _require(isinstance(member["incarnation"], str) and _SEGMENT.fullmatch(member["incarnation"]), "config_gate_members_invalid")
      _require(_is_http_url(member["advertise"]), "config_gate_members_invalid")
      _require(_absolute(member["data_dir"]), "config_gate_members_invalid")
   _require(entry["node_id"] != final["node_id"], "config_gate_members_invalid")
   _require(isinstance(final["ssh_target"], str) and _SSH_TARGET.fullmatch(final["ssh_target"])
            and not final["ssh_target"].startswith("-"), "config_gate_members_invalid")
   _require(_absolute(final["python"]) and _absolute(final["checkout"]), "config_gate_members_invalid")
   seed = os.path.normpath(seed_state_root)
   gate_dir = os.path.dirname(seed)
   entry_dir = os.path.normpath(entry["data_dir"])
   _require(_inside(entry_dir, gate_dir) and entry_dir != gate_dir, "gate_member_dir_not_gate_scoped:entry")
   _require(not _inside(entry_dir, seed), "gate_member_dir_inside_seed:entry")
   # node-2's dir lives on the laptop: gate-scoped means it carries the gate dir's name.
   final_dir = PurePosixPath(os.path.normpath(final["data_dir"]))
   _require(os.path.basename(gate_dir) in final_dir.parts[:-1], "gate_member_dir_not_gate_scoped:final")
   return dict(document)


def validate_fleet_seed(document: Any) -> dict[str, Any]:
   _require(isinstance(document, Mapping) and set(document) == FLEET_FIELDS, "config_fleet_seed_invalid")
   _require(_absolute(document["launch_agent"]) and _absolute(document["data_dir"]), "config_fleet_seed_invalid")
   _require(_is_http_url(document["url"]), "config_fleet_seed_invalid")
   roots = document["protected_roots"]
   _require(isinstance(roots, list) and all(_absolute(r) for r in roots), "config_fleet_seed_invalid")
   return dict(document)


# -- the fleet seed (seed-r7) is refused --------------------------------------------------------


def _agent_arguments(path: str) -> list[str]:
   try:
      document = plistlib.loads(Path(path).read_bytes())
      arguments = document["ProgramArguments"]
   except (OSError, ValueError, KeyError, TypeError, plistlib.InvalidFileException) as error:
      raise MembershipError("fleet_seed_launch_agent_unreadable") from error
   _require(isinstance(arguments, list) and all(isinstance(a, str) for a in arguments), "fleet_seed_launch_agent_unreadable")
   _require("mycelium_seed" in arguments, "fleet_seed_launch_agent_not_a_seed")
   return arguments


def _flag(arguments: list[str], name: str) -> str | None:
   return arguments[arguments.index(name) + 1] if name in arguments[:-1] else None


def fleet_identity(fleet: Mapping[str, Any]) -> dict[str, Any]:
   """Every path, origin and swarm id of the fleet seed: the config block and its launchd agent."""

   arguments = _agent_arguments(fleet["launch_agent"])
   paths = [fleet["data_dir"], *fleet["protected_roots"]]
   origins = {_origin(fleet["url"])}
   swarms: set[str] = set()
   data_dir = _flag(arguments, "--data-dir")
   if data_dir is not None:
      paths.append(data_dir)
   url = _flag(arguments, "--advertised-url")
   if url is not None:
      origins.add(_origin(url))
   bind, port = _flag(arguments, "--bind"), _flag(arguments, "--port")
   if bind is not None and port is not None and port.isdigit():
      origins.add((bind.lower(), int(port)))
   swarm = _flag(arguments, "--swarm-id")
   if swarm is not None:
      swarms.add(swarm)
   return {"paths": [os.path.normpath(p) for p in paths], "origins": {o for o in origins if o}, "swarm_ids": swarms}


def _local(path: str) -> str:
   return os.path.realpath(path)


def fleet_refusals(config: Mapping[str, Any]) -> list[str]:
   """``fleet_seed_refused:<field>`` for every configured gate path/URL that is the fleet seed's."""

   fleet = fleet_identity(config["fleet_seed"])
   members = config["gate_members"]
   gate_paths = {
      "seed_state_root": _local(config["seed_state_root"]),
      "work_dir": _local(config["work_dir"]),
      "gate_members.entry.data_dir": _local(members["entry"]["data_dir"]),
      # Remote (laptop) path: compared as written; the laptop's fleet roots are protected_roots.
      "gate_members.final.data_dir": os.path.normpath(members["final"]["data_dir"]),
   }
   fleet_paths = sorted({p for path in fleet["paths"] for p in (path, _local(path))})
   refusals = [
      f"fleet_seed_refused:{name}"
      for name, path in gate_paths.items()
      if any(_inside(path, f) or _inside(f, path) for f in fleet_paths)
   ]
   gate = config["gate_seed"]
   for name, url in (("seed_url", config["seed_url"]), ("gate_seed.advertised_url", gate["advertised_url"])):
      if _origin(url) in fleet["origins"]:
         refusals.append(f"fleet_seed_refused:{name}")
   if (gate["bind"].lower(), gate["port"]) in fleet["origins"]:
      refusals.append("fleet_seed_refused:gate_seed.bind")
   if gate["swarm_id"] in fleet["swarm_ids"]:
      refusals.append("fleet_seed_refused:gate_seed.swarm_id")
   return refusals


# -- the gate seed's durable store (read-only) ---------------------------------------------------


def read_gate_seed(root: str | Path) -> dict[str, Any]:
   """Binding and members, from one read-only transaction (never a write, never the fleet seed)."""

   database = Path(root) / "state.sqlite3"
   _require(database.is_file(), "gate_seed_state_missing")
   uri = f"file:{quote(str(database), safe='/')}?mode=ro"
   try:
      connection = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
   except sqlite3.Error as error:
      raise MembershipError("gate_seed_state_unreadable") from error
   try:
      connection.row_factory = sqlite3.Row
      connection.execute("PRAGMA query_only = ON")
      connection.execute("BEGIN")
      binding = {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM seed_metadata")}
      members = {row["node_id"]: dict(row) for row in connection.execute(
         "SELECT node_id, endpoint_id, generation, lifecycle_state, lease_expires_at, incarnation, peer_class "
         "FROM seed_members")}
      connection.execute("COMMIT")
   except sqlite3.Error as error:
      raise MembershipError("gate_seed_state_unreadable") from error
   finally:
      connection.close()
   return {"binding": {"swarm_id": binding.get("swarm_id"), "seed_key_digest": binding.get("seed_key_digest")},
           "members": members}


def revoke(root: str | Path, node_id: str, generation: int) -> dict[str, Any]:
   """Fence one gate member generation (the seed operator path the A12 driver also uses)."""

   from mycelium_seed.operator import revoke_seed_member

   return revoke_seed_member(Path(root), node_id=node_id, expected_generation=generation, reason=REVOKE_REASON)


def gate_seed_members(config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
   """The gate seed's members, after checking it IS the configured gate seed."""

   snapshot = read_gate_seed(config["seed_state_root"])
   binding = snapshot["binding"]
   _require(binding["swarm_id"] == config["gate_seed"]["swarm_id"]
            and binding["seed_key_digest"] == config["seed_key_digest"], "gate_seed_binding_mismatch")
   return snapshot["members"]


def member_is_current(row: Mapping[str, Any] | None, *, endpoint_id: str, incarnation: str, at: float) -> bool:
   return (
      row is not None
      and row.get("lifecycle_state") == MEMBER_LIFECYCLE
      and row.get("endpoint_id") == endpoint_id
      and row.get("peer_class") == MEMBER_PEER_CLASS
      and isinstance(row.get("incarnation"), str) and row["incarnation"].startswith(incarnation)
      and isinstance(row.get("lease_expires_at"), (int, float)) and float(row["lease_expires_at"]) > at
   )


# -- local and remote agents --------------------------------------------------------------------


def spawn_local(argv: list[str], *, cwd: str, log_path: Path) -> int:
   """Start the entry member's agent detached; its output goes to a file, never a pipe."""

   environment = {k: v for k, v in os.environ.items() if k not in _SCRUBBED_ENV}
   environment["PYTHONDONTWRITEBYTECODE"] = "1"
   with open(log_path, "ab") as log:
      process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                 env=environment, start_new_session=True)
   return process.pid


def stop_local(pid: int) -> str:
   """SIGTERM, then SIGKILL after 30 s; only a ``mycelium_node`` process is ever signalled."""

   probe = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False)
   if probe.returncode != 0:
      return "absent"
   if "mycelium_node" not in probe.stdout:
      return "not_a_member_agent"
   try:
      os.kill(pid, signal.SIGTERM)
   except ProcessLookupError:
      return "absent"
   for _ in range(30):
      try:
         os.kill(pid, 0)
      except ProcessLookupError:
         return "stopped"
      time.sleep(1.0)
   os.kill(pid, signal.SIGKILL)
   return "killed"


def ssh(target: str, script: str, *, stdin: bytes | None = None, timeout: float = 60.0) -> tuple[int, str]:
   try:
      done = subprocess.run([*_SSH, target, script], input=stdin if stdin is not None else b"",
                            capture_output=True, check=False, timeout=timeout)
   except subprocess.TimeoutExpired:
      return 124, ""
   return done.returncode, done.stdout.decode("utf-8", "replace")


def ssh_argv(target: str, script: str) -> list[str]:
   return [*_SSH, target, script]


def agent_argv(python: str, member: Mapping[str, Any], *, invite: str, endpoint_id: str) -> list[str]:
   return [
      python, "-B", "-m", "mycelium_node", "--data-dir", member["data_dir"], "--join-bundle-file", invite,
      "--node-id", member["node_id"], "--peer-class", MEMBER_PEER_CLASS, "--membership-endpoint-id", endpoint_id,
      "--advertise", member["advertise"], "--membership-control-only", "--lifecycle-state", MEMBER_LIFECYCLE,
      "--incarnation", member["incarnation"],
   ]


def _remote_paths(final: Mapping[str, Any]) -> dict[str, str]:
   base = os.path.normpath(final["data_dir"])
   return {"dir": base, "invite": base + ".invite.json", "pid": base + ".pid", "log": base + ".log",
           "parent": os.path.dirname(base)}


def remote_copy_invite_script(final: Mapping[str, Any], *, seed_key_digest: str, swarm_id: str) -> str:
   """Receive the invite on stdin; refuse a member dir that holds another seed's membership."""

   q, paths = shlex.quote, _remote_paths(final)
   state = paths["dir"] + "/membership.state.json"
   digest_field = '"seed_key_digest":"' + seed_key_digest + '"'
   swarm_field = '"swarm_id":"' + swarm_id + '"'
   return (
      f"set -eu; umask 077; mkdir -p {q(paths['parent'])}; f={q(state)}; "
      f"if [ -e \"$f\" ]; then grep -q {q(digest_field)} \"$f\" && grep -q {q(swarm_field)} \"$f\" "
      f"|| exit {FOREIGN_STATE_EXIT}; rm -f \"$f\"; fi; cat > {q(paths['invite'])}"
   )


def remote_start_script(final: Mapping[str, Any], *, endpoint_id: str) -> str:
   q, paths = shlex.quote, _remote_paths(final)
   argv = agent_argv(final["python"], final, invite=paths["invite"], endpoint_id=endpoint_id)
   scrub = " ".join(f"-u {name}" for name in _SCRUBBED_ENV)
   pid = q(paths["pid"])
   return (
      f"set -eu; cd {q(final['checkout'])}; "
      f"if [ -e {pid} ] && kill -0 \"$(cat {pid})\" 2>/dev/null; then exit 98; fi; "
      f"nohup env {scrub} PYTHONDONTWRITEBYTECODE=1 {' '.join(q(a) for a in argv)} "
      f">> {q(paths['log'])} 2>&1 < /dev/null & echo $! > {pid}; cat {pid}"
   )


def remote_stop_script(final: Mapping[str, Any]) -> str:
   """Stop the laptop's gate agent (only a ``mycelium_node`` pid) and delete its unused invite."""

   q, paths = shlex.quote, _remote_paths(final)
   return (
      f"p=$(cat {q(paths['pid'])} 2>/dev/null || true); "
      "if [ -n \"$p\" ] && ps -p \"$p\" -o command= | grep -q mycelium_node; then kill \"$p\"; "
      "for i in $(seq 1 30); do kill -0 \"$p\" 2>/dev/null || break; sleep 1; done; "
      "kill -0 \"$p\" 2>/dev/null && kill -9 \"$p\"; fi; "
      f"rm -f {q(paths['pid'])} {q(paths['invite'])}; echo stopped"
   )


def remote_forget_invite_script(final: Mapping[str, Any]) -> str:
   return f"rm -f {shlex.quote(_remote_paths(final)['invite'])}"


def revoke_command(python: str, checkout: str, root: str, node_id: str, generation: str) -> list[str]:
   """The CLI equivalent of the in-process teardown revocation (printed by the dry run)."""

   return [python, "-B", f"{checkout}/scripts/manage_seed_membership.py", "revoke", "--seed-data-dir", root,
           "--node-id", node_id, "--expected-generation", generation, "--reason", REVOKE_REASON]


def clear_local_state(member: Mapping[str, Any], *, seed_key_digest: str, swarm_id: str) -> None:
   """Refuse another seed's membership in the entry dir; clear this gate seed's previous attempt."""

   state = Path(member["data_dir"]) / "membership.state.json"
   if not state.exists():
      return
   try:
      document = json.loads(state.read_text("utf-8"))
   except (OSError, ValueError) as error:
      raise MembershipError(f"gate_member_state_foreign:{member['node_id']}") from error
   _require(isinstance(document, Mapping) and document.get("seed_key_digest") == seed_key_digest
            and document.get("swarm_id") == swarm_id, f"gate_member_state_foreign:{member['node_id']}")
   state.unlink()


def remove_tree(path: Path) -> None:
   if path.exists():
      shutil.rmtree(path)


__all__ = [
   "MembershipError", "agent_argv", "fleet_identity", "fleet_refusals", "gate_seed_members", "member_is_current",
   "read_gate_seed", "remote_copy_invite_script", "remote_start_script", "remote_stop_script", "revoke",
   "revoke_command", "spawn_local", "ssh", "ssh_argv", "stop_local", "validate_fleet_seed", "validate_gate_members",
   "validate_gate_seed",
]
