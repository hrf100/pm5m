"""Resource monitor (Linux /proc; no extra dependencies).

Sampled every 2 s, kept for 1 hour in memory:
  * system CPU %, process CPU (% of whole machine, and cores)
  * system memory %, process RSS
  * NIC rx/tx rates (whole server, all processes, excludes lo)
  * app feed traffic (bytes received on PolyBolt / market / Binance sockets)
Totals are counted from the first sample after start.
"""
import os
import shutil
import time
from collections import deque
from typing import Callable, Optional

CLK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
NCPU = os.cpu_count() or 1


def parse_cpu_line(line: str):
    parts = line.split()
    if not parts or parts[0] != "cpu":
        return None
    v = [int(x) for x in parts[1:9]]            # user nice system idle iowait irq softirq steal
    return sum(v), v[3] + v[4]                  # (total, idle + iowait)


def parse_meminfo(text: str) -> dict:
    d = {}
    for ln in text.splitlines():
        k, _, v = ln.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            d[k] = int(v.split()[0]) * 1024
    return d


def parse_netdev(text: str):
    rx = tx = 0
    for ln in text.splitlines()[2:]:
        name, _, rest = ln.partition(":")
        name = name.strip()
        if not rest or name == "lo":
            continue
        f = rest.split()
        rx += int(f[0])
        tx += int(f[8])
    return rx, tx


def parse_self_stat_ticks(text: str) -> int:
    tail = text[text.rindex(")") + 2:].split()  # tail[0] is field 3 (state)
    return int(tail[11]) + int(tail[12])        # utime (14) + stime (15)


def parse_vmrss(text: str) -> Optional[int]:
    for ln in text.splitlines():
        if ln.startswith("VmRSS:"):
            return int(ln.split()[1]) * 1024
    return None


def read_raw(feed_bytes: Callable[[], int], http_out: Callable[[], int]) -> dict:
    d = {"t": time.time(), "feed_bytes": feed_bytes(), "http_out": http_out()}
    try:
        with open("/proc/stat") as f:
            c = parse_cpu_line(f.readline())
        if c:
            d["cpu_total"], d["cpu_idle"] = c
    except OSError:
        pass
    try:
        with open("/proc/meminfo") as f:
            m = parse_meminfo(f.read())
        d["mem_total"], d["mem_avail"] = m.get("MemTotal"), m.get("MemAvailable")
    except OSError:
        pass
    try:
        with open("/proc/net/dev") as f:
            d["rx"], d["tx"] = parse_netdev(f.read())
    except OSError:
        pass
    try:
        with open("/proc/self/stat") as f:
            d["proc_ticks"] = parse_self_stat_ticks(f.read())
        with open("/proc/self/status") as f:
            d["rss"] = parse_vmrss(f.read())
    except OSError:
        pass
    return d


def _dir_size(path: str, limit: int = 200_000) -> int:
    total, n = 0, 0
    for root, _, files in os.walk(path):
        for f in files:
            n += 1
            if n > limit:
                return total
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


class SysMon:
    def __init__(self, data_dir: str, feed_bytes: Callable[[], int], http_out: Callable[[], int],
                 window: int = 1800, reader: Optional[Callable[[], dict]] = None):
        self.data_dir = data_dir
        self.reader = reader or (lambda: read_raw(feed_bytes, http_out))
        self.hist = deque(maxlen=window)
        self.prev = None
        self.first = None
        self.cur_raw = None
        self._disk = (0.0, None, None)           # (checked_at, free, data_size)

    def sample(self) -> Optional[dict]:
        cur = self.reader()
        self.cur_raw = cur
        prev, self.prev = self.prev, cur
        if self.first is None:
            self.first = cur
        if prev is None:
            return None
        dt = max(cur["t"] - prev["t"], 1e-3)
        p = {"t": round(cur["t"], 1)}
        if "cpu_total" in cur and "cpu_total" in prev:
            dtot = cur["cpu_total"] - prev["cpu_total"]
            didle = cur["cpu_idle"] - prev["cpu_idle"]
            p["cpu_sys"] = round(100.0 * (1 - didle / dtot), 1) if dtot > 0 else None
        if "proc_ticks" in cur and "proc_ticks" in prev:
            cores = (cur["proc_ticks"] - prev["proc_ticks"]) / CLK / dt
            p["cores"] = round(max(cores, 0.0), 3)
            p["cpu_proc"] = round(100.0 * cores / NCPU, 2)
        if cur.get("mem_total") and cur.get("mem_avail") is not None:
            p["mem_sys"] = round(100.0 * (1 - cur["mem_avail"] / cur["mem_total"]), 1)
        if cur.get("rss") is not None:
            p["rss_mb"] = round(cur["rss"] / 1e6, 1)
        if "rx" in cur and "rx" in prev:
            p["rx_bps"] = round(max(cur["rx"] - prev["rx"], 0) / dt)
            p["tx_bps"] = round(max(cur["tx"] - prev["tx"], 0) / dt)
        p["feed_bps"] = round(max(cur["feed_bytes"] - prev["feed_bytes"], 0) / dt)
        p["http_bps"] = round(max(cur["http_out"] - prev["http_out"], 0) / dt)
        self.hist.append(p)
        return p

    def _disk_info(self):
        checked, free, size = self._disk
        if time.time() - checked > 300 or free is None:
            try:
                free = shutil.disk_usage(self.data_dir).free
            except OSError:
                free = None
            size = _dir_size(self.data_dir) if os.path.isdir(self.data_dir) else 0
            self._disk = (time.time(), free, size)
        return free, size

    def summary(self) -> dict:
        pts = list(self.hist)
        keys = ["cpu_sys", "cpu_proc", "cores", "mem_sys", "rss_mb", "rx_bps", "tx_bps", "feed_bps", "http_bps"]
        stats = {}
        for k in keys:
            vals = [p[k] for p in pts if p.get(k) is not None]
            stats[k] = {"now": vals[-1] if vals else None,
                        "avg": round(sum(vals) / len(vals), 3) if vals else None,
                        "max": max(vals) if vals else None}
        cur, first = self.cur_raw or {}, self.first or {}

        def delta(k):
            return max(cur.get(k, 0) - first.get(k, 0), 0) if k in cur and k in first else None
        free, size = self._disk_info()
        total, avail = cur.get("mem_total"), cur.get("mem_avail")
        used = total - avail if total and avail is not None else None
        return {"now": time.time(), "ncpu": NCPU, "window_s": len(pts) * 2,
                "series": {"t": [p["t"] for p in pts], **{k: [p.get(k) for p in pts] for k in keys}},
                "stats": stats,
                "mem": {"total_bytes": total, "used_bytes": used},
                "nic": {"rx_bytes": cur.get("rx"), "tx_bytes": cur.get("tx")},
                "totals_since_start": {"nic_rx_bytes": delta("rx"), "nic_tx_bytes": delta("tx"),
                                       "feed_bytes": delta("feed_bytes"), "http_out_bytes": delta("http_out")},
                "disk": {"free_bytes": free, "data_dir_bytes": size}}
