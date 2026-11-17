#!/usr/bin/env python3
"""Ressourcen-Mitschnitt fuer den Warmtransfer-Stack (alle 10 s eine CSV-Zeile).

Gemessen wird pro Dienst: RSS, CPU-Anteil im Intervall und kumulierte CPU-Zeit.
Zusaetzlich: Anzahl aktiver RTP-Sockets des Adapters (= laufende Calls),
System-RAM und Load.

Kein Eingriff in den Anwendungscode - liest ausschliesslich /proc.
"""
from __future__ import annotations

import csv
import os
import subprocess
import time

INTERVAL = float(os.getenv("METRICS_INTERVAL", "10"))
OUT = os.getenv("METRICS_FILE", "/var/log/wt-metrics.csv")
UNITS = {
    "drachtio": "drachtio",
    "router": "router",
    "adapter": "s2a",
    "rtpengine": "rtpengine",
}
FIELDS = [
    "ts",
    "calls",
    "mem_used_mb",
    "mem_total_mb",
    "load1",
    "vcpu",
]
for name in UNITS:
    FIELDS += [f"{name}_rss_mb", f"{name}_cpu_pct", f"{name}_cpu_s"]


def main_pid(unit: str) -> int:
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "--value", unit],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return int(out or 0)
    except Exception:
        return 0


def read_proc(pid: int) -> tuple[float, float] | None:
    """(rss_mb, cpu_seconds) eines Prozesses."""
    if pid <= 0:
        return None
    try:
        with open(f"/proc/{pid}/status") as fh:
            rss_kb = 0
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                    break
        with open(f"/proc/{pid}/stat") as fh:
            parts = fh.read().split()
        cpu_s = (int(parts[13]) + int(parts[14])) / os.sysconf("SC_CLK_TCK")
        return rss_kb / 1024.0, cpu_s
    except (OSError, IndexError, ValueError):
        return None


def count_rtp_sockets(pid: int) -> int:
    """Aktive RTP-Ports des Adapters (ein Socket pro Call)."""
    if pid <= 0:
        return 0
    try:
        out = subprocess.run(
            ["ss", "-uanp"], capture_output=True, text=True, timeout=5
        ).stdout
    except Exception:
        return 0
    count = 0
    needle = f"pid={pid},"
    for line in out.splitlines()[1:]:
        if needle not in line:
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        try:
            # ss -uanp: Feld 3 ist die lokale Adresse, Feld 4 der Peer
            port = int(parts[3].rsplit(":", 1)[1])
        except (IndexError, ValueError):
            continue
        if 10000 <= port <= 20000:
            count += 1
    return count


def meminfo() -> tuple[float, float]:
    used = total = 0.0
    try:
        with open("/proc/meminfo") as fh:
            info = {line.split(":")[0]: line.split()[1] for line in fh if ":" in line}
        total = int(info["MemTotal"]) / 1024.0
        avail = int(info["MemAvailable"]) / 1024.0
        used = total - avail
    except Exception:
        pass
    return used, total


def main() -> None:
    new_file = not os.path.exists(OUT) or os.path.getsize(OUT) == 0
    prev: dict[str, float] = {}
    prev_ts = time.monotonic()

    with open(OUT, "a", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
            fh.flush()

        while True:
            now = time.monotonic()
            dt = now - prev_ts or INTERVAL
            row: dict[str, object] = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "mem_used_mb": round(meminfo()[0], 1),
                "mem_total_mb": round(meminfo()[1], 1),
                "load1": round(os.getloadavg()[0], 2),
                "vcpu": os.cpu_count() or 1,
            }
            for name, unit in UNITS.items():
                data = read_proc(main_pid(unit))
                if data is None:
                    row[f"{name}_rss_mb"] = ""
                    row[f"{name}_cpu_pct"] = ""
                    row[f"{name}_cpu_s"] = ""
                    continue
                rss, cpu_s = data
                before = prev.get(name)
                pct = ((cpu_s - before) / dt * 100.0) if before is not None else 0.0
                prev[name] = cpu_s
                row[f"{name}_rss_mb"] = round(rss, 1)
                row[f"{name}_cpu_pct"] = round(pct, 1)
                row[f"{name}_cpu_s"] = round(cpu_s, 1)

            row["calls"] = count_rtp_sockets(main_pid(UNITS["adapter"]))
            writer.writerow(row)
            fh.flush()
            prev_ts = now
            time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
