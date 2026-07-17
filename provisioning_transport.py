#!/usr/bin/env python3
"""HTTP assignment pull and verification-report return for Mycelium provisioning."""
from __future__ import annotations

import argparse
import errno
import fcntl
import hashlib
import hmac
import json
import os
import stat
import threading
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, unquote, urlsplit

import model_manifest as mm
from layer_assignment import compile_layer_assignments, validate_assignment_identity
from route_contract import validate_manual_provisioning_route_v1
from weight_provisioning import artifact_report_errors, audit_provisioning, provision_assignment


CONTROL_PROTOCOL = "mycelium.provisioning_control.v1"
STATUS_PROTOCOL = "mycelium.provisioning_status.v1"
DEFAULT_PORT = 8790
MAX_REPORT_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 2_000_000
# Environment variable name only; never contains a credential.
TOKEN_ENV = "MYCELIUM_PROVISIONING_TOKEN"  # nosec B105


class ProvisioningTransportError(RuntimeError):
   """Remote provisioning-control request failed."""


class ProvisioningConflict(ValueError):
   """A node submitted evidence conflicting with its stored report."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
   """Keep coordinator bearer credentials on the explicitly configured origin."""

   def redirect_request(self, req, fp, code, msg, headers, newurl):
      return None


def _canonical(document: Any) -> str:
   return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _read_json(path: str | Path) -> Any:
   return json.loads(Path(path).expanduser().read_text())


def _resolve_artifact_path(raw: str, base_dir: Path) -> Path:
   path = Path(raw).expanduser()
   if not path.is_absolute():
      path = base_dir / path
   return path.resolve()


def _validate_bundle(
   route_plan: dict[str, Any],
   assignments: list[dict[str, Any]],
   *,
   deployment_id: str | None = None,
   deployment_epoch: int | None = None,
) -> None:
   validate_manual_provisioning_route_v1(route_plan)
   route_nodes = [stage["node_id"] for stage in route_plan["route"]]
   assignment_nodes = [assignment.get("node_id") for assignment in assignments]
   if len(set(assignment_nodes)) != len(assignment_nodes):
      raise ValueError("duplicate assignment node")
   if set(assignment_nodes) != set(route_nodes):
      raise ValueError("assignment nodes do not match route")

   assignment_ids = set()
   identity = None
   route_by_node = {stage["node_id"]: stage for stage in route_plan["route"]}
   for assignment in assignments:
      if assignment.get("protocol") != "mycelium.layer_assignment.v2":
         raise ValueError("invalid assignment protocol")
      node_id = assignment["node_id"]
      assignment_id = assignment.get("assignment_id")
      try:
         parsed_assignment_id = str(uuid.UUID(str(assignment_id)))
      except (TypeError, ValueError) as exc:
         raise ValueError(f"invalid assignment_id for {node_id}") from exc
      if parsed_assignment_id != assignment_id or assignment_id in assignment_ids:
         raise ValueError(f"invalid or duplicate assignment_id for {node_id}")
      assignment_ids.add(assignment_id)
      if assignment.get("range") != route_by_node[node_id].get("range"):
         raise ValueError(f"assignment range does not match route for {node_id}")
      if assignment.get("model_id") != route_plan.get("model", {}).get("model_id"):
         raise ValueError(f"assignment model does not match route for {node_id}")
      current_identity = (
         assignment.get("deployment_id"),
         assignment.get("deployment_epoch"),
         assignment.get("manifest_digest"),
         assignment.get("resolved_commit"),
      )
      if identity is None:
         identity = current_identity
      elif current_identity != identity:
         raise ValueError("assignment deployment identity mismatch")

   if not identity:
      raise ValueError("deployment has no assignments")
   try:
      canonical_deployment_id = str(uuid.UUID(str(identity[0])))
   except (TypeError, ValueError) as exc:
      raise ValueError("deployment_id must be a canonical UUID") from exc
   if identity[0] != canonical_deployment_id:
      raise ValueError("deployment_id must be a canonical UUID")
   if (
      not isinstance(identity[1], int)
      or isinstance(identity[1], bool)
      or identity[1] < 0
   ):
      raise ValueError("deployment_epoch must be a non-negative integer")
   route_model = route_plan.get("model", {})
   if route_model.get("manifest_digest") != identity[2]:
      raise ValueError("route manifest_digest does not match assignments")
   if route_model.get("resolved_commit") != identity[3]:
      raise ValueError("route resolved_commit does not match assignments")
   for assignment in assignments:
      validate_assignment_identity(assignment)
   if deployment_id is not None and identity[0] != deployment_id:
      raise ValueError("deployment_id does not match assignments")
   if deployment_epoch is not None and identity[1] != deployment_epoch:
      raise ValueError("deployment_epoch does not match assignments")


def load_deployment_bundle(
   deployment_path: str | Path,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
   path = Path(deployment_path).expanduser().resolve()
   deployment = _read_json(path)
   if deployment.get("protocol") != "mycelium.weight_provisioning_demo.v1":
      raise ValueError("invalid deployment protocol")
   base_dir = path.parent
   manifest_raw = deployment.get("manifest")
   route_raw = deployment.get("route")
   assignment_raw = deployment.get("assignments")
   if (
      not isinstance(manifest_raw, str)
      or not isinstance(route_raw, str)
      or not isinstance(assignment_raw, dict)
      or not assignment_raw
   ):
      raise ValueError("deployment requires manifest, route, and assignments")
   manifest = _read_json(_resolve_artifact_path(manifest_raw, base_dir))
   route_plan = _read_json(_resolve_artifact_path(route_raw, base_dir))
   assignments = []
   for declared_node, assignment_path in assignment_raw.items():
      if not isinstance(declared_node, str) or not isinstance(assignment_path, str):
         raise ValueError("invalid deployment assignment reference")
      assignment = _read_json(_resolve_artifact_path(assignment_path, base_dir))
      if assignment.get("node_id") != declared_node:
         raise ValueError(f"assignment reference node mismatch: {declared_node}")
      assignments.append(assignment)
   _validate_bundle(
      route_plan,
      assignments,
      deployment_id=deployment.get("deployment_id"),
      deployment_epoch=deployment.get("deployment_epoch"),
   )
   if not isinstance(manifest, dict) or manifest.get("protocol") != "mycelium.model_manifest.v1":
      raise ValueError("invalid model manifest protocol")
   if not mm.verify_manifest_digest(manifest):
      raise ValueError("model manifest digest mismatch")
   route_model = route_plan["model"]
   manifest_identity = {
      "model_id": manifest.get("model_id"),
      "num_layers": manifest.get("num_layers"),
      "resolved_commit": manifest.get("resolved_commit"),
      "manifest_digest": mm.manifest_digest_ref(manifest),
   }
   for field, expected in manifest_identity.items():
      if route_model.get(field) != expected:
         raise ValueError(f"route {field} does not match model manifest")

   first_assignment = assignments[0]
   expected_assignments = compile_layer_assignments(
      route_plan=route_plan,
      manifest=manifest,
      deployment_id=first_assignment["deployment_id"],
      deployment_epoch=first_assignment["deployment_epoch"],
      cache_roots={item["node_id"]: item["artifact_cache_root"] for item in assignments},
      runtime_by_node={item["node_id"]: item["runtime"] for item in assignments},
   )
   expected_by_node = {item["node_id"]: item for item in expected_assignments}
   for assignment in assignments:
      if assignment != expected_by_node.get(assignment["node_id"]):
         raise ValueError(
            f"assignment for {assignment['node_id']} does not match manifest-derived assignment"
         )
   return route_plan, assignments, deployment


def _report_errors(assignment: dict[str, Any], report: dict[str, Any]) -> list[str]:
   return artifact_report_errors(assignment, report)


def _fsync_directory(path: Path) -> None:
   descriptor = os.open(path, os.O_RDONLY)
   try:
      os.fsync(descriptor)
   finally:
      os.close(descriptor)


def _atomic_write_json(path: Path, document: Any) -> None:
   path.parent.mkdir(parents=True, exist_ok=True)
   temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
   try:
      with temporary.open("w", encoding="utf-8") as handle:
         handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
         handle.flush()
         os.fsync(handle.fileno())
      temporary.replace(path)
      _fsync_directory(path.parent)
   finally:
      temporary.unlink(missing_ok=True)


def _report_fingerprint(report: dict[str, Any]) -> str:
   """Fingerprint immutable verification proof, excluding acquisition telemetry."""
   proof = {
      field: report.get(field)
      for field in (
         "protocol",
         "deployment_id",
         "deployment_epoch",
         "assignment_id",
         "node_id",
         "manifest_digest",
         "resolved_commit",
         "range",
         "artifact_cache_root",
         "verified_tensor_prefixes",
         "verified_tensor_count",
         "expected_bytes",
         "ready_for_load",
         "route_ready",
      )
   }
   proof["verified_files"] = sorted(
      (
         {
            "path": item.get("path"),
            "size_bytes": item.get("size_bytes"),
            "content_digest": item.get("content_digest"),
         }
         for item in report.get("verified_files", [])
         if isinstance(item, dict)
      ),
      key=lambda item: str(item["path"]),
   )
   return hashlib.sha256(_canonical(proof).encode("utf-8")).hexdigest()


class ProvisioningCoordinator:
   def __init__(
      self,
      route_plan: dict[str, Any],
      assignments: list[dict[str, Any]],
      *,
      bearer_token: str,
      report_dir: str | Path,
   ):
      _validate_bundle(route_plan, assignments)
      if not isinstance(bearer_token, str) or len(bearer_token.encode("utf-8")) < 32:
         raise ValueError("bearer token must contain at least 32 bytes")
      self.route_plan = json.loads(json.dumps(route_plan))
      self.node_order = [stage["node_id"] for stage in self.route_plan["route"]]
      self.assignments = {
         assignment["node_id"]: json.loads(json.dumps(assignment))
         for assignment in assignments
      }
      first = assignments[0]
      self.deployment_id = first["deployment_id"]
      self.deployment_epoch = first["deployment_epoch"]
      self._bearer_token = bearer_token
      self.report_root = Path(report_dir).expanduser().resolve()
      self.report_dir = self.report_root / self.deployment_id / str(self.deployment_epoch)
      self.report_dir.mkdir(parents=True, exist_ok=True)
      self._reports: dict[str, dict[str, Any]] = {}
      self._audit: dict[str, Any] | None = None
      self._lock = threading.RLock()
      self._recover_reports()

   def _report_path(self, assignment: dict[str, Any]) -> Path:
      return self.report_dir / f"report-{assignment['assignment_id']}.json"

   def _refresh_audit(self) -> None:
      if set(self._reports) != set(self.assignments):
         self._audit = None
         return
      ordered_assignments = [self.assignments[node] for node in self.node_order]
      ordered_reports = [self._reports[node] for node in self.node_order]
      audit = audit_provisioning(
         self.route_plan,
         ordered_assignments,
         ordered_reports,
      )
      _atomic_write_json(self.report_dir / "provisioning-audit.json", audit)
      self._audit = audit

   def _recover_reports(self) -> None:
      for node_id, assignment in self.assignments.items():
         path = self._report_path(assignment)
         if not path.exists():
            continue
         report = _read_json(path)
         if not isinstance(report, dict):
            raise ValueError(f"persisted report for {node_id} is not a JSON object")
         errors = _report_errors(assignment, report)
         if errors:
            raise ValueError("invalid persisted report: " + "; ".join(errors))
         self._reports[node_id] = report
      self._refresh_audit()

   def authorized(self, token: str) -> bool:
      return hmac.compare_digest(self._bearer_token, token)

   def assignment(self, deployment_id: str, node_id: str) -> dict[str, Any]:
      if deployment_id != self.deployment_id:
         raise KeyError("deployment not found")
      assignment = self.assignments.get(node_id)
      if assignment is None:
         raise KeyError("assignment not found")
      return json.loads(json.dumps(assignment))

   def submit_report(
      self,
      deployment_id: str,
      node_id: str,
      report: dict[str, Any],
   ) -> dict[str, Any]:
      assignment = self.assignment(deployment_id, node_id)
      if not isinstance(report, dict):
         raise ValueError("report must be a JSON object")
      errors = _report_errors(assignment, report)
      if errors:
         raise ValueError("; ".join(errors))
      with self._lock:
         existing = self._reports.get(node_id)
         if existing is not None:
            if _report_fingerprint(existing) != _report_fingerprint(report):
               raise ProvisioningConflict(f"conflicting report for {node_id}")
            audit_path = self.report_dir / "provisioning-audit.json"
            if self._audit is None or not audit_path.is_file():
               self._refresh_audit()
            return self.status()
         _atomic_write_json(self._report_path(assignment), report)
         self._reports[node_id] = json.loads(json.dumps(report))
         self._refresh_audit()
         return self.status()

   def status(self) -> dict[str, Any]:
      with self._lock:
         expected = list(self.node_order)
         reported = [node for node in expected if node in self._reports]
         pending = [node for node in expected if node not in self._reports]
         if pending:
            state = "pending_reports"
         elif self._audit is not None and self._audit.get("all_assignments_verified") is True:
            state = "verified"
         else:
            state = "failed"
         return json.loads(json.dumps({
            "protocol": STATUS_PROTOCOL,
            "deployment_id": self.deployment_id,
            "deployment_epoch": self.deployment_epoch,
            "state": state,
            "expected_nodes": expected,
            "reported_nodes": reported,
            "pending_nodes": pending,
            "all_reports_received": not pending,
            "audit": self._audit,
         }))


class _ProvisioningHandler(BaseHTTPRequestHandler):
   server_version = "MyceliumProvisioning/0.1"

   @property
   def state(self) -> ProvisioningCoordinator:
      return self.server.state  # type: ignore[attr-defined]

   @property
   def quiet(self) -> bool:
      return self.server.quiet  # type: ignore[attr-defined]

   def log_message(self, fmt: str, *args: Any) -> None:
      if not self.quiet:
         super().log_message(fmt, *args)

   def _send_json(self, document: Any, status: int = 200) -> None:
      body = json.dumps(document, indent=2, sort_keys=True).encode("utf-8")
      self.send_response(status)
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(body)))
      self.send_header("Cache-Control", "no-store")
      self.end_headers()
      self.wfile.write(body)

   def _authorized(self) -> bool:
      raw = self.headers.get("Authorization", "")
      scheme, separator, token = raw.partition(" ")
      if separator and scheme.lower() == "bearer" and self.state.authorized(token):
         return True
      self.send_response(401)
      body = json.dumps({"ok": False, "error": "unauthorized"}).encode("utf-8")
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(body)))
      self.send_header("Cache-Control", "no-store")
      self.send_header("WWW-Authenticate", "Bearer")
      self.end_headers()
      self.wfile.write(body)
      return False

   def _path_parts(self) -> list[str]:
      return [unquote(part) for part in urlsplit(self.path).path.strip("/").split("/") if part]

   def _read_json(self) -> Any:
      raw_length = self.headers.get("Content-Length")
      if raw_length is None:
         raise ValueError("Content-Length is required")
      try:
         length = int(raw_length)
      except ValueError as exc:
         raise ValueError("invalid Content-Length") from exc
      if length <= 0 or length > MAX_REPORT_BYTES:
         raise ValueError("invalid report body size")
      return json.loads(self.rfile.read(length).decode("utf-8"))

   def do_GET(self) -> None:  # noqa: N802
      parts = self._path_parts()
      if parts == ["health"]:
         self._send_json({
            "ok": True,
            "protocol": CONTROL_PROTOCOL,
            "deployment_id": self.state.deployment_id,
         })
         return
      if not self._authorized():
         return
      try:
         if len(parts) == 5 and parts[:2] == ["v1", "deployments"] and parts[3] == "assignments":
            self._send_json(self.state.assignment(parts[2], parts[4]))
         elif len(parts) == 4 and parts[:2] == ["v1", "deployments"] and parts[3] == "status":
            if parts[2] != self.state.deployment_id:
               raise KeyError("deployment not found")
            self._send_json(self.state.status())
         else:
            self._send_json({"ok": False, "error": "not_found"}, status=404)
      except KeyError as exc:
         self._send_json({"ok": False, "error": str(exc.args[0])}, status=404)

   def do_POST(self) -> None:  # noqa: N802
      if not self._authorized():
         return
      parts = self._path_parts()
      if not (
         len(parts) == 5
         and parts[:2] == ["v1", "deployments"]
         and parts[3] == "reports"
      ):
         self._send_json({"ok": False, "error": "not_found"}, status=404)
         return
      try:
         report = self._read_json()
         status = self.state.submit_report(parts[2], parts[4], report)
         self._send_json(status)
      except KeyError as exc:
         self._send_json({"ok": False, "error": str(exc.args[0])}, status=404)
      except ProvisioningConflict as exc:
         self._send_json({"ok": False, "error": str(exc)}, status=409)
      except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
         self._send_json({"ok": False, "error": str(exc)}, status=400)


def start_provisioning_server(
   host: str,
   port: int,
   state: ProvisioningCoordinator,
   *,
   quiet: bool = True,
) -> ThreadingHTTPServer:
   server = ThreadingHTTPServer((host, int(port)), _ProvisioningHandler)
   server.state = state  # type: ignore[attr-defined]
   server.quiet = quiet  # type: ignore[attr-defined]
   thread = threading.Thread(
      target=server.serve_forever,
      name="mycelium-provisioning-http",
      daemon=True,
   )
   thread.start()
   server.worker_thread = thread  # type: ignore[attr-defined]
   return server


def _read_bounded_json_response(response: Any) -> Any:
   content_length = response.headers.get("Content-Length")
   if content_length is not None:
      try:
         declared_length = int(content_length)
      except ValueError as exc:
         raise ProvisioningTransportError("invalid response Content-Length") from exc
      if declared_length < 0 or declared_length > MAX_RESPONSE_BYTES:
         raise ProvisioningTransportError("response body too large")
   body = response.read(MAX_RESPONSE_BYTES + 1)
   if len(body) > MAX_RESPONSE_BYTES:
      raise ProvisioningTransportError("response body too large")
   try:
      return json.loads(body)
   except (json.JSONDecodeError, UnicodeDecodeError) as exc:
      raise ProvisioningTransportError("coordinator returned invalid JSON") from exc


def _remote_json(
   method: str,
   url: str,
   bearer_token: str,
   payload: dict[str, Any] | None = None,
   timeout: float = 30.0,
) -> Any:
   parsed = urlsplit(url)
   if parsed.scheme not in {"http", "https"} or not parsed.hostname:
      raise ProvisioningTransportError("coordinator URL must use http or https")
   body = None if payload is None else _canonical(payload).encode("utf-8")
   request = urllib.request.Request(
      url,
      data=body,
      method=method,
      headers={
         "Accept": "application/json",
         "Authorization": f"Bearer {bearer_token}",
         "Content-Type": "application/json",
         "User-Agent": "MyceliumProvisioningPeer/0.1",
      },
   )
   opener = urllib.request.build_opener(_NoRedirect())
   try:
      with opener.open(request, timeout=timeout) as response:
         return _read_bounded_json_response(response)
   except urllib.error.HTTPError as exc:
      try:
         try:
            document = _read_bounded_json_response(exc)
            error = document.get("error") if isinstance(document, dict) else None
         except ProvisioningTransportError:
            error = None
      finally:
         exc.close()
      raise ProvisioningTransportError(error or f"HTTP {exc.code}") from exc
   except urllib.error.URLError as exc:
      raise ProvisioningTransportError(str(exc.reason)) from exc
   except OSError as exc:
      raise ProvisioningTransportError(str(exc)) from exc


def _deployment_url(base_url: str, deployment_id: str, suffix: str) -> str:
   return (
      base_url.rstrip("/")
      + "/v1/deployments/"
      + quote(deployment_id, safe="")
      + "/"
      + suffix
   )


def fetch_assignment(
   coordinator_url: str,
   deployment_id: str,
   node_id: str,
   bearer_token: str,
   *,
   timeout: float = 30.0,
) -> dict[str, Any]:
   url = _deployment_url(
      coordinator_url,
      deployment_id,
      "assignments/" + quote(node_id, safe=""),
   )
   assignment = _remote_json("GET", url, bearer_token, timeout=timeout)
   if not isinstance(assignment, dict):
      raise ProvisioningTransportError("coordinator assignment response must be a JSON object")
   if assignment.get("node_id") != node_id or assignment.get("deployment_id") != deployment_id:
      raise ProvisioningTransportError("coordinator returned wrong assignment identity")
   return assignment


def _validate_status_response(status: Any, deployment_id: str) -> dict[str, Any]:
   if not isinstance(status, dict):
      raise ProvisioningTransportError("coordinator status must be a JSON object")
   if status.get("protocol") != STATUS_PROTOCOL:
      raise ProvisioningTransportError("coordinator returned wrong status protocol")
   if status.get("deployment_id") != deployment_id:
      raise ProvisioningTransportError("coordinator returned wrong status deployment identity")
   epoch = status.get("deployment_epoch")
   if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
      raise ProvisioningTransportError("coordinator returned invalid deployment epoch")

   node_lists: dict[str, list[str]] = {}
   for field in ("expected_nodes", "reported_nodes", "pending_nodes"):
      value = status.get(field)
      if (
         not isinstance(value, list)
         or any(not isinstance(node, str) or not node for node in value)
         or len(value) != len(set(value))
      ):
         raise ProvisioningTransportError(f"coordinator returned invalid {field}")
      node_lists[field] = value
   expected = set(node_lists["expected_nodes"])
   reported = set(node_lists["reported_nodes"])
   pending = set(node_lists["pending_nodes"])
   if reported & pending or reported | pending != expected:
      raise ProvisioningTransportError("coordinator status node partition is inconsistent")

   state = status.get("state")
   all_received = status.get("all_reports_received")
   audit = status.get("audit")
   if not isinstance(all_received, bool):
      raise ProvisioningTransportError("coordinator returned invalid completion flag")
   if state == "pending_reports":
      valid = not all_received and bool(pending) and audit is None
   elif state == "verified":
      valid = (
         all_received
         and not pending
         and isinstance(audit, dict)
         and audit.get("all_assignments_verified") is True
      )
   elif state == "failed":
      valid = (
         all_received
         and not pending
         and isinstance(audit, dict)
         and audit.get("all_assignments_verified") is False
      )
   else:
      raise ProvisioningTransportError("coordinator returned unknown provisioning state")
   if not valid:
      raise ProvisioningTransportError("coordinator status state is internally inconsistent")
   return status


def submit_report(
   coordinator_url: str,
   deployment_id: str,
   node_id: str,
   report: dict[str, Any],
   bearer_token: str,
   *,
   timeout: float = 30.0,
) -> dict[str, Any]:
   url = _deployment_url(
      coordinator_url,
      deployment_id,
      "reports/" + quote(node_id, safe=""),
   )
   status = _remote_json("POST", url, bearer_token, payload=report, timeout=timeout)
   return _validate_status_response(status, deployment_id)


def fetch_status(
   coordinator_url: str,
   deployment_id: str,
   bearer_token: str,
   *,
   timeout: float = 30.0,
) -> dict[str, Any]:
   url = _deployment_url(coordinator_url, deployment_id, "status")
   status = _remote_json("GET", url, bearer_token, timeout=timeout)
   return _validate_status_response(status, deployment_id)


def _claim_peer_report(path: str | Path, force: bool) -> tuple[Path, int]:
   destination = Path(path).expanduser().resolve()
   destination.parent.mkdir(parents=True, exist_ok=True)
   lock_path = destination.with_name(f".{destination.name}.lock")
   flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
   try:
      descriptor = os.open(lock_path, flags, 0o600)
   except OSError as exc:
      if exc.errno == errno.ELOOP:
         raise ValueError(f"peer report lock must not be a symlink: {lock_path}") from exc
      raise
   metadata = os.fstat(descriptor)
   if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
      os.close(descriptor)
      raise ValueError("peer report lock must be a regular file owned by current user")
   os.fchmod(descriptor, 0o600)
   try:
      try:
         fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
      except BlockingIOError as exc:
         raise ValueError(f"peer report operation already in progress: {destination}") from exc
      os.ftruncate(descriptor, 0)
      os.write(descriptor, f"pid={os.getpid()}\n".encode("utf-8"))
      os.fsync(descriptor)
      _fsync_directory(destination.parent)
      if destination.exists():
         if not force:
            raise ValueError(
               f"report already exists: {destination}; use resubmit instead of reprovisioning"
            )
         backup = destination.with_name(
            f"{destination.name}.previous-{uuid.uuid4().hex}"
         )
         destination.replace(backup)
         _fsync_directory(destination.parent)
      return destination, descriptor
   except Exception:
      fcntl.flock(descriptor, fcntl.LOCK_UN)
      os.close(descriptor)
      raise


def _release_peer_report_claim(descriptor: int) -> None:
   fcntl.flock(descriptor, fcntl.LOCK_UN)
   os.close(descriptor)


def run_remote_peer(
   coordinator_url: str,
   deployment_id: str,
   node_id: str,
   bearer_token: str,
   *,
   report_path: str | Path,
   local_files_only: bool = False,
   force: bool = False,
   provisioner: Callable[..., dict[str, Any]] = provision_assignment,
   submitter: Callable[..., dict[str, Any]] = submit_report,
) -> dict[str, Any]:
   destination, lock_descriptor = _claim_peer_report(report_path, force)
   try:
      assignment = fetch_assignment(
         coordinator_url,
         deployment_id,
         node_id,
         bearer_token,
      )
      report = provisioner(assignment, local_files_only=local_files_only)
      _write_peer_report(destination, report)
      status = submitter(
         coordinator_url,
         deployment_id,
         node_id,
         report,
         bearer_token,
      )
      return {
         "assignment": assignment,
         "report": report,
         "report_path": str(destination),
         "status": status,
      }
   finally:
      _release_peer_report_claim(lock_descriptor)


def submit_persisted_report(
   coordinator_url: str,
   deployment_id: str,
   node_id: str,
   bearer_token: str,
   *,
   report_path: str | Path,
) -> dict[str, Any]:
   report = _read_json(report_path)
   if not isinstance(report, dict):
      raise ValueError("persisted report must be a JSON object")
   if report.get("deployment_id") != deployment_id:
      raise ValueError("persisted report deployment_id mismatch")
   if report.get("node_id") != node_id:
      raise ValueError("persisted report node_id mismatch")
   return submit_report(
      coordinator_url,
      deployment_id,
      node_id,
      report,
      bearer_token,
   )


def load_bearer_token(token_file: str | Path | None = None) -> str:
   if token_file is not None:
      token_path = Path(token_file).expanduser()
      flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
      try:
         descriptor = os.open(token_path, flags)
      except OSError as exc:
         if exc.errno == errno.ELOOP:
            raise ValueError("token file must be a regular file, not a symlink") from exc
         raise
      try:
         metadata = os.fstat(descriptor)
         if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid():
            raise ValueError("token file must be a regular file owned by current user")
         if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("token file permissions must deny group and world access (mode 0600)")
         with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            token = handle.read(4097).strip()
         if len(token.encode("utf-8")) > 4096:
            raise ValueError("token file exceeds 4096 bytes")
      finally:
         if descriptor >= 0:
            os.close(descriptor)
   else:
      token = os.environ.get(TOKEN_ENV, "").strip()
   if len(token.encode("utf-8")) < 32:
      source = "token file" if token_file is not None else TOKEN_ENV
      raise ValueError(f"{source} must provide at least 32 bytes")
   return token


def _write_peer_report(path: str | Path, report: dict[str, Any]) -> None:
   destination = Path(path).expanduser().resolve()
   if destination.exists():
      raise ValueError(f"claimed report destination unexpectedly exists: {destination}")
   _atomic_write_json(destination, report)


def command_coordinator(args: argparse.Namespace) -> int:
   route, assignments, deployment = load_deployment_bundle(args.deployment)
   token = load_bearer_token(args.token_file)
   report_dir = (
      Path(args.report_dir).expanduser().resolve()
      if args.report_dir
      else Path(args.deployment).expanduser().resolve().parent / "http-reports"
   )
   state = ProvisioningCoordinator(
      route,
      assignments,
      bearer_token=token,
      report_dir=report_dir,
   )
   server = ThreadingHTTPServer((args.host, args.port), _ProvisioningHandler)
   server.state = state  # type: ignore[attr-defined]
   server.quiet = args.quiet  # type: ignore[attr-defined]
   host, port = server.server_address
   print(json.dumps({
      "ok": True,
      "protocol": CONTROL_PROTOCOL,
      "deployment_id": deployment["deployment_id"],
      "listen": f"http://{host}:{port}",
      "report_dir": str(state.report_dir),
      "security_boundary": "bearer-authenticated HTTP; use trusted LAN or HTTPS termination",
   }, indent=2, sort_keys=True), flush=True)
   try:
      server.serve_forever()
   except KeyboardInterrupt:
      return 0
   finally:
      server.server_close()


def command_peer(args: argparse.Namespace) -> int:
   token = load_bearer_token(args.token_file)
   result = run_remote_peer(
      args.coordinator_url,
      args.deployment_id,
      args.node_id,
      token,
      report_path=args.report,
      local_files_only=args.local_files_only,
      force=args.force,
   )
   print(json.dumps({
      "ok": True,
      "node_id": args.node_id,
      "assignment_id": result["assignment"]["assignment_id"],
      "ready_for_load": result["report"]["ready_for_load"],
      "route_ready": result["report"]["route_ready"],
      "network_download_bytes": result["report"]["network_download_bytes"],
      "cache_hit_bytes": result["report"]["cache_hit_bytes"],
      "coordinator_status": result["status"],
      "local_report": result["report_path"],
   }, indent=2, sort_keys=True))
   return 0


def command_resubmit(args: argparse.Namespace) -> int:
   token = load_bearer_token(args.token_file)
   status = submit_persisted_report(
      args.coordinator_url,
      args.deployment_id,
      args.node_id,
      token,
      report_path=args.report,
   )
   print(json.dumps({
      "ok": True,
      "node_id": args.node_id,
      "local_report": str(Path(args.report).expanduser().resolve()),
      "coordinator_status": status,
   }, indent=2, sort_keys=True))
   return 0


def command_status(args: argparse.Namespace) -> int:
   token = load_bearer_token(args.token_file)
   status = fetch_status(args.coordinator_url, args.deployment_id, token)
   print(json.dumps(status, indent=2, sort_keys=True))
   return {
      "verified": 0,
      "failed": 2,
      "pending_reports": 3,
   }[status["state"]]


def main(argv: list[str] | None = None) -> int:
   parser = argparse.ArgumentParser(description="Mycelium provisioning transport without SSH")
   subparsers = parser.add_subparsers(dest="command", required=True)

   coordinator = subparsers.add_parser("coordinator", help="serve assignments and receive reports")
   coordinator.add_argument("--deployment", required=True)
   coordinator.add_argument("--host", default="127.0.0.1")
   coordinator.add_argument("--port", type=int, default=DEFAULT_PORT)
   coordinator.add_argument("--token-file", help=f"token file; otherwise use {TOKEN_ENV}")
   coordinator.add_argument("--report-dir")
   coordinator.add_argument("--quiet", action="store_true")
   coordinator.set_defaults(handler=command_coordinator)

   peer = subparsers.add_parser("peer", help="fetch assignment, provision locally, return report")
   peer.add_argument("--coordinator-url", required=True)
   peer.add_argument("--deployment-id", required=True)
   peer.add_argument("--node-id", required=True)
   peer.add_argument("--token-file", help=f"token file; otherwise use {TOKEN_ENV}")
   peer.add_argument("--report", required=True, help="durable local verification report written before submit")
   peer.add_argument("--local-files-only", action="store_true")
   peer.add_argument("--force", action="store_true")
   peer.set_defaults(handler=command_peer)

   resubmit = subparsers.add_parser("resubmit", help="resubmit an existing local report without provisioning")
   resubmit.add_argument("--coordinator-url", required=True)
   resubmit.add_argument("--deployment-id", required=True)
   resubmit.add_argument("--node-id", required=True)
   resubmit.add_argument("--token-file", help=f"token file; otherwise use {TOKEN_ENV}")
   resubmit.add_argument("--report", required=True)
   resubmit.set_defaults(handler=command_resubmit)

   status = subparsers.add_parser("status", help="read coordinator provisioning status")
   status.add_argument("--coordinator-url", required=True)
   status.add_argument("--deployment-id", required=True)
   status.add_argument("--token-file", help=f"token file; otherwise use {TOKEN_ENV}")
   status.set_defaults(handler=command_status)

   args = parser.parse_args(argv)
   return args.handler(args)


if __name__ == "__main__":
   raise SystemExit(main())
