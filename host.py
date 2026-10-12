#!/usr/bin/env python3
"""Host Manifold with one command: the hub, a public address, and your own sharing agent.

  python3 host.py

It opens your page. Click "Create invite link" there and send the link to a friend.
Already have a tunnel (ngrok, etc.)?  python3 host.py --public-url https://xxxx.ngrok-free.dev
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
import webbrowser
from http.server import ThreadingHTTPServer

import hub

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(os.path.expanduser("~"), ".manifold")


def open_browser(url):
    try:
        if "microsoft" in platform.release().lower():  # WSL: use the Windows browser
            subprocess.Popen(["cmd.exe", "/c", "start", "", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            webbrowser.open(url)
    except Exception:
        pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--name", default=socket.gethostname(), help="name shown on your card")
    p.add_argument("--max-cores", type=int, default=0, help="cores friends may use on THIS machine (default: half)")
    p.add_argument("--public-url", default="", help="use your own tunnel address instead of opening one")
    p.add_argument("--no-tunnel", action="store_true", help="local only; friends can't join from outside")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--state", default=os.path.join(HERE, "agent_state.json"))
    a = p.parse_args()

    hub.load()
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", a.port), hub.H)
    except OSError:
        sys.exit(f"Port {a.port} is busy. Stop the old hub (pkill -f hub.py) or use --port 8090.")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    local = f"http://127.0.0.1:{a.port}"

    if a.public_url:
        hub.set_public(a.public_url)
    elif not a.no_tunnel:
        hub.start_tunnel(a.port)  # fills in hub.PUBLIC_URL when the address is ready
        for _ in range(60):
            if hub.PUBLIC_URL:
                break
            time.sleep(0.5)

    os.makedirs(LOG_DIR, exist_ok=True)
    cmd = [sys.executable, os.path.join(HERE, "agent.py"), "--hub", local, "--name", a.name,
           "--allow-jobs", "--state", a.state]
    if a.max_cores:
        cmd += ["--max-cores", str(a.max_cores)]
    agent = subprocess.Popen(cmd, stdout=open(os.path.join(LOG_DIR, "agent.log"), "w"), stderr=subprocess.STDOUT)

    state = {}
    for _ in range(60):
        try:
            state = json.load(open(a.state))
            if state.get("secret"):
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not state.get("secret"):
        agent.terminate()
        sys.exit(f"Your own agent didn't start. See {os.path.join(LOG_DIR, 'agent.log')}")

    link = f"{local}/#key={state['secret']}"
    print("\nManifold is live.\n")
    print(f"  Your page:  {link}\n")
    if hub.PUBLIC_URL:
        print(f"  Public address: {hub.PUBLIC_URL}")
        print("  To add a friend: open your page, click 'Create invite link', and send them the link.\n")
    else:
        print("  No public address yet, so friends can't join from outside.")
        print(f"  Fix: run 'ngrok http {a.port}' in another window, then start this again with --public-url https://THE-ADDRESS\n")
    print("Keep this window open while your crew is working. Ctrl+C stops everything.\n")
    if not a.no_browser:
        open_browser(link)

    try:
        while agent.poll() is None:
            time.sleep(1)
        print("Your agent stopped. See", os.path.join(LOG_DIR, "agent.log"))
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        agent.terminate()  # the tunnel process is cleaned up by hub.start_tunnel's atexit hook


if __name__ == "__main__":
    main()
