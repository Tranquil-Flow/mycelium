#!/usr/bin/env python3
import hashlib
import json
import shutil
import struct
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import layer_assignment as la
import weight_provisioning as wp


def write_safetensors(path, tensor_names):
   header = {}
   offset = 0
   payload = bytearray()
   for index, name in enumerate(tensor_names):
      data = bytes([index + 1]) * 4
      header[name] = {"dtype": "F32", "shape": [1], "data_offsets": [offset, offset + len(data)]}
      payload.extend(data)
      offset += len(data)
   encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
   padding = (8 - len(encoded) % 8) % 8
   encoded += b" " * padding
   path.write_bytes(struct.pack("<Q", len(encoded)) + encoded + payload)


def digest(path):
   return hashlib.sha256(path.read_bytes()).hexdigest()


class WeightProvisioningTests(unittest.TestCase):
   def make_assignment(self, source, cache):
      shard_a = source / "shard-a.safetensors"
      shard_b = source / "shard-b.safetensors"
      write_safetensors(shard_a, ["h.0.attn.weight", "h.1.attn.weight"])
      write_safetensors(shard_b, ["h.1.mlp.weight"])
      assignment = {
         "protocol": "mycelium.layer_assignment.v1",
         "deployment_id": "12345678-1234-5678-1234-567812345678",
         "deployment_epoch": 1,
         "assignment_id": "87654321-4321-8765-4321-876543218765",
         "node_id": "node-a",
         "manifest_digest": "sha256:" + "a" * 64,
         "model_id": "org/model",
         "resolved_commit": "b" * 40,
         "range": {"start_layer": 0, "end_layer_exclusive": 2, "layer_count": 2},
         "components": ["decoder"],
         "expected_tensor_prefixes": ["h.0.", "h.1."],
         "expected_tensor_keys": ["h.0.attn.weight", "h.1.attn.weight", "h.1.mlp.weight"],
         "files": [
            {
               "path": shard_a.name,
               "size_bytes": shard_a.stat().st_size,
               "content_digest": "sha256:" + digest(shard_a),
            },
            {
               "path": shard_b.name,
               "size_bytes": shard_b.stat().st_size,
               "content_digest": "sha256:" + digest(shard_b),
            },
         ],
         "artifact_cache_root": str(cache.resolve()),
         "runtime": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
         "route_ready": False,
      }
      assignment["assignment_id"] = la.assignment_id_for(assignment)
      return assignment

   def test_downloads_exact_allowlist_at_pinned_commit_and_verifies_tensors(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         source = root / "source"
         cache = root / "cache"
         source.mkdir()
         assignment = self.make_assignment(source, cache)
         calls = []

         def fetch(model_id, revision, filename, cache_root, local_files_only=False):
            calls.append((model_id, revision, filename, Path(cache_root), local_files_only))
            target = Path(cache_root) / "snapshot" / filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / filename, target)
            return target, False

         report = wp.provision_assignment(assignment, fetch_file=fetch)

         self.assertEqual([call[2] for call in calls], ["shard-a.safetensors", "shard-b.safetensors"])
         self.assertTrue(all(call[1] == "b" * 40 for call in calls))
         self.assertTrue(all(call[3] == cache.resolve() for call in calls))
         self.assertTrue(report["ready_for_load"])
         self.assertFalse(report["route_ready"])
         self.assertEqual(report["verified_tensor_prefixes"], ["h.0.", "h.1."])
         self.assertEqual(len(report["verified_files"]), 2)

   def test_report_preserves_assigned_cache_root_while_using_resolved_local_path(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         source = root / "source"
         real_cache = root / "real-cache"
         cache_link = root / "peer-cache"
         source.mkdir()
         real_cache.mkdir()
         cache_link.symlink_to(real_cache, target_is_directory=True)
         assignment = self.make_assignment(source, cache_link)
         assignment["artifact_cache_root"] = str(cache_link)
         assignment["assignment_id"] = la.assignment_id_for(assignment)

         def fetch(model_id, revision, filename, cache_root, local_files_only=False):
            return source / filename, False

         report = wp.provision_assignment(assignment, fetch_file=fetch)
         self.assertEqual(report["artifact_cache_root"], str(cache_link))
         self.assertEqual(report["resolved_artifact_cache_root"], str(real_cache.resolve()))

   def test_checksum_mismatch_fails_closed(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         source = root / "source"
         cache = root / "cache"
         source.mkdir()
         assignment = self.make_assignment(source, cache)
         assignment["files"][0]["content_digest"] = "sha256:" + "0" * 64
         assignment["assignment_id"] = la.assignment_id_for(assignment)

         def fetch(model_id, revision, filename, cache_root, local_files_only=False):
            return source / filename, False

         with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            wp.provision_assignment(assignment, fetch_file=fetch)

   def test_missing_assigned_tensor_fails_closed(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         source = root / "source"
         cache = root / "cache"
         source.mkdir()
         assignment = self.make_assignment(source, cache)
         assignment["expected_tensor_keys"].append("h.1.missing.weight")
         assignment["assignment_id"] = la.assignment_id_for(assignment)

         def fetch(model_id, revision, filename, cache_root, local_files_only=False):
            return source / filename, False

         with self.assertRaisesRegex(ValueError, "missing assigned tensors"):
            wp.provision_assignment(assignment, fetch_file=fetch)

   def test_parent_traversal_filename_fails_before_fetch(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         source = root / "source"
         cache = root / "cache"
         source.mkdir()
         assignment = self.make_assignment(source, cache)
         assignment["files"][0]["path"] = "../escape.safetensors"
         assignment["assignment_id"] = la.assignment_id_for(assignment)
         called = False

         def fetch(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("fetch must not run")

         with self.assertRaisesRegex(ValueError, "unsafe artifact path"):
            wp.provision_assignment(assignment, fetch_file=fetch)
         self.assertFalse(called)

   def test_cache_hit_accounting_reports_zero_network_bytes(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         source = root / "source"
         cache = root / "cache"
         source.mkdir()
         assignment = self.make_assignment(source, cache)

         def fetch(model_id, revision, filename, cache_root, local_files_only=False):
            return source / filename, True

         report = wp.provision_assignment(
            assignment,
            fetch_file=fetch,
            local_files_only=True,
         )
         self.assertEqual(report["network_download_bytes"], 0)
         self.assertEqual(report["cache_hit_bytes"], report["expected_bytes"])
         self.assertTrue(all(item["cache_hit"] for item in report["verified_files"]))

   def test_local_only_cache_verification_does_not_require_duplicate_free_space(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         source = root / "source"
         cache = root / "cache"
         source.mkdir()
         assignment = self.make_assignment(source, cache)

         def fetch(model_id, revision, filename, cache_root, local_files_only=False):
            self.assertTrue(local_files_only)
            return source / filename, True

         no_free_space = type("DiskUsage", (), {"free": 0})()
         with mock.patch.object(wp.shutil, "disk_usage", return_value=no_free_space):
            report = wp.provision_assignment(
               assignment,
               fetch_file=fetch,
               local_files_only=True,
            )
         self.assertEqual(report["network_download_bytes"], 0)
         self.assertEqual(report["cache_hit_bytes"], report["expected_bytes"])

   def test_control_character_filename_fails_before_fetch(self):
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         source = root / "source"
         cache = root / "cache"
         source.mkdir()
         assignment = self.make_assignment(source, cache)
         assignment["files"][0]["path"] = "bad\nname.safetensors"
         assignment["assignment_id"] = la.assignment_id_for(assignment)
         called = False

         def fetch(*args, **kwargs):
            nonlocal called
            called = True
            raise AssertionError("fetch must not run")

         with self.assertRaisesRegex(ValueError, "unsafe artifact path"):
            wp.provision_assignment(assignment, fetch_file=fetch)
         self.assertFalse(called)

   def test_coordinator_audit_rejects_wrong_report_protocol(self):
      route = {
         "ok": True,
         "protocol": "mycelium.route_plan.v2",
         "model": {"model_id": "org/model", "num_layers": 1},
         "route": [{
            "node_id": "node-a",
            "range": {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1},
         }],
      }
      assignment = {
         "protocol": "mycelium.layer_assignment.v1",
         "deployment_id": "12345678-1234-5678-1234-567812345678",
         "deployment_epoch": 1,
         "assignment_id": "87654321-4321-8765-4321-876543218765",
         "node_id": "node-a",
         "manifest_digest": "sha256:" + "a" * 64,
         "resolved_commit": "b" * 40,
         "range": route["route"][0]["range"],
      }
      report = {
         **assignment,
         "protocol": "wrong.report.v1",
         "ready_for_load": True,
         "route_ready": False,
         "verified_files": [{"path": "x.safetensors"}],
         "verified_tensor_count": 1,
      }
      audit = wp.audit_provisioning(route, [assignment], [report])
      self.assertFalse(audit["all_assignments_verified"])
      self.assertIn("wrong report protocol", " ".join(audit["errors"]))

   def test_coordinator_audit_rejects_duplicate_node_evidence(self):
      route = {
         "ok": True,
         "protocol": "mycelium.route_plan.v2",
         "model": {"model_id": "org/model", "num_layers": 1},
         "route": [{
            "node_id": "node-a",
            "range": {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1},
         }],
      }
      assignment = {
         "protocol": "mycelium.layer_assignment.v1",
         "deployment_id": "12345678-1234-5678-1234-567812345678",
         "deployment_epoch": 1,
         "assignment_id": "87654321-4321-8765-4321-876543218765",
         "node_id": "node-a",
         "manifest_digest": "sha256:" + "a" * 64,
         "resolved_commit": "b" * 40,
         "range": route["route"][0]["range"],
      }
      report = {
         **assignment,
         "protocol": "mycelium.artifact_verification_report.v1",
         "ready_for_load": True,
         "route_ready": False,
         "verified_files": [{"path": "x.safetensors"}],
         "verified_tensor_count": 1,
      }
      audit = wp.audit_provisioning(route, [assignment, dict(assignment)], [report])
      self.assertFalse(audit["all_assignments_verified"])
      self.assertIn("duplicate assignment", " ".join(audit["errors"]))

   def test_coordinator_audit_accepts_verified_reports_without_activating_route(self):
      route = {
         "ok": True,
         "protocol": "mycelium.route_plan.v2",
         "model": {"model_id": "org/model", "num_layers": 2},
         "route": [{
            "node_id": "node-a",
            "range": {"start_layer": 0, "end_layer_exclusive": 2, "layer_count": 2},
         }],
      }
      assignment = {
         "protocol": "mycelium.layer_assignment.v1",
         "deployment_id": "12345678-1234-5678-1234-567812345678",
         "deployment_epoch": 1,
         "assignment_id": "87654321-4321-8765-4321-876543218765",
         "node_id": "node-a",
         "manifest_digest": "sha256:" + "a" * 64,
         "model_id": "org/model",
         "resolved_commit": "b" * 40,
         "range": route["route"][0]["range"],
         "components": ["decoder"],
         "artifact_cache_root": "/tmp/node-a",
         "expected_tensor_prefixes": ["h.0.", "h.1."],
         "expected_tensor_keys": ["h.0.weight", "h.1.weight"],
         "files": [{
            "path": "x.safetensors",
            "size_bytes": 123,
            "content_digest": "sha256:" + "c" * 64,
         }],
         "runtime": {"backend": "artifact_verifier", "dtype": "source", "quantization": "none"},
         "route_ready": False,
      }
      assignment["assignment_id"] = la.assignment_id_for(assignment)
      report = {
         **assignment,
         "protocol": "mycelium.artifact_verification_report.v1",
         "ready_for_load": True,
         "route_ready": False,
         "verified_files": [{
            "path": "x.safetensors",
            "size_bytes": 123,
            "content_digest": "sha256:" + "c" * 64,
         }],
         "verified_tensor_prefixes": assignment["expected_tensor_prefixes"],
         "verified_tensor_count": 2,
         "expected_bytes": 123,
         "network_download_bytes": 123,
         "cache_hit_bytes": 0,
      }
      audit = wp.audit_provisioning(route, [assignment], [report])
      self.assertTrue(audit["all_assignments_verified"])
      self.assertTrue(audit["ready_for_runtime_load"])
      self.assertFalse(audit["route_ready"])

   def test_coordinator_audit_rejects_invalid_assignment_identity(self):
      route = {
         "ok": True,
         "protocol": "mycelium.route_plan.v2",
         "model": {"model_id": "org/model", "num_layers": 2},
         "route": [{"node_id": "node-a", "range": {"start_layer": 0, "end_layer_exclusive": 2, "layer_count": 2}}],
      }
      with tempfile.TemporaryDirectory() as td:
         root = Path(td)
         source = root / "source"
         source.mkdir()
         assignment = self.make_assignment(source, root / "cache")
      assignment["deployment_id"] = "not-a-uuid"
      report = {
         **assignment,
         "protocol": "mycelium.artifact_verification_report.v1",
         "verified_files": assignment["files"],
         "verified_tensor_prefixes": assignment["expected_tensor_prefixes"],
         "verified_tensor_count": len(assignment["expected_tensor_keys"]),
         "expected_bytes": sum(item["size_bytes"] for item in assignment["files"]),
         "network_download_bytes": sum(item["size_bytes"] for item in assignment["files"]),
         "cache_hit_bytes": 0,
         "ready_for_load": True,
         "route_ready": False,
      }
      audit = wp.audit_provisioning(route, [assignment], [report])
      self.assertFalse(audit["ready_for_runtime_load"])
      self.assertIn("assignment", " ".join(audit["errors"]))

   def test_coordinator_audit_rejects_forged_file_digest_and_byte_evidence(self):
      route = {
         "ok": True,
         "protocol": "mycelium.route_plan.v2",
         "model": {"model_id": "org/model", "num_layers": 1},
         "route": [{
            "node_id": "node-a",
            "range": {"start_layer": 0, "end_layer_exclusive": 1, "layer_count": 1},
         }],
      }
      assignment = {
         "protocol": "mycelium.layer_assignment.v1",
         "deployment_id": "12345678-1234-5678-1234-567812345678",
         "deployment_epoch": 1,
         "assignment_id": "87654321-4321-8765-4321-876543218765",
         "node_id": "node-a",
         "manifest_digest": "sha256:" + "a" * 64,
         "model_id": "org/model",
         "resolved_commit": "b" * 40,
         "range": route["route"][0]["range"],
         "components": ["decoder"],
         "artifact_cache_root": "/tmp/node-a",
         "expected_tensor_prefixes": ["h.0."],
         "expected_tensor_keys": ["h.0.weight"],
         "files": [{
            "path": "x.safetensors",
            "size_bytes": 123,
            "content_digest": "sha256:" + "c" * 64,
         }],
      }
      forged = {
         **assignment,
         "protocol": "mycelium.artifact_verification_report.v1",
         "ready_for_load": True,
         "route_ready": False,
         "verified_files": [{
            "path": "x.safetensors",
            "size_bytes": 123,
            "content_digest": "sha256:" + "d" * 64,
         }],
         "verified_tensor_prefixes": assignment["expected_tensor_prefixes"],
         "verified_tensor_count": 1,
         "expected_bytes": 123,
         "network_download_bytes": 1,
         "cache_hit_bytes": 0,
      }
      audit = wp.audit_provisioning(route, [assignment], [forged])
      self.assertFalse(audit["all_assignments_verified"])
      self.assertIn("verified digest mismatch", " ".join(audit["errors"]))
      self.assertIn("download byte accounting", " ".join(audit["errors"]))


if __name__ == "__main__":
   unittest.main()
