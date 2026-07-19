#!/usr/bin/env python3
"""
Mycelium Android/ADB Capability Probe
Runs from a host that has adb access to an Android phone.
Stdlib only. Emits join-time node capability JSON.
"""
import json
import re
import subprocess
import time
import sys

ADB = sys.argv[1:] if len(sys.argv) > 1 else ["adb"]

def adb(args, timeout=10):
    cmd = ADB + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None

def shell(cmd, timeout=10):
    return adb(["shell", cmd], timeout=timeout)

def gb_from_kb(kb):
    return round(int(kb) / 1024 / 1024, 2)

def parse_meminfo():
    out = shell("cat /proc/meminfo") or ""
    vals = {}
    for line in out.splitlines():
        m = re.match(r"(\w+):\s+(\d+)\s+kB", line)
        if m:
            vals[m.group(1)] = int(m.group(2))
    return {
        "total_gb": gb_from_kb(vals.get("MemTotal", 0)),
        "available_gb": gb_from_kb(vals.get("MemAvailable", vals.get("MemFree", 0))),
        "bandwidth_gbps": None,  # Android does not expose reliable RAM bw via adb shell
        "unified_with_gpu": True,
    }

def parse_cpu():
    cpuinfo = shell("cat /proc/cpuinfo") or ""
    hardware = None
    processor = None
    implementer = None
    part = None
    features = None
    cores = len(re.findall(r"^processor\s*:", cpuinfo, flags=re.M))
    for line in cpuinfo.splitlines():
        if line.startswith("Hardware"):
            hardware = line.split(":",1)[1].strip()
        elif line.startswith("Processor"):
            processor = line.split(":",1)[1].strip()
        elif line.startswith("CPU implementer") and not implementer:
            implementer = line.split(":",1)[1].strip()
        elif line.startswith("CPU part") and not part:
            part = line.split(":",1)[1].strip()
        elif line.startswith("Features") and not features:
            features = line.split(":",1)[1].strip().split()
    soc = shell("getprop ro.soc.model") or shell("getprop ro.board.platform") or hardware or "unknown"
    return {
        "name": soc,
        "cores": cores or None,
        "arch": (shell("uname -m") or "unknown"),
        "hardware": hardware,
        "processor": processor,
        "cpu_implementer": implementer,
        "cpu_part": part,
        "features": features[:40] if features else [],
        "max_clock_mhz": parse_cpu_max_mhz(),
    }

def parse_cpu_max_mhz():
    out = shell("for f in /sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq; do cat $f 2>/dev/null; done") or ""
    freqs = []
    for x in out.split():
        try:
            freqs.append(int(x) / 1000)
        except ValueError:
            pass
    return round(max(freqs), 1) if freqs else None

def parse_storage():
    out = shell("df -k /data | tail -1") or ""
    parts = out.split()
    if len(parts) >= 4:
        total = round(int(parts[1]) / 1024 / 1024, 2)
        avail = round(int(parts[3]) / 1024 / 1024, 2)
    else:
        total = avail = None
    return {"total_gb": total, "available_gb": avail, "type": "ufs_or_emmc"}

def parse_power():
    out = shell("dumpsys battery") or ""
    vals = {}
    for line in out.splitlines():
        if ":" in line:
            k, v = line.strip().split(":", 1)
            vals[k.strip()] = v.strip()
    plugged = vals.get("powered", "false") == "true" or vals.get("AC powered", "false") == "true" or vals.get("USB powered", "false") == "true" or vals.get("Wireless powered", "false") == "true"
    pct = None
    try:
        pct = int(vals.get("level", ""))
    except ValueError:
        pass
    return {
        "on_ac_power": plugged,
        "battery_pct": pct,
        "status": vals.get("status"),
        "temperature_c": round(int(vals.get("temperature", "0")) / 10, 1) if vals.get("temperature") else None,
    }

def parse_network():
    ip = shell("ip -4 addr show wlan0 | grep -oE 'inet [0-9.]+' | awk '{print $2}'")
    if not ip:
        ip = shell("ip route get 1.1.1.1 | awk '{for(i=1;i<=NF;i++) if($i==\"src\") print $(i+1)}'")
    return {"lan_ip": ip, "download_mbps": None, "upload_mbps": None}

def parse_location():
    # ADB shell shouldn't expose GPS without permissions. Use LAN/IP later from coordinator.
    return None

def parse_gpu():
    props = {
        "ro.hardware.egl": shell("getprop ro.hardware.egl"),
        "ro.opengles.version": shell("getprop ro.opengles.version"),
        "ro.hardware.vulkan": shell("getprop ro.hardware.vulkan"),
        "ro.gfx.driver.0": shell("getprop ro.gfx.driver.0"),
    }
    # Pixel 8 Pro uses Google Tensor G3 with Immortalis-G715s MC10 GPU. Android often hides exact GPU via adb shell.
    device = shell("getprop ro.product.device")
    model = shell("getprop ro.product.model")
    name = "unknown"
    if device == "husky" or model == "Pixel 8 Pro":
        name = "Immortalis-G715s MC10 (Pixel 8 Pro / Tensor G3)"
    return [{
        "name": name,
        "vendor": "arm/mali" if "Immortalis" in name else "unknown",
        "vram_total_gb": None,
        "vram_available_gb": None,
        "vram_bandwidth_gbps": None,
        "backend": "vulkan/opengl/android",
        "unified": True,
        "android_props": props,
    }]

def parse_backends():
    # ADB shell only; Termux inventory will be separate when bridge works.
    binaries = shell("command -v python3 python node llama-cli llama-server mlc_llm 2>/dev/null") or ""
    backends = []
    if "python" in binaries:
        backends.append("python_adb_shell")
    if "node" in binaries:
        backends.append("node")
    if "llama-cli" in binaries or "llama-server" in binaries:
        backends.append("llama_cpp")
    if "mlc_llm" in binaries:
        backends.append("mlc_llm")
    return backends

def profile():
    model = shell("getprop ro.product.model")
    device = shell("getprop ro.product.device")
    serial = adb(["get-serialno"])
    android = shell("getprop ro.build.version.release")
    sdk = shell("getprop ro.build.version.sdk")
    manufacturer = shell("getprop ro.product.manufacturer")
    soc = shell("getprop ro.soc.model") or shell("getprop ro.board.platform")
    return {
        "hostname": f"android-{serial}" if serial else "android-unknown",
        "platform": "Android",
        "device_class": "phone",
        "manufacturer": manufacturer,
        "model": model,
        "device": device,
        "serial": serial,
        "android_version": android,
        "android_sdk": sdk,
        "soc": soc,
        "arch": shell("uname -m"),
        "cpu": parse_cpu(),
        "ram": parse_meminfo(),
        "gpus": parse_gpu(),
        "storage": parse_storage(),
        "power": parse_power(),
        "network": parse_network(),
        "location": parse_location(),
        "backends": parse_backends(),
        "supported_precision": ["fp32"],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

if __name__ == "__main__":
    print(json.dumps(profile(), indent=2))
