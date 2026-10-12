#!/usr/bin/env python3
"""Manifold agent: run on each laptop. Registers with the hub and keeps your
specs fresh. Only shares CPU model, cores, RAM and GPU name/VRAM.

  python3 agent.py --hub http://HUB:8080 --name "Nemin's 4050"
  python3 agent.py --hub http://HUB:8080 --allow-jobs --max-cores 4
  python3 agent.py --hub https://HUB --invite TOKEN --allow-jobs   (join a friend's crew)

Without --allow-jobs your machine never runs anyone's work. With it, only your
connected friends can send built-in tasks (no arbitrary code), capped at --max-cores.
"""
import argparse
import json
import os
import platform
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from manifold import count_primes, get_specs

TASKS = {"count_primes": lambda a, procs: count_primes(int(a["lo"]), int(a["hi"]), procs)}


class HubError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def cpu_model():
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
            if out:
                return out.splitlines()[0]
        except Exception:
            pass
    return platform.processor() or "Unknown CPU"


def share_specs():
    s = get_specs()
    return {"cpu_model": cpu_model(), "cores": s["cores"], "ram_gb": s["ram_gb"] or 0,
            "gpus": [{"name": g["name"], "vram_mb": g["vram_mb"]} for g in s["gpus"]]}


def post(hub, path, body):
    req = urllib.request.Request(hub.rstrip("/") + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            msg = json.load(e).get("error", str(e))
        except Exception:
            msg = str(e)
        raise HubError(e.code, msg)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hub", required=True, help="hub address, e.g. http://1.2.3.4:8080")
    p.add_argument("--name", default=socket.gethostname())
    p.add_argument("--state", default="agent_state.json")
    p.add_argument("--invite", default="", help="invite token from a friend's link")
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

    def save_state():
        with open(a.state, "w") as f:
            json.dump(box["state"], f)
        try:
            os.chmod(a.state, 0o600)
        except Exception:
            pass

    def register(invite=""):
        body = {"name": a.name, "specs": share_specs()}
        if invite:
            body["invite"] = invite
        r = post(a.hub, "/api/register", body)
        box["state"] = {"code": r["code"], "secret": r["secret"], "host_name": r.get("joined") or ""}
        save_state()
        return r

    if not box["state"].get("secret"):
        try:
            register(a.invite)
        except HubError as e:
            sys.exit(f"Couldn't join: {e}")
        except Exception as e:
            sys.exit(f"Couldn't reach the hub at {a.hub}: {e}")
        if box["state"].get("host_name"):
            print(f"\nJoined {box['state']['host_name']}'s crew.")
    st = box["state"]

    sharing = a.allow_jobs

    print(f"\nYour pairing code: {st['code']}")
    print("Open this link in your browser to see your card (it's private to you, don't share it):")
    print(f"  {a.hub.rstrip('/')}/#key={st['secret']}\n")
    if sharing:
        print(f"Sharing ON: connected friends may use up to {cap} of your {cores} cores. Keep this window open.\n")
    else:
        print("Sharing OFF (run again with --allow-jobs to let friends use your machine).\n")

    def heartbeat_loop():
        while True:
            try:
                post(a.hub, "/api/heartbeat", {"secret": box["state"]["secret"], "specs": share_specs(),
                                               "sharing": sharing, "share_cores": cap if sharing else 0})
            except HubError as e:
                if e.code == 401:
                    try:
                        register()
                        print(f"Hub forgot us, new code: {box['state']['code']}")
                    except Exception as e2:
                        print("couldn't re-register:", e2)
            except Exception as e:
                print("hub unreachable, retrying:", e)
            time.sleep(10)

    threading.Thread(target=heartbeat_loop, daemon=True).start()

    while True:  # job loop runs in the main thread
        if not sharing:
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
                body = {"result": fn(job["args"], cap)}
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
