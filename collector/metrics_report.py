#!/usr/bin/env python3
"""Wertet /var/log/wt-metrics.csv aus: Grundlast, Mehrverbrauch pro Call, Kapazitaet.

Aufruf:  python3 metrics_report.py [datei]
"""
from __future__ import annotations

import csv
import statistics
import sys
from collections import defaultdict

path = sys.argv[1] if len(sys.argv) > 1 else "/var/log/wt-metrics.csv"
rows = list(csv.DictReader(open(path)))
if not rows:
    print("keine Daten")
    raise SystemExit(1)

svc = ["drachtio", "router", "adapter", "rtpengine"]


def f(row, key):
    v = row.get(key) or ""
    try:
        return float(v)
    except ValueError:
        return None


by_calls = defaultdict(list)
for r in rows:
    c = int(f(r, "calls") or 0)
    by_calls[c].append(r)

print(f"Zeilen: {len(rows)}   Messpunkt-Abstand: ~10 s")
print(f"Messwerte je Call-Zahl: {sorted(by_calls)}\n")

base = by_calls.get(0, [])

print("Grundlast (calls = 0):")
for s in svc:
    vals = [f(r, f"{s}_rss_mb") for r in base if f(r, f"{s}_rss_mb") is not None]
    if vals:
        print(f"  {s:10s} RSS {statistics.mean(vals):6.1f} MB   CPU {statistics.mean([f(r, f'{s}_cpu_pct') or 0 for r in base]):4.1f} %")
print()

print("Pro Call-Zahl (Mittelwerte):")
print(f"  {'calls':>5} {'RSS ges.':>9} {'dra':>7} {'rou':>7} {'ada':>7} {'CPU ges.':>9} {'N':>4}")
for c in sorted(by_calls):
    rs = by_calls[c]
    total_rss = sum(
        statistics.mean([f(r, f"{s}_rss_mb") for r in rs if f(r, f"{s}_rss_mb") is not None] or [0])
        for s in svc
    )
    per = {
        s: statistics.mean([f(r, f"{s}_rss_mb") for r in rs if f(r, f"{s}_rss_mb") is not None] or [0])
        for s in svc
    }
    cpu = sum(statistics.mean([f(r, f"{s}_cpu_pct") or 0 for r in rs]) for s in svc)
    print(f"  {c:5d} {total_rss:8.1f}M {per['drachtio']:6.1f}M {per['router']:6.1f}M {per['adapter']:6.1f}M {cpu:8.1f}% {len(rs):4d}")

# Delta pro Call aus dem groessten gemessenen Punkt
if base and len(by_calls) > 1:
    top = max(by_calls)
    if top > 0:
        rs = by_calls[top]
        base_rss = sum(statistics.mean([f(r, f"{s}_rss_mb") for r in base if f(r, f"{s}_rss_mb") is not None] or [0]) for s in svc)
        top_rss = sum(statistics.mean([f(r, f"{s}_rss_mb") for r in rs if f(r, f"{s}_rss_mb") is not None] or [0]) for s in svc)
        base_cpu = sum(statistics.mean([f(r, f"{s}_cpu_pct") or 0 for r in base]) for s in svc)
        top_cpu = sum(statistics.mean([f(r, f"{s}_cpu_pct") or 0 for r in rs]) for s in svc)
        d_rss = (top_rss - base_rss) / top
        d_cpu = (top_cpu - base_cpu) / top
        print(f"\nMehrverbrauch pro Call (aus {top} gleichzeitigen Calls):")
        print(f"  RAM  {d_rss:6.1f} MB/Call")
        print(f"  CPU  {d_cpu:6.1f} %/Call  (1 Kern = 100 %)")

        mem_total = statistics.mean([f(r, "mem_total_mb") or 0 for r in rows])
        if d_rss > 0:
            nutzbar = mem_total * 0.8 - base_rss
            print(f"\nKapazitaet (80 % von {mem_total:.0f} MB, abzueglich Grundlast):")
            print(f"  RAM-Grenze  ~{int(nutzbar / d_rss)} Calls")
        if d_cpu > 0:
            vcpu = statistics.mean([f(r, "vcpu") or 2 for r in rows])
            print(f"  CPU-Grenze  ~{int(vcpu * 80 / d_cpu)} Calls (bei 80 % Last, {vcpu:.0f} vCPU)")
