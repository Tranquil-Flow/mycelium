# Mycelium Network Probe — ping, jitter, geolocation

Goal: turn broadcast layer inputs into measured pairwise network quality plus city-level geographic location so the allocator can use real link cost instead of just `upload_mbps`.

## Files

- `network_probe.py` — stdlib-only probe module + CLI.
- `test_network_probe.py` — probe regression tests.
- `outputs/network-probe-m4pro.json` — latest measured link matrix artifact.

## Why TCP/UDP probes instead of ICMP

ICMP requires raw socket privileges. Docker and Termux usually deny that. Mycelium uses TCP handshake timing against each peer's HTTP `profile_url`, plus UDP send/receive timing for fallback. No raw sockets.

## Probe types

- `tcp_ping(host, port)` — TCP handshake RTT.
- `udp_ping(host, port)` — UDP datagram round-trip; if no echo, falls back to send-side timing.
- `http_ping(url)` — full GET round-trip including server-side work.

For each measurement we record:

- `count`
- `min_ms`, `avg_ms`, `max_ms`, `p50_ms`, `p95_ms`
- `stddev_ms`
- `jitter_ms` (mean absolute deviation around the average)
- `loss_ratio`

Jitter formula: `mean(|x - mean(x)|)`. Stddev uses population formula `pstdev`.

## Geographic location

- `lookup_ip_geolocation()` queries `ip-api.com` and falls back to `ipwho.is`.
- Returns `lat`, `lon`, `city`, `country`, `isp`, `precision="city"`.
- IP-API free tier is 45 req/min; if rate-limited, we fall back automatically.
- Per-node `location` field from broadcast layer is normalized to `precision = city|country|unknown`.

## Pairwise matrix

CLI:

```bash
python3 network_probe.py \
  --nodes-url http://100.84.252.4:8788/nodes \
  --self m4pro \
  --probe-attempts 10 \
  --probe-timeout 3.0 \
  --probe-port 8788 \
  --out outputs/network-probe-m4pro.json
```

The probe target is the URL in each peer `profile_url`, so SSH-reverse-tunneled or LAN-routed peers work transparently. Probe respects the port inside `profile_url` if it differs from `--probe-port`.

Output:

```json
{
  "ok": true,
  "protocol": "mycelium.network_probe.v1",
  "self": "m4pro",
  "self_location": { "city": "Berlin", "country": "Germany", "lat": 52.49, "lon": 13.36, "precision": "city" },
  "locations": { "...": "..." },
  "pairwise": {
    "m4pro->4b982606bdf4": {
      "samples": 10,
      "ping_avg_ms": 0.34,
      "ping_min_ms": 0.34,
      "ping_max_ms": 0.34,
      "ping_p50_ms": 0.34,
      "ping_p95_ms": 0.34,
      "jitter_ms": 0.0,
      "stddev_ms": 0.0,
      "loss_ratio": 0.0,
      "distance_km": 0.0,
      "location_src": { "...": "..." },
      "location_dst": { "...": "..." },
      "source": "probe"
    }
  },
  "errors": [],
  "timestamp": "..."
}
```

Reverse direction `b->a` is mirrored as `samples=0` with `geo_estimate` or `none` source. We only probe the actively chosen direction from `--self`.

If a probe fails, `ping_avg_ms` is filled from a distance-based fallback (`max(5 ms, dist * 0.005 + 5 ms)`) and marked `source: "geo_estimate"`.

## Allocator integration

`layer_planner.py` now consumes the link matrix:

```bash
python3 layer_planner.py \
  --nodes-file outputs/m4pro-router-nodes.json \
  --model-id llama-3.2-3b-ish \
  --num-layers 28 \
  --hidden-size 3072 \
  --context-length 2048 \
  --link-matrix-file outputs/network-probe-m4pro.json \
  --self-node-id m4pro \
  --out outputs/layer-plan-with-measured-links.json
```

`estimate_link_ms(src, dst, spec)` prefers:

- measured `ping_avg_ms` + measured `jitter_ms` from `dst.pairwise_*`,
- otherwise falls back to upload-based transfer estimate plus haversine distance latency floor.

So when we have N≥2 eligible nodes with measured ping/jitter, the allocator's stage-to-stage transfer cost becomes real, not synthetic.

## Live run against current cluster

- M4 Pro seeded at `100.84.252.4:8788`
- Linux container reachable via SSH reverse tunnel at `127.0.0.1:18788` on M4
- Pixel 8 Pro Tailscale offline at the moment of run

Probe result:
```text
self m4pro -> Berlin, Germany, lat 52.49 lon 13.36, precision city
locations
  4b982606bdf4 Berlin Germany
  m4pro        Berlin Germany
pairwise
  m4pro->4b982606bdf4 samples=10 avg=0.34ms min=0.34ms p95=0.34ms jitter=0.00ms loss=0.00 dist_km=0.0 source=probe
errors []
```

This proves:
- TCP-based ping works through the SSH reverse tunnel.
- Stdlib-only probe produces min/avg/max/p50/p95/jitter/stddev/loss for each pair.
- Pairwise matrix is emitted in router-consumable form.
- Layer planner now consumes measured ping/jitter when provided.

## Verification

```bash
python3 -m py_compile probe.py probe_android_adb.py mycelium_broadcast.py network_probe.py layer_planner.py test_broadcast.py test_layer_planner.py test_network_probe.py
python3 -m unittest -v
```

Current result:
```text
Ran 20 tests
OK
```

## Claim boundary / not done

Implemented:
- TCP / UDP / HTTP ping with min/avg/max/p50/p95/jitter/stddev/loss
- IP geolocation with city-level precision and fallback endpoints
- Pairwise matrix builder with mirror entries
- Per-pair routing via `profile_url` (works with SSH reverse tunnels)
- Allocator integration so measured ping/jitter feed transfer cost

Not implemented yet:
- real cross-ISP throughput probes (TCP_RR / iperf-style streams)
- bandwidth-aware per-layer MB transfer estimate
- jitter-driven buffering / KV prefetch hint
- signed claimed geolocation (currently trusts IP-API)
- continuous background re-probe / telemetry