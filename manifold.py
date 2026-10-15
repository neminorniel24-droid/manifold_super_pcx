#!/usr/bin/env python3
"""Manifold: pool several laptops into one compute cluster.

  python3 manifold.py init                  # make workers.json + shared token (main machine)
  python3 manifold.py worker --token XXX    # run on every machine
  python3 manifold.py status                # see combined specs
  python3 manifold.py run --n 1000000       # demo: split a job across all machines
  python3 manifold.py llm --model m.gguf    # print commands to run a big LLM across GPUs

Only needs Python 3.8+. No pip installs.
"""
import argparse
import hmac
import json
import math
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import Pool, cpu_count

PORT = 8765
CONFIG = "workers.json"


# ---------------------------------------------------------------- specs

def get_gpus():
    if not shutil.which("nvidia-smi"):
        return []
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5).stdout
        gpus = []
        for line in out.strip().splitlines():
            name, total, used = [x.strip() for x in line.split(",")]
            gpus.append({"name": name, "vram_mb": int(total), "vram_used_mb": int(used)})
        return gpus
    except Exception:
        return []


def _other_ram():
    """RAM on Windows and macOS (Linux reads /proc/meminfo)."""
    try:
        if os.name == "nt":
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("m", ctypes.c_ulong), ("tp", ctypes.c_ulonglong),
                            ("ap", ctypes.c_ulonglong), ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
            s = MS()
            s.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            return round(s.tp / 2 ** 30, 1), round(s.ap / 2 ** 30, 1)
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout
        return round(int(out.strip()) / 2 ** 30, 1), None
    except Exception:
        return None, None


def get_specs():
    total = free = None
    try:
        with open("/proc/meminfo") as f:
            m = {l.split(":")[0]: int(l.split()[1]) for l in f}
        total = round(m["MemTotal"] / 1048576, 1)
        free = round(m.get("MemAvailable", 0) / 1048576, 1)
    except Exception:
        pass
    if total is None:
        total, free = _other_ram()
    if total is None and sys.platform == "win32":  # Windows has no /proc
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("l", ctypes.c_ulong), ("load", ctypes.c_ulong), ("tp", ctypes.c_ulonglong),
                            ("ap", ctypes.c_ulonglong), ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong),
                            ("tpf", ctypes.c_ulonglong), ("apf", ctypes.c_ulonglong), ("x", ctypes.c_ulonglong)]
            m = MS()
            m.l = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            total, free = round(m.tp / 2**30, 1), round(m.ap / 2**30, 1)
        except Exception:
            pass
    try:
        load = round(os.getloadavg()[0], 2)
    except Exception:
        load = None
    return {"host": socket.gethostname(), "os": platform.platform(),
            "cores": cpu_count(), "ram_gb": total, "ram_free_gb": free,
            "load": load, "gpus": get_gpus()}


# ---------------------------------------------------------------- demo task

def _is_prime(n):
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    for i in range(3, math.isqrt(n) + 1, 2):
        if n % i == 0:
            return False
    return True


def _count_chunk(r):
    lo, hi = r
    return sum(1 for n in range(lo, hi) if _is_prime(n))


def count_primes(lo, hi, procs=None):
    """Count primes in [lo, hi) using `procs` cores (default: all)."""
    procs = max(1, min(procs or cpu_count(), cpu_count()))
    t = time.time()
    step = 20000
    chunks = [(a, min(a + step, hi)) for a in range(lo, hi, step)]
    with Pool(procs) as p:
        total = sum(p.map(_count_chunk, chunks))
    return {"count": total, "seconds": round(time.time() - t, 2), "cores": procs}


# ---------------------------------------------------------------- worker

class Handler(BaseHTTPRequestHandler):
    token = ""

    def log_message(self, *a):
        pass

    def _authed(self):
        sent = self.headers.get("X-Token", "")
        return hmac.compare_digest(sent.encode(), self.token.encode())

    def _send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        if self.path == "/specs":
            return self._send(200, get_specs())
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._authed():
            return self._send(401, {"error": "bad token"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/run" and req.get("task") == "count_primes":
                a = req.get("args", {})
                return self._send(200, count_primes(int(a["lo"]), int(a["hi"])))
        except Exception as e:
            return self._send(400, {"error": str(e)})
        self._send(404, {"error": "unknown task"})


def cmd_worker(a):
    token = a.token or os.environ.get("MANIFOLD_TOKEN")
    if not token:
        sys.exit("Need --token (or MANIFOLD_TOKEN). Run 'init' on your main machine first.")
    Handler.token = token
    s = get_specs()
    print(f"Manifold worker on {a.host}:{a.port} | {s['cores']} cores, "
          f"{s['ram_gb']} GB RAM, {len(s['gpus'])} GPU(s)")
    print("Only expose this over Tailscale or a trusted LAN. Ctrl+C to stop.")
    try:
        ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


# ---------------------------------------------------------------- controller

def load_cfg():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except FileNotFoundError:
        sys.exit(f"No {CONFIG}. Run: python3 manifold.py init")


def call(host, path, token, body=None, timeout=600):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://{host}{path}", data=data,
                                 headers={"X-Token": token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_all(cfg):
    res = {}

    def one(h):
        try:
            res[h] = call(h, "/specs", cfg["token"], timeout=5)
        except Exception as e:
            res[h] = {"error": str(e)}

    threads = [threading.Thread(target=one, args=(h,)) for h in cfg["workers"]]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return {h: res[h] for h in cfg["workers"]}


def cmd_init(a):
    if os.path.exists(CONFIG) and not a.force:
        sys.exit(f"{CONFIG} already exists (use --force to overwrite)")
    token = secrets.token_urlsafe(16)
    with open(CONFIG, "w") as f:
        json.dump({"token": token, "workers": [f"127.0.0.1:{PORT}"]}, f, indent=2)
    print(f"Created {CONFIG}. Add each friend's Tailscale IP to the 'workers' list, like 100.64.1.2:{PORT}")
    print("\nOn EVERY machine (including this one), run:")
    print(f"  python3 manifold.py worker --token {token}")


def cmd_status(a):
    cfg = load_cfg()
    specs = fetch_all(cfg)
    cores = ram = vram = up = 0
    print(f"{'WORKER':<22}{'HOST':<16}{'CORES':>6}{'RAM GB':>8}{'LOAD':>7}  GPU")
    for h, s in specs.items():
        if "error" in s:
            print(f"{h:<22}OFFLINE ({s['error'][:40]})")
            continue
        up += 1
        cores += s["cores"]
        ram += s["ram_gb"] or 0
        g = ", ".join(f"{x['name']} {x['vram_mb'] // 1024}GB" for x in s["gpus"]) or "none"
        vram += sum(x["vram_mb"] for x in s["gpus"])
        print(f"{h:<22}{s['host'][:15]:<16}{s['cores']:>6}{s['ram_gb']:>8}{str(s['load']):>7}  {g}")
    print(f"\nPOOL: {up}/{len(specs)} online | {cores} cores | {ram:.1f} GB RAM | {vram / 1024:.1f} GB VRAM")


def cmd_run(a):
    cfg = load_cfg()
    specs = {h: s for h, s in fetch_all(cfg).items() if "error" not in s}
    if not specs:
        sys.exit("No workers reachable. Run 'status' to see why.")
    hosts = list(specs)
    total_cores = sum(specs[h]["cores"] for h in hosts)
    n, pos, plan = a.n, 0, []
    for i, h in enumerate(hosts):
        share = n - pos if i == len(hosts) - 1 else int(n * specs[h]["cores"] / total_cores)
        plan.append((h, pos, pos + share))
        pos += share

    results = {}

    def go(h, lo, hi):
        t = time.time()
        try:
            r = call(h, "/run", cfg["token"], {"task": "count_primes", "args": {"lo": lo, "hi": hi}})
            r["wall"] = round(time.time() - t, 2)
            results[h] = r
        except Exception as e:
            results[h] = {"error": str(e)}

    start = time.time()
    threads = [threading.Thread(target=go, args=p) for p in plan]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - start

    print(f"Counting primes below {n:,} across {len(hosts)} worker(s)\n")
    total = 0
    for h, lo, hi in plan:
        r = results[h]
        if "error" in r:
            print(f"  {h}: FAILED ({r['error']})")
        else:
            total += r["count"]
            print(f"  {h}: range {lo:,}-{hi:,} -> {r['count']:,} primes in {r['wall']}s ({r['cores']} cores)")
    failed = any("error" in r for r in results.values())
    print(f"\nTotal: {total:,} primes in {wall:.2f}s" + ("  (INCOMPLETE, a worker failed)" if failed else ""))

    if a.compare:
        t = time.time()
        solo = _count_chunk((0, n))
        solo_t = time.time() - t
        print(f"One core on this machine: {solo:,} primes in {solo_t:.2f}s -> pool was {solo_t / wall:.1f}x faster")


def cmd_llm(a):
    cfg = load_cfg()
    hosts = [h.split(":")[0] for h in cfg["workers"]]
    remote = [h for h in hosts if h not in ("127.0.0.1", "localhost")]
    if not remote:
        sys.exit("Add your friends' IPs to workers.json first.")
    rpc = ",".join(f"{h}:50052" for h in remote)
    print("1) On each friend's machine (needs llama.cpp built with -DGGML_CUDA=ON -DGGML_RPC=ON):")
    print("     rpc-server -H 0.0.0.0 -p 50052")
    print("   Keep this on Tailscale only. The RPC server has no password.\n")
    print("2) On this machine:")
    print(f"     llama-cli -m {a.model} --rpc {rpc} -ngl 99")


def main():
    p = argparse.ArgumentParser(prog="manifold", description="Pool laptops into one compute cluster")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create workers.json with a shared token")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("worker", help="run the worker agent on this machine")
    s.add_argument("--token")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=PORT)
    s.set_defaults(fn=cmd_worker)

    s = sub.add_parser("status", help="show every machine and the combined pool")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("run", help="demo: split a prime-counting job across the pool")
    s.add_argument("--n", type=int, default=1_000_000)
    s.add_argument("--compare", action="store_true", help="also time one core locally")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("llm", help="print llama.cpp commands to split a model across GPUs")
    s.add_argument("--model", default="model.gguf")
    s.set_defaults(fn=cmd_llm)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
