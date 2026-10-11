#!/usr/bin/env python3
"""Manifold agent: run on each laptop. Registers with the hub and keeps your
specs fresh. Only shares CPU model, cores, RAM and GPU name/VRAM.

  python3 agent.py --hub http://HUB:8080 --name "Nemin's 4050"
  python3 agent.py --hub http://HUB:8080 --allow-jobs --max-cores 4

Without --allow-jobs your machine never runs anyone's work. With it, only your
connected friends can send built-in tasks (no arbitrary code), capped at --max-cores.
"""
import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.request

from manifold import count_primes, get_specs

TASKS = {"count_primes": lambda a, procs: count_primes(int(a["lo"]), int(a["hi"]), procs)}


def cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return "Unknown CPU"


def share_specs():
    s = get_specs()
    return {"cpu_model": cpu_model(), "cores": s["cores"], "ram_gb": s["ram_gb"] or 0,
            "gpus": [{"name": g["name"], "vram_mb": g["vram_mb"]} for g in s["gpus"]]}


def post(hub, path, body):
    req = urllib.request.Request(hub.rstrip("/") + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hub", required=True, help="hub address, e.g. http://1.2.3.4:8080")
    p.add_argument("--name", default=os.uname().nodename)
    p.add_argument("--state", default="agent_state.json")
    p.add_argument("--allow-jobs", action="store_true",
                   help="let connected friends run built-in tasks on this machine")
    p.add_argument("--max-cores", type=int, default=0,
                   help="most cores friends may use (default: half of this machine)")
    a = p.parse_args()

    cores = share_specs()["cores"]
    cap = max(1, min(a.max_cores or max(1, cores // 2), cores))
    box = {"state": {}}
    try:
        with open(a.state) as f:
            box["state"] = json.load(f)
    except Exception:
        pass

    def register():
        r = post(a.hub, "/api/register", {"name": a.name, "specs": share_specs()})
        with open(a.state, "w") as f:
            json.dump(r, f)
        os.chmod(a.state, 0o600)
        box["state"] = r
        return r

    if not box["state"].get("secret"):
        register()
    st = box["state"]
    print(f"\nYour pairing code: {st['code']}")
    print("Share the code with a friend. Open this link in your browser to see your card")
    print("(it's private to you, don't share it):")
    print(f"  {a.hub.rstrip('/')}/#key={st['secret']}\n")
    if a.allow_jobs:
        print(f"Sharing ON: connected friends may use up to {cap} of your {cores} cores.\n")
    else:
        print("Sharing OFF (add --allow-jobs to let friends use your machine).\n")

    def heartbeat_loop():
        while True:
            try:
                post(a.hub, "/api/heartbeat", {"secret": box["state"]["secret"], "specs": share_specs(),
                                               "sharing": a.allow_jobs, "share_cores": cap if a.allow_jobs else 0})
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    register()
                    print(f"Hub forgot us, new code: {box['state']['code']}")
            except Exception as e:
                print("hub unreachable, retrying:", e)
            time.sleep(10)

    threading.Thread(target=heartbeat_loop, daemon=True).start()

    while True:  # job loop runs in the main thread
        if not a.allow_jobs:
            time.sleep(2)
            continue
        try:
            job = post(a.hub, "/api/job/poll", {"secret": box["state"]["secret"]})["job"]
            if not job:
                time.sleep(0.5)
                continue
            fn = TASKS.get(job["task"])
            if not fn:
                post(a.hub, "/api/job/result", {"secret": box["state"]["secret"], "job": job["id"],
                                                "error": "unsupported task"})
                continue
            print(f"job {job['id']}: {job['task']} {job['args']} on {cap} core(s)")
            try:
                res = fn(job["args"], cap)
                body = {"result": res}
            except Exception as e:
                body = {"error": str(e)}
            post(a.hub, "/api/job/result", {"secret": box["state"]["secret"], "job": job["id"], **body})
            print("  done")
        except Exception as e:
            print("job loop error:", e)
            time.sleep(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nstopped")
