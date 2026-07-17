# Mycelium Tier-1 Node Broadcast Protocol

Goal: let every trusted node publish the static join-time information the router needs.

## Transport

Two lightweight paths, both stdlib-only:

1. HTTP seed announce — works over LAN, Tailscale, SSH-accessible networks.
   - Node runs `GET /profile`.
   - Node posts full profile to seed `POST /announce`.
   - Router reads all known nodes from seed `GET /nodes`.

2. UDP LAN discovery — works on same broadcast-capable LAN.
   - Node sends tiny `mycelium.hello.v1` packet to UDP port 8789.
   - Packet contains only `node_id`, `profile_url`, `profile_hash`, `ts`.
   - Receiver fetches full profile from `profile_url`.

Use HTTP seed as canonical path. UDP is convenience for local discovery.

## HTTP endpoints

All JSON.

- `GET /health`
  - Returns `{ ok, protocol, node_id }`.

- `GET /profile`
  - Returns this node's full `mycelium.node_profile.v1` payload.

- `POST /announce`
  - Body: `{ "profile": <mycelium.node_profile.v1> }`.
  - Stores/refreshes peer in local registry.

- `GET /peers`
  - Returns remote peers only.

- `GET /nodes`
  - Returns self + peers. This is the router input.

## Router input shape

`GET /nodes` returns:

```json
{
  "ok": true,
  "nodes": {
    "node-id": {
      "node_id": "node-id",
      "role": "self|peer",
      "source": "self|http:<ip>|udp:<ip>",
      "seen_at": 1784010000.0,
      "expires_at": 1784010120.0,
      "profile_hash": "sha256...",
      "profile": {
        "protocol": "mycelium.node_profile.v1",
        "node_id": "node-id",
        "profile_url": "http://host:8788/profile",
        "capabilities": {
          "device_class": "desktop|laptop|phone|server",
          "platform": "Darwin|Linux|Android",
          "arch": "arm64|aarch64|x86_64",
          "ram_total_gb": 48.0,
          "ram_available_gb": 22.7,
          "ram_bandwidth_gbps": 273.0,
          "unified_memory": true,
          "gpu_count": 1,
          "primary_gpu_name": "Apple M4 Pro GPU",
          "primary_gpu_backend": "metal",
          "vram_total_gb": null,
          "vram_available_gb": null,
          "vram_bandwidth_gbps": 273.0,
          "storage_available_gb": 140.8,
          "storage_type": "nvme",
          "on_ac_power": true,
          "battery_pct": 100,
          "download_mbps": 44.0,
          "upload_mbps": 18.0,
          "lan_ip": null,
          "backends": ["torch_mps", "mlx"],
          "supported_precision": ["fp16", "fp32", "int8", "int4"]
        }
      }
    }
  }
}
```

The full raw profile is preserved under `profile`; compact router-facing fields live under `profile.capabilities`.

## Example commands

Run M4 Pro as seed/router:

```bash
python3 mycelium_broadcast.py \
  --host 0.0.0.0 \
  --port 8788 \
  --advertise-host 100.84.252.4 \
  --probe-cmd python3 mycelium_probe.py
```

Announce a node to seed once:

```bash
python3 mycelium_broadcast.py \
  --once --no-udp \
  --profile-file outputs/linux-container-profile.json \
  --advertise-host <node-ip> \
  --seed-url http://100.84.252.4:8788
```

Router fetch:

```bash
curl http://100.84.252.4:8788/nodes
```

## Current claim boundary

Implemented and smoke-tested:

- stdlib HTTP profile server
- HTTP seed announce
- router-facing `/nodes` endpoint
- local UDP discovery
- M4 Pro seed receiving Linux container + Pixel 8 profiles

Not yet implemented:

- authentication / signatures
- NAT traversal
- dynamic updates beyond periodic re-announce
- pairwise latency/jitter probes
- Termux-native backend inventory via bridge
