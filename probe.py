#!/usr/bin/env python3
"""
Mycelium Node Capability Probe
Join-time auto-profile for layer allocation.
Stdlib only — no pip installs required.
"""
import json, os, platform, subprocess, sys, time, urllib.request

# ── helpers ──────────────────────────────────────────────────────────────────

def run(cmd, timeout=10):
    """Run shell command, return stdout string or None."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None

def to_gb(val_bytes):
    return round(val_bytes / (1024**3), 2)

def to_mb(val_bytes):
    return round(val_bytes / (1024**2), 2)

# ── bandwidth lookup tables ──────────────────────────────────────────────────

APPLE_BANDWIDTH = {
    # chip name prefix → unified memory bandwidth GB/s
    # Order matters: longer matches first to avoid "Apple M4" shadowing "Apple M4 Pro"
    "Apple M1 Ultra":     819.2,
    "Apple M2 Ultra":     819.2,
    "Apple M1 Max":       400.0,
    "Apple M2 Max":       400.0,
    "Apple M3 Max":       400.0,
    "Apple M4 Max":       546.0,
    "Apple M1 Pro":       204.8,
    "Apple M2 Pro":       273.2,
    "Apple M3 Pro":       300.8,
    "Apple M4 Pro":       273.0,
    "Apple M1":            68.25,
    "Apple M2":           100.0,
    "Apple M3":           120.0,
    "Apple M4":           120.0,
}

NVIDIA_VRAM_BANDWIDTH = {
    # GPU name → VRAM bandwidth GB/s
    "RTX 4090":      1008.0,
    "RTX 4080":       716.8,
    "RTX 4080 SUPER": 736.0,
    "RTX 4070 Ti":    504.0,
    "RTX 4070":       504.0,
    "RTX 4060 Ti":     288.0,
    "RTX 4060":        288.0,
    "RTX 3090":        936.0,
    "RTX 3090 Ti":    1008.0,
    "RTX 3080":        760.0,
    "RTX 3080 Ti":     912.0,
    "RTX 3070":        448.0,
    "RTX 3060":        360.0,
    "RTX 3060 Ti":     448.0,
    "RTX 2080 Ti":     616.0,
    "RTX 2080":        448.0,
    "RTX A100":       1555.0,   # 80GB HBM2
    "RTX A6000":      768.0,
    "RTX A5000":      640.0,
    "RTX A4000":      448.0,
    "A100-SXM4":      2039.0,   # 80GB HBM2e
    "H100":           3350.0,   # HBM3
    "H200":           4800.0,   # HBM3e
    "L40S":            864.0,
    "L4":              300.0,
}

AMD_VRAM_BANDWIDTH = {
    "Radeon RX 7900 XTX": 960.0,
    "Radeon RX 7900 XT":  800.0,
    "Radeon RX 6900 XT":  512.0,
    "Radeon RX 6800":     512.0,
    "Radeon RX 7800 XT":  624.0,
}

# ── platform detection ───────────────────────────────────────────────────────

IS_MAC = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"
PAGE_SIZE = int(run("getconf PAGE_SIZE") or 4096)

# ── CPU ──────────────────────────────────────────────────────────────────────

def get_cpu():
    if IS_MAC:
        name = run("sysctl -n machdep.cpu.brand_string") or "unknown"
        if "Apple" in name:
            # Apple Silicon — sysctl gives generic name, use chip from system_profiler
            chip = run("system_profiler SPHardwareDataType | grep 'Chip'") or ""
            if chip:
                name = chip.split(":", 1)[1].strip()
        cores = int(run("sysctl -n hw.ncpu") or 0) or None
        pcores = run("sysctl -n hw.perflevel0.physicalcpu")
        ecores = run("sysctl -n hw.perflevel1.physicalcpu")
        max_clock = None  # Apple Silicon doesn't expose via sysctl reliably
        arch = platform.machine()
        return {
            "name": name,
            "cores": cores,
            "p_cores": int(pcores) if pcores else None,
            "e_cores": int(ecores) if ecores else None,
            "max_clock_mhz": max_clock,
            "arch": arch,
        }
    else:
        # Linux: try lscpu, fall back to /proc/cpuinfo
        name = run("lscpu 2>/dev/null | awk -F: '/Model name/ {gsub(/^ +/, \"\", $2); print $2}'") or ""
        name = name.strip()
        if not name or name == "-":
            # ARM/Apple Silicon containers or no model name
            impl = run("grep 'CPU implementer' /proc/cpuinfo | head -1 | awk -F': *' '{print $2}'") or ""
            arch = platform.machine()
            if impl == "0x61":
                name = "Apple Silicon (virtualized)"
            elif arch in ("x86_64",):
                name = run("grep 'model name' /proc/cpuinfo | head -1 | cut -d: -f2") or "unknown"
                name = name.strip()
            else:
                name = f"{arch} processor"
        cores = int(run("nproc") or 0) or None
        mhz = run("lscpu | grep 'MHz max' | cut -d: -f2")
        mhz = float(mhz) if mhz else None
        arch = platform.machine()
        return {
            "name": name,
            "cores": cores,
            "p_cores": None,
            "e_cores": None,
            "max_clock_mhz": mhz,
            "arch": arch,
        }

# ── RAM ──────────────────────────────────────────────────────────────────────

def get_ram():
    """Return (total_gb, available_gb, bandwidth_gbps_or_None)."""
    if IS_MAC:
        total = int(run("sysctl -n hw.memsize") or 0)
        # available from vm_stat
        vm = run("vm_stat") or ""
        avail = 0
        for line in vm.splitlines():
            if "free" in line.lower() or "inactive" in line.lower():
                num = line.split()[-1].rstrip(".")
                try:
                    avail += int(num) * PAGE_SIZE
                except ValueError:
                    pass
        # bandwidth from chip name
        chip = run("system_profiler SPHardwareDataType | grep 'Chip'") or ""
        chip_name = chip.split(":", 1)[1].strip() if chip else ""
        bw = None
        for key, val in APPLE_BANDWIDTH.items():
            if key in chip_name:
                bw = val
                break
        return to_gb(total), to_gb(avail), bw

    else:
        total = int(run("grep MemTotal /proc/meminfo | awk '{print $2}'") or 0) * 1024
        avail = int(run("grep MemAvailable /proc/meminfo | awk '{print $2}'") or 0) * 1024
        # bandwidth from dmidecode if root
        bw = None
        dmi = run("dmidecode -t memory 2>/dev/null | grep -E 'Speed:|Type:'") or ""
        if dmi:
            # parse DDR type and speed
            for line in dmi.splitlines():
                if "Speed:" in line and "No Module" not in line:
                    speed_str = line.split(":", 1)[1].strip()
                    if "MT/s" in speed_str or "MHz" in speed_str:
                        try:
                            mts = int(speed_str.split()[0])
                            # DDR transfer rate * 8 bytes per channel
                            bw = round(mts * 8 / 1000, 1)
                        except ValueError:
                            pass
                        break
        return to_gb(total), to_gb(avail), bw

# ── GPU / VRAM ───────────────────────────────────────────────────────────────

def get_gpu():
    """Detect GPU(s), return list of GPU dicts."""
    gpus = []

    # NVIDIA
    nvidia = run("nvidia-smi --query-gpu=name,memory.total,memory.used,memory.free --format=csv,noheader,nounits")
    if nvidia:
        for line in nvidia.strip().splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 4:
                name = parts[0]
                vram_total_mb = int(parts[1])
                vram_used_mb = int(parts[2])
                vram_free_mb = int(parts[3])
                bw = None
                for key, val in NVIDIA_VRAM_BANDWIDTH.items():
                    if key.lower() in name.lower():
                        bw = val
                        break
                gpus.append({
                    "name": name,
                    "vendor": "nvidia",
                    "vram_total_gb": round(vram_total_mb / 1024, 2),
                    "vram_available_gb": round(vram_free_mb / 1024, 2),
                    "vram_bandwidth_gbps": bw,
                    "backend": "cuda",
                })

    # Apple Silicon GPU
    if IS_MAC:
        chip = run("system_profiler SPHardwareDataType | grep 'Chip'") or ""
        chip_name = chip.split(":", 1)[1].strip() if chip else ""
        if chip_name.startswith("Apple M"):
            gpu_info = run("system_profiler SPDisplaysDataType") or ""
            # count GPU cores
            gpu_cores = None
            for line in gpu_info.splitlines():
                if "Total Number of Cores" in line:
                    try:
                        gpu_cores = int(line.split(":")[1].strip())
                    except ValueError:
                        pass
            bw = None
            for key, val in APPLE_BANDWIDTH.items():
                if key in chip_name:
                    bw = val
                    break
            # Apple Silicon: unified memory, GPU shares RAM
            gpus.append({
                "name": chip_name + " GPU",
                "vendor": "apple",
                "vram_total_gb": None,      # unified — same as RAM
                "vram_available_gb": None,
                "vram_bandwidth_gbps": bw,   # unified bandwidth
                "gpu_cores": gpu_cores,
                "backend": "metal",
                "unified": True,
            })

    # AMD (Linux via rocm-smi)
    amd = run("rocm-smi --showproductname --json 2>/dev/null")
    if amd:
        try:
            data = json.loads(amd)
            for card_key, card_val in data.items():
                if isinstance(card_val, dict):
                    name = card_val.get("Card series", card_val.get("Card model", "AMD GPU"))
                    bw = None
                    for key, val in AMD_VRAM_BANDWIDTH.items():
                        if key.lower() in name.lower():
                            bw = val
                            break
                    gpus.append({
                        "name": name,
                        "vendor": "amd",
                        "vram_bandwidth_gbps": bw,
                        "backend": "rocm",
                    })
        except json.JSONDecodeError:
            pass

    return gpus if gpus else []

# ── storage ──────────────────────────────────────────────────────────────────

def get_storage():
    if IS_MAC:
        disk = run("df -k / | tail -1")
        if disk:
            parts = disk.split()
            total = int(parts[1]) * 1024
            avail = int(parts[3]) * 1024
        else:
            total, avail = 0, 0
        # detect SSD vs HDD
        disk_type = run("diskutil info / | grep 'Solid State'") or ""
        is_ssd = "Yes" in disk_type
        # NVMe detection
        nvme = run("system_profiler SPNVMeDataType 2>/dev/null | head -5") or ""
        storage_type = "nvme" if nvme else ("ssd" if is_ssd else "hdd")
        return {
            "total_gb": to_gb(total),
            "available_gb": to_gb(avail),
            "type": storage_type,
        }
    else:
        disk = run("df -k / | tail -1")
        if disk:
            parts = disk.split()
            total = int(parts[1]) * 1024
            avail = int(parts[3]) * 1024
        else:
            total, avail = 0, 0
        # rotational flag
        rot = run("cat /sys/block/sda/queue/rotational 2>/dev/null") or run("cat /sys/block/vda/queue/rotational 2>/dev/null")
        if rot == "0":
            storage_type = "ssd"
            # check for NVMe
            if run("ls /sys/class/nvme 2>/dev/null"):
                storage_type = "nvme"
        elif rot == "1":
            storage_type = "hdd"
        else:
            storage_type = "unknown"
        return {
            "total_gb": to_gb(total),
            "available_gb": to_gb(avail),
            "type": storage_type,
        }

# ── power / battery ──────────────────────────────────────────────────────────

def get_power():
    if IS_MAC:
        batt = run("pmset -g batt") or ""
        on_ac = "AC Power" in batt
        pct = None
        if batt:
            for part in batt.split():
                if "%" in part:
                    try:
                        pct = int(part.strip(";%"))
                    except ValueError:
                        pass
        time_left = None
        if not on_ac and batt:
            # try to parse "X:YY remaining"
            for line in batt.splitlines():
                if "remaining" in line.lower():
                    t = line.split("remaining")[0].strip().split()[-1]
                    if ":" in t:
                        time_left = t
        return {
            "on_ac_power": on_ac,
            "battery_pct": pct,
            "battery_time_remaining": time_left,
        }
    else:
        # Linux: check /sys/class/power_supply
        ac = run("cat /sys/class/power_supply/AC*/online 2>/dev/null")
        bat_cap = run("cat /sys/class/power_supply/BAT*/capacity 2>/dev/null")
        bat_status = run("cat /sys/class/power_supply/BAT*/status 2>/dev/null")
        has_battery = bat_cap is not None
        # No battery present = plugged in (desktop/server/container)
        on_ac = True if not has_battery else (ac == "1" or bat_status == "Charging")
        pct = int(bat_cap) if bat_cap else None
        return {
            "on_ac_power": on_ac,
            "battery_pct": pct,
            "battery_time_remaining": None,
        }

# ── network speed test ───────────────────────────────────────────────────────

def speedtest_download(bytes_to_fetch=5_000_000, timeout=20):
    """Download from multiple free endpoints, return Mbps or None."""
    endpoints = [
        f"https://speed.cloudflare.com/__down?bytes={bytes_to_fetch}",
        f"https://speed.hetzner.de/100MB.bin",
        f"http://ipv4.download.thinkbroadband.com/10MB.zip",
    ]
    for url in endpoints:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "MyceliumProbe/1.0"})
            start = time.monotonic()
            resp = urllib.request.urlopen(req, timeout=timeout)
            data = resp.read(bytes_to_fetch)
            elapsed = time.monotonic() - start
            if elapsed < 0.1:
                elapsed = 0.1
            mbits = (len(data) * 8) / 1_000_000
            return round(mbits / elapsed, 2)
        except Exception:
            continue
    return None

def speedtest_upload(bytes_to_send=2_000_000, timeout=20):
    """Upload to free speed test endpoint, return Mbps or None."""
    url = "https://speed.cloudflare.com/__up"
    payload = os.urandom(bytes_to_send)
    try:
        req = urllib.request.Request(url, data=payload, method="POST",
                                      headers={"Content-Type": "application/octet-stream",
                                               "User-Agent": "MyceliumProbe/1.0"})
        start = time.monotonic()
        resp = urllib.request.urlopen(req, timeout=timeout)
        resp.read()
        elapsed = time.monotonic() - start
        if elapsed < 0.1:
            elapsed = 0.1
        mbits = (len(payload) * 8) / 1_000_000
        return round(mbits / elapsed, 2)
    except Exception:
        return None

# ── location via IP geolocation ──────────────────────────────────────────────

def get_location():
    """Free IP geolocation via ip-api.com (no key, 45 req/min)."""
    try:
        resp = urllib.request.urlopen("http://ip-api.com/json/?fields=lat,lon,city,country,isp", timeout=10)
        data = json.loads(resp.read())
        if data.get("lat") and data.get("lon"):
            return {
                "lat": round(data["lat"], 2),
                "lon": round(data["lon"], 2),
                "city": data.get("city"),
                "country": data.get("country"),
                "isp": data.get("isp"),
            }
    except Exception:
        pass
    return None

# ── backend detection ────────────────────────────────────────────────────────

def get_backends():
    """Probe installed ML frameworks."""
    backends = []
    # Python-based checks
    checks = [
        ("torch_cpu",     "import torch"),
        ("torch_cuda",    "import torch; assert torch.cuda.is_available()"),
        ("torch_mps",     "import torch; assert torch.backends.mps.is_available()"),
        ("mlx",           "import mlx.core"),
        ("mlx_lm",        "import mlx_lm"),
        ("llama_cpp",     "import llama_cpp"),
        ("jax",           "import jax"),
        ("tensorflow",    "import tensorflow"),
        ("onnxruntime",   "import onnxruntime"),
        ("transformers",  "import transformers"),
        ("accelerate",    "import accelerate"),
        ("bitsandbytes",  "import bitsandbytes"),
    ]
    py = sys.executable
    for name, code in checks:
        r = run(f"{py} -c \"{code}\" 2>/dev/null")
        if r is not None:
            backends.append(name)
    # Binary checks
    if (run("which llama-cli 2>/dev/null") or run("which llama-server 2>/dev/null")) and "llama_cpp" not in backends:
        backends.append("llama_cpp")
    if run("which nvcc 2>/dev/null"):
        backends.append("cuda_toolkit")
    return backends

def get_supported_precision():
    """Estimate supported precisions from available backends."""
    precisions = set()
    # Python check for actual support
    py = sys.executable
    if run(f"{py} -c \"import torch\" 2>/dev/null") is not None:
        precisions.update(["fp32", "fp16"])
        if run(f"{py} -c \"import torch; assert torch.cuda.is_available()\" 2>/dev/null") is not None:
            precisions.update(["bf16", "int8", "int4"])  # most CUDA GPUs
    if run(f"{py} -c \"import mlx.core\" 2>/dev/null") is not None:
        precisions.update(["fp32", "fp16", "int8", "int4"])
    precisions.update(["fp32"])  # CPU fallback always
    return sorted(precisions)

# ── main ─────────────────────────────────────────────────────────────────────

def profile():
    print("Probing hardware...", file=sys.stderr)

    cpu = get_cpu()
    print(f"  CPU: {cpu['name']}", file=sys.stderr)

    ram_total, ram_avail, ram_bw = get_ram()
    print(f"  RAM: {ram_total} GB ({ram_avail} GB avail, {ram_bw} GB/s)", file=sys.stderr)

    gpus = get_gpu()
    for g in gpus:
        print(f"  GPU: {g['name']} ({g.get('vram_bandwidth_gbps', '?')} GB/s)", file=sys.stderr)

    storage = get_storage()
    print(f"  Disk: {storage['available_gb']} GB avail ({storage['type']})", file=sys.stderr)

    power = get_power()
    print(f"  Power: {'AC' if power['on_ac_power'] else 'Battery'} {power['battery_pct']}%", file=sys.stderr)

    print("  Speed test (download)...", file=sys.stderr)
    dl = speedtest_download()
    print(f"  Speed test (upload)...", file=sys.stderr)
    ul = speedtest_upload()

    location = get_location()
    if location:
        print(f"  Location: {location.get('city')}, {location.get('country')}", file=sys.stderr)

    backends = get_backends()
    precision = get_supported_precision()

    # determine unified memory
    unified = any(g.get("unified") for g in gpus)

    report = {
        "hostname": platform.node(),
        "platform": platform.system(),
        "device_class": _guess_device_class(),
        "arch": platform.machine(),

        "cpu": cpu,

        "ram": {
            "total_gb": ram_total,
            "available_gb": ram_avail,
            "bandwidth_gbps": ram_bw,
            "unified_with_gpu": unified,
        },

        "gpus": gpus,

        "storage": storage,
        "power": power,

        "network": {
            "download_mbps": dl,
            "upload_mbps": ul,
        },

        "location": location,

        "backends": backends,
        "supported_precision": precision,

        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    return report


def _guess_device_class():
    if IS_MAC:
        model = run("sysctl -n hw.model") or ""
        if "MacBook" in model or "Air" in model:
            return "laptop"
        if "MacBookPro" in model:
            return "laptop"
        if "iMac" in model or "mini" in model or "Studio" in model or "MacPro" in model:
            return "desktop"
        return "desktop"
    else:
        # check if laptop via battery
        bat = run("ls /sys/class/power_supply/BAT* 2>/dev/null")
        if bat:
            return "laptop"
        return "desktop"


if __name__ == "__main__":
    report = profile()
    print(json.dumps(report, indent=2))
