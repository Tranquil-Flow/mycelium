#!/usr/bin/env python3
"""
Mycelium profile broadcast / peer registry.

Tier-1 goals:
- expose local node profile over HTTP
- accept profile announcements from trusted peers/seeds
- emit tiny UDP LAN discovery hellos containing only a profile pointer
- keep full capability profile exchange over HTTP JSON

Stdlib only. No crypto/trust model yet; this is for high-trust bootstrap.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROFILE_PROTOCOL = "mycelium.node_profile.v1"
HELLO_PROTOCOL = "mycelium.hello.v1"
DEFAULT_PORT = 8788
DEFAULT_UDP_PORT = 8789
DEFAULT_MULTICAST_GROUP = "239.255.42.99"


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _profile_hash(profile: dict[str, Any]) -> str:
    tmp = dict(profile)
    tmp.pop("profile_hash", None)
    encoded = _json_dumps(tmp).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _capabilities(raw: dict[str, Any]) -> dict[str, Any]:
    ram = raw.get("ram") or {}
    storage = raw.get("storage") or {}
    power = raw.get("power") or {}
    network = raw.get("network") or {}
    gpus = raw.get("gpus") or []
    primary_gpu = gpus[0] if gpus else {}
    return {
        "device_class": raw.get("device_class"),
        "arch": raw.get("arch"),
        "platform": raw.get("platform"),
        "ram_total_gb": ram.get("total_gb"),
        "ram_available_gb": ram.get("available_gb"),
        "ram_bandwidth_gbps": ram.get("bandwidth_gbps"),
        "unified_memory": ram.get("unified_with_gpu"),
        "gpu_count": len(gpus),
        "primary_gpu_name": primary_gpu.get("name"),
        "primary_gpu_backend": primary_gpu.get("backend"),
        "vram_total_gb": primary_gpu.get("vram_total_gb"),
        "vram_available_gb": primary_gpu.get("vram_available_gb"),
        "vram_bandwidth_gbps": primary_gpu.get("vram_bandwidth_gbps"),
        "storage_available_gb": storage.get("available_gb"),
        "storage_type": storage.get("type"),
        "on_ac_power": power.get("on_ac_power"),
        "battery_pct": power.get("battery_pct"),
        "download_mbps": network.get("download_mbps"),
        "upload_mbps": network.get("upload_mbps"),
        "lan_ip": network.get("lan_ip"),
        "backends": raw.get("backends") or [],
        "supported_precision": raw.get("supported_precision") or [],
    }


def normalize_profile(raw: dict[str, Any], advertised_host: str, port: int) -> dict[str, Any]:
    """Add router-facing protocol fields and compact capability summary."""
    profile = dict(raw)
    node_id = profile.get("node_id") or profile.get("hostname") or profile.get("serial") or socket.gethostname()
    profile["protocol"] = PROFILE_PROTOCOL
    profile["node_id"] = str(node_id)
    profile["profile_url"] = f"http://{advertised_host}:{int(port)}/profile"
    profile["capabilities"] = _capabilities(profile)
    profile["profile_hash"] = _profile_hash(profile)
    return profile


class PeerRegistry:
    def __init__(self, ttl_seconds: float = 120.0):
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.RLock()
        self._peers: dict[str, dict[str, Any]] = {}

    def upsert(self, profile: dict[str, Any], source: str = "unknown") -> dict[str, Any]:
        if profile.get("protocol") != PROFILE_PROTOCOL:
            raise ValueError("invalid profile protocol")
        node_id = profile.get("node_id")
        if not node_id:
            raise ValueError("missing node_id")
        now = time.time()
        record = {
            "node_id": node_id,
            "source": source,
            "seen_at": now,
            "expires_at": now + self.ttl_seconds,
            "profile_hash": profile.get("profile_hash"),
            "profile": profile,
        }
        with self._lock:
            self._peers[str(node_id)] = record
        return record

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = time.time()
        with self._lock:
            stale = [k for k, v in self._peers.items() if v.get("expires_at", 0) < now]
            for k in stale:
                self._peers.pop(k, None)
            return json.loads(json.dumps(self._peers))


def make_hello(node_id: str, profile_url: str, profile_hash: str, ts: float | None = None) -> bytes:
    hello = {
        "protocol": HELLO_PROTOCOL,
        "node_id": node_id,
        "profile_url": profile_url,
        "profile_hash": profile_hash,
        "ts": ts if ts is not None else time.time(),
    }
    return (_json_dumps(hello) + "\n").encode("utf-8")


def parse_hello(data: bytes | str) -> dict[str, Any]:
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    hello = json.loads(data)
    if hello.get("protocol") != HELLO_PROTOCOL:
        raise ValueError("invalid hello protocol")
    for field in ("node_id", "profile_url", "profile_hash"):
        if not hello.get(field):
            raise ValueError(f"missing hello field: {field}")
    return hello


def fetch_profile(profile_url: str, timeout: float = 5.0) -> dict[str, Any]:
    with urllib.request.urlopen(profile_url, timeout=timeout) as resp:
        data = json.loads(resp.read())
    if data.get("protocol") != PROFILE_PROTOCOL:
        raise ValueError("fetched invalid profile")
    return data


def announce_to_seed(profile: dict[str, Any], seed_url: str, timeout: float = 5.0) -> dict[str, Any]:
    url = seed_url.rstrip("/") + "/announce"
    payload = json.dumps({"profile": profile}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "MyceliumNode/0.1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


class _Handler(BaseHTTPRequestHandler):
    server_version = "MyceliumBroadcast/0.1"

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, indent=2, sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2_000_000:
            raise ValueError("request too large")
        body = self.rfile.read(length)
        return json.loads(body.decode("utf-8")) if body else None

    @property
    def app(self) -> dict[str, Any]:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.app.get("quiet"):
            return
        super().log_message(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json({"ok": True, "protocol": PROFILE_PROTOCOL, "node_id": self.app["profile"]["node_id"]})
        elif self.path == "/profile":
            self._send_json(self.app["profile"])
        elif self.path == "/peers":
            self._send_json({"ok": True, "peers": self.app["registry"].snapshot()})
        elif self.path == "/nodes":
            nodes = {}
            own = self.app["profile"]
            nodes[own["node_id"]] = {
                "node_id": own["node_id"],
                "role": "self",
                "source": "self",
                "seen_at": time.time(),
                "expires_at": None,
                "profile_hash": own.get("profile_hash"),
                "profile": own,
            }
            for node_id, record in self.app["registry"].snapshot().items():
                item = dict(record)
                item["role"] = "peer"
                nodes[node_id] = item
            self._send_json({"ok": True, "nodes": nodes})
        else:
            self._send_json({"ok": False, "error": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/announce":
            self._send_json({"ok": False, "error": "not_found"}, status=404)
            return
        try:
            payload = self._read_json()
            profile = payload.get("profile") if isinstance(payload, dict) and "profile" in payload else payload
            if not isinstance(profile, dict):
                raise ValueError("missing profile object")
            record = self.app["registry"].upsert(profile, source=f"http:{self.client_address[0]}")
            self._send_json({"ok": True, "node_id": record["node_id"], "profile_hash": record["profile_hash"]})
        except Exception as exc:  # fail closed; high-trust but still structured
            self._send_json({"ok": False, "error": str(exc)}, status=400)


def start_http_server(
    host: str,
    port: int,
    profile: dict[str, Any],
    registry: PeerRegistry,
    quiet: bool = True,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, int(port)), _Handler)
    server.app = {"profile": profile, "registry": registry, "quiet": quiet}  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, name="mycelium-http", daemon=True)
    thread.start()
    return server


def udp_announce_once(
    profile: dict[str, Any],
    udp_port: int = DEFAULT_UDP_PORT,
    multicast_group: str = DEFAULT_MULTICAST_GROUP,
) -> None:
    hello = make_hello(profile["node_id"], profile["profile_url"], profile["profile_hash"])
    targets = [("255.255.255.255", int(udp_port)), (multicast_group, int(udp_port))]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in targets:
            try:
                sock.sendto(hello, target)
            except OSError:
                pass
    finally:
        sock.close()


class UdpDiscovery:
    def __init__(self, registry: PeerRegistry, own_node_id: str, bind_host: str = "0.0.0.0", udp_port: int = DEFAULT_UDP_PORT):
        self.registry = registry
        self.own_node_id = own_node_id
        self.bind_host = bind_host
        self.udp_port = int(udp_port)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="mycelium-udp", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(1.0)
        try:
            sock.bind((self.bind_host, self.udp_port))
        except OSError:
            sock.close()
            return
        try:
            while not self.stop_event.is_set():
                try:
                    data, addr = sock.recvfrom(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    hello = parse_hello(data)
                    if hello["node_id"] == self.own_node_id:
                        continue
                    profile = fetch_profile(hello["profile_url"])
                    if profile.get("profile_hash") and profile.get("profile_hash") != hello.get("profile_hash"):
                        continue
                    self.registry.upsert(profile, source=f"udp:{addr[0]}")
                except Exception:
                    continue
        finally:
            sock.close()


def run_probe(probe_cmd: list[str]) -> dict[str, Any]:
    result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"probe exited {result.returncode}")
    return json.loads(result.stdout)


def infer_advertised_host(preferred: str | None = None) -> str:
    if preferred:
        return preferred
    # Does not contact the internet; UDP connect picks outbound interface.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def load_profile(args: argparse.Namespace) -> dict[str, Any]:
    if args.profile_file:
        raw = json.loads(Path(args.profile_file).read_text())
    elif args.probe_cmd:
        raw = run_probe(args.probe_cmd)
    else:
        probe_path = Path(__file__).with_name("probe.py")
        raw = run_probe([sys.executable, str(probe_path)])
    return normalize_profile(raw, advertised_host=args.advertise_host or infer_advertised_host(), port=args.port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mycelium node profile broadcaster")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP bind host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP profile server port")
    parser.add_argument("--advertise-host", help="host/IP peers should use to fetch /profile")
    parser.add_argument("--udp-port", type=int, default=DEFAULT_UDP_PORT, help="UDP discovery port")
    parser.add_argument("--no-udp", action="store_true", help="disable UDP LAN discovery")
    parser.add_argument("--seed-url", action="append", default=[], help="seed node base URL to POST /announce")
    parser.add_argument("--profile-file", help="use existing probe JSON")
    parser.add_argument("--probe-cmd", nargs="+", help="command that prints raw profile JSON")
    parser.add_argument("--announce-interval", type=float, default=15.0)
    parser.add_argument("--once", action="store_true", help="announce once, print own profile, exit")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    profile = load_profile(args)
    registry = PeerRegistry(ttl_seconds=max(60.0, args.announce_interval * 4))
    server = None
    discovery = None
    if not args.once:
        server = start_http_server(args.host, args.port, profile, registry, quiet=args.quiet)
        if not args.no_udp:
            discovery = UdpDiscovery(registry, own_node_id=profile["node_id"], udp_port=args.udp_port)
            discovery.start()

    def announce_all() -> None:
        if not args.no_udp:
            udp_announce_once(profile, udp_port=args.udp_port)
        for seed in args.seed_url:
            try:
                announce_to_seed(profile, seed)
            except Exception as exc:
                if not args.quiet:
                    print(f"seed announce failed {seed}: {exc}", file=sys.stderr)

    if args.once:
        announce_all()
        print(json.dumps(profile, indent=2, sort_keys=True))
        return 0

    print(json.dumps({"ok": True, "node_id": profile["node_id"], "profile_url": profile["profile_url"]}, indent=2))
    try:
        while True:
            announce_all()
            time.sleep(args.announce_interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if discovery:
            discovery.stop()
        if server:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
