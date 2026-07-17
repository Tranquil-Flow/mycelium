#!/usr/bin/env python3
import contextlib
import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import layer_assignment as la
import model_manifest as mm
import provisioning_transport as pt


DEPLOYMENT_ID = "12345678-1234-5678-1234-567812345678"
TOKEN = "moonlit-demo-token-that-is-at-least-32-bytes"


class ProvisioningTransportTests(unittest.TestCase):
   def route(self):
      return {
         "ok": True,
         "protocol": "mycelium.route_plan.v2",
         "model": {
            "model_id": "org/model",
            "num_layers": 2,
            "manifest_digest": "sha256:" + "a" * 64,
            "resolved_commit": "b" * 40,
         },
         "route": [
            {
               "node_id": "node-a",
               "range": {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1},
            },
            {
               "node_id": "node-b",
               "range": {"start_layer": 1, "end_layer_exclusive": 2, "layer_count": 1},
            },
         ],
         "node_order": ["node-a", "node-b"],
      }

   def assignment(self, node_id, start, cache_root):
      assignment = {
         "protocol": "mycelium.layer_assignment.v1",
         "deployment_id": DEPLOYMENT_ID,
         "deployment_epoch": 1,
         "assignment_id": "pending",
         "node_id": node_id,
         "manifest_digest": "sha256:" + "a" * 64,
         "model_id": "org/model",
         "resolved_commit": "b" * 40,
         "range": {
            "start_layer": start,
            "end_layer_exclusive": start + 1,
            "layer_count": 1,
         },
         "components": ["decoder"],
         "expected_tensor_prefixes": [f"h.{start}."],
         "expected_tensor_keys": [f"h.{start}.weight"],
         "files": [{
            "path": f"shard-{start}.safetensors",
            "size_bytes": 100 + start,
            "content_digest": "sha256:" + str(start + 1) * 64,
         }],
         "artifact_cache_root": str(cache_root),
         "runtime": {
            "backend": "artifact_verifier",
            "dtype": "source",
            "quantization": "none",
         },
         "route_ready": False,
      }
      assignment["assignment_id"] = la.assignment_id_for(assignment)
      return assignment

   def report(self, assignment):
      assigned_file = assignment["files"][0]
      return {
         "protocol": "mycelium.artifact_verification_report.v1",
         "deployment_id": assignment["deployment_id"],
         "deployment_epoch": assignment["deployment_epoch"],
         "assignment_id": assignment["assignment_id"],
         "node_id": assignment["node_id"],
         "manifest_digest": assignment["manifest_digest"],
         "resolved_commit": assignment["resolved_commit"],
         "range": assignment["range"],
         "artifact_cache_root": assignment["artifact_cache_root"],
         "verified_files": [{
            "path": assigned_file["path"],
            "size_bytes": assigned_file["size_bytes"],
            "content_digest": assigned_file["content_digest"],
         }],
         "verified_tensor_prefixes": assignment["expected_tensor_prefixes"],
         "verified_tensor_count": len(assignment["expected_tensor_keys"]),
         "expected_bytes": assigned_file["size_bytes"],
         "network_download_bytes": assigned_file["size_bytes"],
         "cache_hit_bytes": 0,
         "ready_for_load": True,
         "route_ready": False,
      }

   def start(self, root):
      assignments = [
         self.assignment("node-a", 0, root / "cache-a"),
         self.assignment("node-b", 1, root / "cache-b"),
      ]
      state = pt.ProvisioningCoordinator(
         self.route(),
         assignments,
         bearer_token=TOKEN,
         report_dir=root / "reports",
      )
      server = pt.start_provisioning_server("127.0.0.1", 0, state, quiet=True)
      host, port = server.server_address
      return state, server, f"http://{host}:{port}", assignments

   def test_two_peers_fetch_assignments_submit_reports_and_complete_audit(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         state, server, base_url, expected = self.start(root)
         try:
            fetched_a = pt.fetch_assignment(base_url, DEPLOYMENT_ID, "node-a", TOKEN)
            fetched_b = pt.fetch_assignment(base_url, DEPLOYMENT_ID, "node-b", TOKEN)
            self.assertEqual(fetched_a, expected[0])
            self.assertEqual(fetched_b, expected[1])

            first = pt.submit_report(base_url, DEPLOYMENT_ID, "node-a", self.report(fetched_a), TOKEN)
            self.assertEqual(first["pending_nodes"], ["node-b"])
            self.assertEqual(first["state"], "pending_reports")
            final = pt.submit_report(base_url, DEPLOYMENT_ID, "node-b", self.report(fetched_b), TOKEN)

            self.assertTrue(final["all_reports_received"])
            self.assertEqual(final["state"], "verified")
            self.assertTrue(final["audit"]["all_assignments_verified"])
            self.assertFalse(final["audit"]["route_ready"])
            self.assertEqual(state.status(), final)
            self.assertEqual(len(list(state.report_dir.glob("report-*.json"))), 2)
            self.assertTrue((state.report_dir / "provisioning-audit.json").is_file())
         finally:
            server.shutdown()
            server.server_close()

   def test_assignment_endpoint_rejects_missing_or_wrong_bearer_token(self):
      with tempfile.TemporaryDirectory() as td:
         _, server, base_url, _ = self.start(Path(td))
         try:
            url = f"{base_url}/v1/deployments/{DEPLOYMENT_ID}/assignments/node-a"
            with self.assertRaises(urllib.error.HTTPError) as missing:
               urllib.request.urlopen(url, timeout=5)
            self.assertEqual(missing.exception.code, 401)
            missing.exception.close()

            with self.assertRaisesRegex(pt.ProvisioningTransportError, "unauthorized"):
               pt.fetch_assignment(base_url, DEPLOYMENT_ID, "node-a", "x" * 32)
         finally:
            server.shutdown()
            server.server_close()

   def test_remote_client_rejects_redirect_without_forwarding_bearer_token(self):
      received_authorization = []

      class SinkHandler(BaseHTTPRequestHandler):
         def do_GET(self):  # noqa: N802
            received_authorization.append(self.headers.get("Authorization"))
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

         def log_message(self, fmt, *args):
            return

      sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
      sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
      sink_thread.start()
      sink_host, sink_port = sink.server_address
      sink_url = f"http://{sink_host}:{sink_port}/capture"

      class RedirectHandler(BaseHTTPRequestHandler):
         def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", sink_url)
            self.end_headers()

         def log_message(self, fmt, *args):
            return

      redirect = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
      redirect_thread = threading.Thread(target=redirect.serve_forever, daemon=True)
      redirect_thread.start()
      redirect_host, redirect_port = redirect.server_address
      try:
         with self.assertRaisesRegex(pt.ProvisioningTransportError, "HTTP 302"):
            pt._remote_json(
               "GET",
               f"http://{redirect_host}:{redirect_port}/redirect",
               TOKEN,
            )
         self.assertEqual(received_authorization, [])
      finally:
         redirect.shutdown()
         redirect.server_close()
         sink.shutdown()
         sink.server_close()

   def test_remote_client_rejects_non_http_url_before_reading_local_file(self):
      with tempfile.TemporaryDirectory() as td:
         local_document = Path(td) / "private.json"
         local_document.write_text('{"private": true}')
         with self.assertRaisesRegex(pt.ProvisioningTransportError, "http or https"):
            pt._remote_json("GET", local_document.as_uri(), TOKEN)

   def test_remote_client_rejects_oversized_response_before_reading_body(self):
      class LargeHandler(BaseHTTPRequestHandler):
         def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(pt.MAX_RESPONSE_BYTES + 1))
            self.end_headers()

         def log_message(self, fmt, *args):
            return

      server = ThreadingHTTPServer(("127.0.0.1", 0), LargeHandler)
      thread = threading.Thread(target=server.serve_forever, daemon=True)
      thread.start()
      host, port = server.server_address
      try:
         with self.assertRaisesRegex(pt.ProvisioningTransportError, "response body too large"):
            pt._remote_json("GET", f"http://{host}:{port}/large", TOKEN)
      finally:
         server.shutdown()
         server.server_close()

   def test_coordinator_cli_defaults_to_loopback_binding(self):
      observed_hosts = []

      def command(args):
         observed_hosts.append(args.host)
         return 0

      with mock.patch.object(pt, "command_coordinator", side_effect=command):
         self.assertEqual(pt.main(["coordinator", "--deployment", "unused.json"]), 0)
      self.assertEqual(observed_hosts, ["127.0.0.1"])

   def test_token_file_rejects_group_or_world_readable_permissions(self):
      with tempfile.TemporaryDirectory() as td:
         token_path = Path(td) / "token"
         token_path.write_text(TOKEN)
         token_path.chmod(0o644)
         with self.assertRaisesRegex(ValueError, "permissions"):
            pt.load_bearer_token(token_path)

   def test_coordinator_derives_node_order_when_route_omits_redundant_field(self):
      with tempfile.TemporaryDirectory() as td:
         route = self.route()
         route.pop("node_order")
         assignments = [self.assignment("node-a", 0, Path(td) / "cache-a"), self.assignment("node-b", 1, Path(td) / "cache-b")]
         coordinator = pt.ProvisioningCoordinator(route, assignments, bearer_token=TOKEN, report_dir=Path(td) / "reports")
         self.assertEqual(coordinator.status()["pending_nodes"], ["node-a", "node-b"])

   def test_fetch_assignment_rejects_non_object_json_response(self):
      with mock.patch.object(pt, "_remote_json", return_value=[]):
         with self.assertRaisesRegex(pt.ProvisioningTransportError, "JSON object"):
            pt.fetch_assignment("http://127.0.0.1:1", DEPLOYMENT_ID, "node-a", TOKEN)

   def test_repeated_warm_cache_report_is_idempotent_for_same_assignment(self):
      with tempfile.TemporaryDirectory() as td:
         _, server, base_url, _ = self.start(Path(td))
         try:
            assignment = pt.fetch_assignment(base_url, DEPLOYMENT_ID, "node-a", TOKEN)
            report = self.report(assignment)
            first = pt.submit_report(base_url, DEPLOYMENT_ID, "node-a", report, TOKEN)
            warm = json.loads(json.dumps(report))
            warm["network_download_bytes"] = 0
            warm["cache_hit_bytes"] = warm["expected_bytes"]
            warm["verified_files"][0]["cache_hit"] = True
            warm["timestamp"] = "later"
            second = pt.submit_report(base_url, DEPLOYMENT_ID, "node-a", warm, TOKEN)
            self.assertEqual(second, first)
         finally:
            server.shutdown()
            server.server_close()

   def test_remote_peer_flow_provisions_fetched_assignment_and_posts_report(self):
      with tempfile.TemporaryDirectory() as td:
         _, server, base_url, expected = self.start(Path(td))
         seen = []

         def provisioner(assignment, *, local_files_only=False):
            seen.append((assignment, local_files_only))
            return self.report(assignment)

         try:
            report_path = Path(td) / "peer-report.json"
            result = pt.run_remote_peer(
               base_url,
               DEPLOYMENT_ID,
               "node-a",
               TOKEN,
               report_path=report_path,
               local_files_only=True,
               provisioner=provisioner,
            )
            self.assertEqual(seen, [(expected[0], True)])
            self.assertEqual(result["report"]["node_id"], "node-a")
            self.assertEqual(result["status"]["reported_nodes"], ["node-a"])
            self.assertEqual(json.loads(report_path.read_text()), result["report"])
         finally:
            server.shutdown()
            server.server_close()

   def test_existing_durable_report_stops_before_fetch_or_provision(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         _, server, base_url, _ = self.start(root)
         server.shutdown()
         server.server_close()
         report_path = root / "durable-report.json"
         report_path.write_text("{}")
         provision_calls = []

         def provisioner(assignment, *, local_files_only=False):
            provision_calls.append(assignment)
            return self.report(assignment)

         with self.assertRaisesRegex(ValueError, "resubmit"):
            pt.run_remote_peer(
               base_url,
               DEPLOYMENT_ID,
               "node-a",
               TOKEN,
               report_path=report_path,
               provisioner=provisioner,
            )
         self.assertEqual(provision_calls, [])
         self.assertEqual(report_path.read_text(), "{}")

   def test_stale_unlocked_report_lock_does_not_block_peer(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         _, server, base_url, assignments = self.start(root)
         report_path = root / "peer-report.json"
         lock_path = report_path.with_name(f".{report_path.name}.lock")
         lock_path.write_text("stale lock from terminated process")

         def provisioner(assignment, *, local_files_only=False):
            return self.report(assignment)

         try:
            result = pt.run_remote_peer(
               base_url,
               DEPLOYMENT_ID,
               "node-a",
               TOKEN,
               report_path=report_path,
               provisioner=provisioner,
            )
            self.assertEqual(result["assignment"], assignments[0])
            self.assertTrue(report_path.is_file())
         finally:
            server.shutdown()
            server.server_close()

   def test_peer_lock_symlink_is_rejected_without_modifying_target(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         report_path = root / "peer-report.json"
         lock_path = root / ".peer-report.json.lock"
         victim = root / "victim.txt"
         victim.write_text("do-not-touch")
         lock_path.symlink_to(victim)
         with self.assertRaisesRegex(ValueError, "symlink"):
            pt._claim_peer_report(report_path, force=False)
         self.assertEqual(victim.read_text(), "do-not-touch")

   def test_remote_peer_persists_report_before_submit_failure(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         _, server, base_url, assignments = self.start(root)
         report_path = root / "durable-report.json"

         def provisioner(assignment, *, local_files_only=False):
            return self.report(assignment)

         def failing_submitter(*args, **kwargs):
            self.assertTrue(report_path.is_file())
            persisted = json.loads(report_path.read_text())
            self.assertEqual(persisted["node_id"], "node-a")
            raise pt.ProvisioningTransportError("temporary outage")

         try:
            with self.assertRaisesRegex(pt.ProvisioningTransportError, "temporary outage"):
               pt.run_remote_peer(
                  base_url,
                  DEPLOYMENT_ID,
                  "node-a",
                  TOKEN,
                  report_path=report_path,
                  provisioner=provisioner,
                  submitter=failing_submitter,
               )
            self.assertEqual(
               json.loads(report_path.read_text())["assignment_id"],
               assignments[0]["assignment_id"],
            )
         finally:
            server.shutdown()
            server.server_close()

   def test_submit_persisted_report_retries_without_provisioning(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         _, server, base_url, assignments = self.start(root)
         report_path = root / "durable-report.json"
         report_path.write_text(json.dumps(self.report(assignments[0])))
         try:
            status = pt.submit_persisted_report(
               base_url,
               DEPLOYMENT_ID,
               "node-a",
               TOKEN,
               report_path=report_path,
            )
            self.assertEqual(status["reported_nodes"], ["node-a"])
            self.assertEqual(status["pending_nodes"], ["node-b"])
         finally:
            server.shutdown()
            server.server_close()

   def test_peer_cli_passes_required_report_path_before_submission(self):
      result = {
         "assignment": {"assignment_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
         "report": {
            "ready_for_load": True,
            "route_ready": False,
            "network_download_bytes": 100,
            "cache_hit_bytes": 0,
         },
         "report_path": "/tmp/node-a-report.json",
         "status": {"pending_nodes": ["node-b"]},
      }
      with (
         mock.patch.object(pt, "load_bearer_token", return_value=TOKEN),
         mock.patch.object(pt, "run_remote_peer", return_value=result) as run,
         contextlib.redirect_stdout(io.StringIO()),
      ):
         rc = pt.main([
            "peer",
            "--coordinator-url", "http://coordinator.test",
            "--deployment-id", DEPLOYMENT_ID,
            "--node-id", "node-a",
            "--report", "/tmp/node-a-report.json",
         ])
      self.assertEqual(rc, 0)
      self.assertEqual(run.call_args.kwargs["report_path"], "/tmp/node-a-report.json")

   def test_resubmit_cli_posts_persisted_report_without_provisioning(self):
      status = {"pending_nodes": ["node-b"], "audit": None}
      with (
         mock.patch.object(pt, "load_bearer_token", return_value=TOKEN),
         mock.patch.object(pt, "submit_persisted_report", return_value=status) as submit,
         contextlib.redirect_stdout(io.StringIO()),
      ):
         rc = pt.main([
            "resubmit",
            "--coordinator-url", "http://coordinator.test",
            "--deployment-id", DEPLOYMENT_ID,
            "--node-id", "node-a",
            "--report", "/tmp/node-a-report.json",
         ])
      self.assertEqual(rc, 0)
      self.assertEqual(submit.call_args.kwargs["report_path"], "/tmp/node-a-report.json")

   def test_report_schema_rejects_numeric_type_confusion(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         state, server, _, assignments = self.start(root)
         server.shutdown()
         server.server_close()
         mutations = {
            "deployment_epoch": lambda report: report.__setitem__("deployment_epoch", True),
            "verified_tensor_count": lambda report: report.__setitem__("verified_tensor_count", 1.0),
            "expected_bytes": lambda report: report.__setitem__("expected_bytes", 100.0),
            "verified_file_size": lambda report: report["verified_files"][0].__setitem__("size_bytes", 100.0),
         }
         for name, mutate in mutations.items():
            with self.subTest(name=name):
               report = self.report(assignments[0])
               mutate(report)
               with self.assertRaisesRegex(ValueError, "invalid report field type"):
                  state.submit_report(DEPLOYMENT_ID, "node-a", report)

   def test_status_response_rejects_wrong_identity_and_state_invariants(self):
      valid_pending = {
         "protocol": pt.STATUS_PROTOCOL,
         "deployment_id": DEPLOYMENT_ID,
         "deployment_epoch": 1,
         "state": "pending_reports",
         "expected_nodes": ["node-a", "node-b"],
         "reported_nodes": ["node-a"],
         "pending_nodes": ["node-b"],
         "all_reports_received": False,
         "audit": None,
      }
      mutations = {
         "protocol": lambda value: value.__setitem__("protocol", "wrong"),
         "deployment": lambda value: value.__setitem__("deployment_id", "wrong"),
         "contradictory_pending": lambda value: value.__setitem__("all_reports_received", True),
         "partition": lambda value: value.__setitem__("pending_nodes", []),
      }
      for name, mutate in mutations.items():
         with self.subTest(name=name):
            response = json.loads(json.dumps(valid_pending))
            mutate(response)
            with mock.patch.object(pt, "_remote_json", return_value=response):
               with self.assertRaises(pt.ProvisioningTransportError):
                  pt.fetch_status("http://coordinator.test", DEPLOYMENT_ID, TOKEN)

   def test_status_cli_maps_authoritative_states_to_exit_codes(self):
      statuses = {
         "pending_reports": ({
            "state": "pending_reports",
            "all_reports_received": False,
            "pending_nodes": ["node-b"],
            "audit": None,
         }, 3),
         "failed": ({
            "state": "failed",
            "all_reports_received": True,
            "pending_nodes": [],
            "audit": {"all_assignments_verified": False},
         }, 2),
         "verified": ({
            "state": "verified",
            "all_reports_received": True,
            "pending_nodes": [],
            "audit": {"all_assignments_verified": True},
         }, 0),
      }
      for state, (status, expected_exit) in statuses.items():
         with self.subTest(state=state):
            with (
               mock.patch.object(pt, "load_bearer_token", return_value=TOKEN),
               mock.patch.object(pt, "fetch_status", return_value=status),
               contextlib.redirect_stdout(io.StringIO()),
            ):
               rc = pt.main([
                  "status",
                  "--coordinator-url", "http://coordinator.test",
                  "--deployment-id", DEPLOYMENT_ID,
               ])
            self.assertEqual(rc, expected_exit)

   def test_status_cli_returns_pending_exit_code_until_audit_exists(self):
      pending = {
         "state": "pending_reports",
         "all_reports_received": False,
         "pending_nodes": ["node-b"],
         "audit": None,
      }
      with (
         mock.patch.object(pt, "load_bearer_token", return_value=TOKEN),
         mock.patch.object(pt, "fetch_status", return_value=pending),
         contextlib.redirect_stdout(io.StringIO()),
      ):
         rc = pt.main([
            "status",
            "--coordinator-url", "http://coordinator.test",
            "--deployment-id", DEPLOYMENT_ID,
         ])
      self.assertEqual(rc, 3)

   def test_identical_retry_repairs_missing_audit_after_storage_failure(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         assignments = [
            self.assignment("node-a", 0, root / "cache-a"),
            self.assignment("node-b", 1, root / "cache-b"),
         ]
         state = pt.ProvisioningCoordinator(
            self.route(), assignments, bearer_token=TOKEN, report_dir=root / "reports"
         )
         state.submit_report(DEPLOYMENT_ID, "node-a", self.report(assignments[0]))
         real_write = pt._atomic_write_json
         failed = False

         def fail_first_audit(path, document):
            nonlocal failed
            if path.name == "provisioning-audit.json" and not failed:
               failed = True
               raise OSError("simulated audit storage failure")
            return real_write(path, document)

         with mock.patch.object(pt, "_atomic_write_json", side_effect=fail_first_audit):
            with self.assertRaisesRegex(OSError, "simulated audit storage failure"):
               state.submit_report(DEPLOYMENT_ID, "node-b", self.report(assignments[1]))
         audit_path = state.report_dir / "provisioning-audit.json"
         self.assertFalse(audit_path.exists())
         repaired = state.submit_report(
            DEPLOYMENT_ID, "node-b", self.report(assignments[1])
         )
         self.assertTrue(audit_path.is_file())
         self.assertEqual(repaired["state"], "verified")

   def test_coordinator_cli_reports_actual_namespaced_storage(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         assignments = [
            self.assignment("node-a", 0, root / "cache-a"),
            self.assignment("node-b", 1, root / "cache-b"),
         ]

         class OneShotServer:
            server_address = ("127.0.0.1", 8790)

            def serve_forever(self):
               raise KeyboardInterrupt

            def server_close(self):
               pass

         server = OneShotServer()
         args = type("Args", (), {
            "deployment": str(root / "deployment.json"),
            "token_file": None,
            "report_dir": str(root / "reports"),
            "host": "127.0.0.1",
            "port": 8790,
            "quiet": True,
         })()
         output = io.StringIO()
         with (
            mock.patch.object(
               pt,
               "load_deployment_bundle",
               return_value=(
                  self.route(),
                  assignments,
                  {"deployment_id": DEPLOYMENT_ID},
               ),
            ),
            mock.patch.object(pt, "load_bearer_token", return_value=TOKEN),
            mock.patch.object(pt, "ThreadingHTTPServer", return_value=server),
            contextlib.redirect_stdout(output),
         ):
            self.assertEqual(pt.command_coordinator(args), 0)
         advertised = json.loads(output.getvalue())
         self.assertEqual(
            advertised["report_dir"],
            str(root.resolve() / "reports" / DEPLOYMENT_ID / "1"),
         )

   def test_report_storage_is_namespaced_by_deployment_and_epoch(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td) / "shared-reports"
         assignments = [
            self.assignment("node-a", 0, root / "cache-a"),
            self.assignment("node-b", 1, root / "cache-b"),
         ]
         state = pt.ProvisioningCoordinator(
            self.route(), assignments, bearer_token=TOKEN, report_dir=root
         )
         self.assertEqual(
            state.report_dir,
            root.resolve() / DEPLOYMENT_ID / "1",
         )

   def test_coordinator_recovers_valid_reports_after_restart(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         state, server, base_url, assignments = self.start(root)
         try:
            pt.submit_report(base_url, DEPLOYMENT_ID, "node-a", self.report(assignments[0]), TOKEN)
         finally:
            server.shutdown()
            server.server_close()

         recovered = pt.ProvisioningCoordinator(
            self.route(),
            assignments,
            bearer_token=TOKEN,
            report_dir=root / "reports",
         )
         self.assertEqual(recovered.status()["reported_nodes"], ["node-a"])
         final = recovered.submit_report(DEPLOYMENT_ID, "node-b", self.report(assignments[1]))
         self.assertTrue(final["audit"]["all_assignments_verified"])

   def test_load_bundle_rejects_assignment_set_that_does_not_match_route(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         route_path = root / "route.json"
         assignment_path = root / "assignment-a.json"
         manifest_path = root / "manifest.json"
         deployment_path = root / "deployment.json"
         route_path.write_text(json.dumps(self.route()))
         assignment_path.write_text(json.dumps(self.assignment("node-a", 0, root / "cache-a")))
         manifest_path.write_text("{}")
         deployment_path.write_text(json.dumps({
            "protocol": "mycelium.weight_provisioning_demo.v1",
            "deployment_id": DEPLOYMENT_ID,
            "deployment_epoch": 1,
            "manifest": str(manifest_path),
            "route": str(route_path),
            "assignments": {"node-a": str(assignment_path)},
         }))

         with self.assertRaisesRegex(ValueError, "assignment nodes do not match route"):
            pt.load_deployment_bundle(deployment_path)

   def test_load_bundle_rejects_tampered_manifest_even_when_route_and_assignments_agree(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         route_path = root / "route.json"
         manifest_path = root / "manifest.json"
         deployment_path = root / "deployment.json"
         assignments = [
            self.assignment("node-a", 0, root / "cache-a"),
            self.assignment("node-b", 1, root / "cache-b"),
         ]
         route_path.write_text(json.dumps(self.route()))
         manifest_path.write_text(json.dumps({"protocol": "mycelium.model_manifest.v1"}))
         assignment_refs = {}
         for assignment in assignments:
            assignment_path = root / f"assignment-{assignment['node_id']}.json"
            assignment_path.write_text(json.dumps(assignment))
            assignment_refs[assignment["node_id"]] = str(assignment_path)
         deployment_path.write_text(json.dumps({
            "protocol": "mycelium.weight_provisioning_demo.v1",
            "deployment_id": DEPLOYMENT_ID,
            "deployment_epoch": 1,
            "manifest": str(manifest_path),
            "route": str(route_path),
            "assignments": assignment_refs,
         }))

         with self.assertRaisesRegex(ValueError, "manifest"):
            pt.load_deployment_bundle(deployment_path)

   def test_load_bundle_rejects_assignment_content_not_derived_from_manifest(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         manifest = mm.compile_model_manifest(
            model_id="org/model",
            requested_revision="main",
            resolved_commit="a" * 40,
            config={"model_type": "gpt2", "n_layer": 2},
            checkpoint_index={
               "weight_map": {
                  "h.0.weight": "a.safetensors",
                  "h.1.weight": "b.safetensors",
               },
            },
            file_metadata={
               "a.safetensors": {"size_bytes": 1, "sha256": "1" * 64},
               "b.safetensors": {"size_bytes": 2, "sha256": "2" * 64},
            },
         )
         route = self.route()
         route["model"]["resolved_commit"] = manifest["resolved_commit"]
         route["model"]["manifest_digest"] = mm.manifest_digest_ref(manifest)
         assignments = la.compile_layer_assignments(
            route_plan=route,
            manifest=manifest,
            deployment_id=DEPLOYMENT_ID,
            deployment_epoch=1,
            cache_roots={"node-a": str(root / "cache-a"), "node-b": str(root / "cache-b")},
            runtime_by_node={
               node: {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"}
               for node in ("node-a", "node-b")
            },
         )
         assignments[0]["files"][0]["size_bytes"] += 1
         assignments[0]["assignment_id"] = la.assignment_id_for(assignments[0])
         manifest_path = root / "manifest.json"
         route_path = root / "route.json"
         manifest_path.write_text(json.dumps(manifest))
         route_path.write_text(json.dumps(route))
         references = {}
         for assignment in assignments:
            path = root / f"assignment-{assignment['node_id']}.json"
            path.write_text(json.dumps(assignment))
            references[assignment["node_id"]] = path.name
         deployment_path = root / "deployment.json"
         deployment_path.write_text(json.dumps({
            "protocol": "mycelium.weight_provisioning_demo.v1",
            "deployment_id": DEPLOYMENT_ID,
            "deployment_epoch": 1,
            "manifest": manifest_path.name,
            "route": route_path.name,
            "assignments": references,
         }))

         with self.assertRaisesRegex(ValueError, "manifest-derived assignment"):
            pt.load_deployment_bundle(deployment_path)

   def test_coordinator_rejects_noncanonical_deployment_identity_before_storage(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         assignments = [
            self.assignment("node-a", 0, root / "cache-a"),
            self.assignment("node-b", 1, root / "cache-b"),
         ]
         for assignment in assignments:
            assignment["deployment_id"] = "../escaped"

         with self.assertRaisesRegex(ValueError, "deployment_id must be a canonical UUID"):
            pt.ProvisioningCoordinator(
               self.route(),
               assignments,
               bearer_token=TOKEN,
               report_dir=root / "reports",
            )
         self.assertFalse((root / "escaped").exists())

   def test_coordinator_rejects_route_manifest_digest_mismatch(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         assignments = [
            self.assignment("node-a", 0, root / "cache-a"),
            self.assignment("node-b", 1, root / "cache-b"),
         ]
         route = self.route()
         route["model"]["manifest_digest"] = "sha256:" + "c" * 64

         with self.assertRaisesRegex(ValueError, "manifest_digest does not match assignments"):
            pt.ProvisioningCoordinator(
               route,
               assignments,
               bearer_token=TOKEN,
               report_dir=root / "reports",
            )

   def test_coordinator_rejects_route_resolved_commit_mismatch(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         assignments = [
            self.assignment("node-a", 0, root / "cache-a"),
            self.assignment("node-b", 1, root / "cache-b"),
         ]
         route = self.route()
         route["model"]["resolved_commit"] = "c" * 40

         with self.assertRaisesRegex(ValueError, "resolved_commit does not match assignments"):
            pt.ProvisioningCoordinator(
               route,
               assignments,
               bearer_token=TOKEN,
               report_dir=root / "reports",
            )


if __name__ == "__main__":
   unittest.main()
