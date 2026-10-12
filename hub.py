#!/usr/bin/env python3
"""Manifold Hub: the website + API where laptops show up as profile cards.

  python3 hub.py --port 8080

Laptops run agent.py, which connects OUT to this hub (works on any network,
including mobile data). Friends connect with a short pairing code and the other
side must accept. Stdlib only.
"""
import argparse
import hmac
import json
import os
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(HERE, "hub_data.json")
ONLINE_SECS = 30
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I
LOCK = threading.Lock()
DB = {"machines": {}}  # code -> machine
JOBS = {}  # job id -> job (memory only)
ALLOWED_TASKS = {"count_primes"}  # built-in tasks only, never arbitrary code
MAX_RANGE = 50_000_000
PUBLIC_URL = ""
INVITES = {}  # token -> {"owner": code, "exp": ts}
INVITE_TTL = 48 * 3600
DL_FILES = {"manifold.py", "agent.py"}
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
HOST_RE = re.compile(r"^[A-Za-z0-9.-]+(:[0-9]{1,5})?$")

INSTALL_SH = '#!/usr/bin/env bash\n# Manifold installer. Saves two files to ~/manifold and starts sharing.\nset -e\nHUB="__HUB__"\nif ! command -v python3 >/dev/null 2>&1; then\n  echo "Python 3 is needed first:  sudo apt install -y python3"\n  exit 1\nfi\nmkdir -p "$HOME/manifold" && cd "$HOME/manifold"\ncurl -fsSL -H "ngrok-skip-browser-warning: 1" "$HUB/dl/manifold.py" -o manifold.py\ncurl -fsSL -H "ngrok-skip-browser-warning: 1" "$HUB/dl/agent.py" -o agent.py\necho "Starting Manifold. Keep this window open. Press Ctrl+C to stop sharing."\nexec python3 agent.py --hub "$HUB" __INVITE__--allow-jobs --name "$(hostname)"\n'

INSTALL_PS1 = '$ErrorActionPreference = "Stop"\n$hub = "__HUB__"\nif (-not (Get-Command python -ErrorAction SilentlyContinue)) {\n  Write-Host "Python 3 is needed first. Install it with:  winget install Python.Python.3.12"\n  Write-Host "Then open a new PowerShell window and run this command again."\n  exit 1\n}\n$dir = Join-Path $env:USERPROFILE "manifold"\nNew-Item -ItemType Directory -Force -Path $dir | Out-Null\n$h = @{ "ngrok-skip-browser-warning" = "1" }\nInvoke-WebRequest -UseBasicParsing -Headers $h -Uri "$hub/dl/manifold.py" -OutFile (Join-Path $dir "manifold.py")\nInvoke-WebRequest -UseBasicParsing -Headers $h -Uri "$hub/dl/agent.py" -OutFile (Join-Path $dir "agent.py")\nSet-Location $dir\nWrite-Host "Starting Manifold. Keep this window open. Press Ctrl+C to stop sharing."\npython agent.py --hub $hub __INVITE__--allow-jobs --name $env:COMPUTERNAME\n'

TIERS = [  # (min score, creature, emoji, title)
    (300, "dragon", "\U0001F409", "Dragon"),
    (180, "tiger", "\U0001F42F", "Tiger"),
    (100, "bull", "\U0001F402", "Bull"),
    (40, "wolf", "\U0001F43A", "Wolf"),
    (0, "hatchling", "\U0001F423", "Hatchling"),
]


def load():
    global DB
    try:
        with open(DB_FILE) as f:
            DB = json.load(f)
    except Exception:
        DB = {"machines": {}}


def save():
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(DB, f)
    os.replace(tmp, DB_FILE)


def clean_specs(s):
    """Never trust the agent: keep only expected fields, bounded."""
    s = s if isinstance(s, dict) else {}

    def num(v, hi):
        try:
            return max(0.0, min(float(v), hi))
        except Exception:
            return 0.0

    gpus = []
    for g in (s.get("gpus") or [])[:8]:
        if isinstance(g, dict):
            gpus.append({"name": str(g.get("name", "GPU"))[:60],
                         "vram_gb": round(num(g.get("vram_mb", 0), 1e7) / 1024, 1)})
    return {"cpu": str(s.get("cpu_model", "CPU"))[:80], "cores": int(num(s.get("cores"), 1024)),
            "ram_gb": round(num(s.get("ram_gb"), 4096), 1), "gpus": gpus}


def score(sp):
    vram = sum(g["vram_gb"] for g in sp["gpus"])
    return int(sp["cores"] * 3 + sp["ram_gb"] * 3 + vram * 20)


def tier(sc):
    for lo, key, emoji, title in TIERS:
        if sc >= lo:
            return {"creature": key, "emoji": emoji, "title": title}


def public(m):
    sp = m["specs"]
    sc = score(sp)
    return {"code": m["code"], "name": m["name"], "specs": sp, "score": sc,
            "tier": tier(sc), "online": time.time() - m["last_seen"] < ONLINE_SECS,
            "sharing": bool(m.get("sharing")), "share_cores": int(m.get("share_cores", 0))}


def new_code():
    while True:
        c = "".join(secrets.choice(ALPHABET) for _ in range(6))
        c = c[:3] + "-" + c[3:]
        if c not in DB["machines"]:
            return c


def by_secret(sec):
    for m in DB["machines"].values():
        if hmac.compare_digest(m["secret"].encode(), str(sec).encode()):
            return m
    return None


def crew_view(me):
    ms = DB["machines"]
    friends = [public(ms[c]) for c in me["friends"] if c in ms]
    pool = {"machines": 0, "cores": 0, "ram_gb": 0.0, "vram_gb": 0.0}
    for p in [public(me)] + friends:
        if p["online"]:
            pool["machines"] += 1
            pool["cores"] += p["specs"]["cores"]
            pool["ram_gb"] += p["specs"]["ram_gb"]
            pool["vram_gb"] += sum(g["vram_gb"] for g in p["specs"]["gpus"])
    pool = {k: round(v, 1) for k, v in pool.items()}
    incoming = [{"code": c, "name": ms[c]["name"]} for c in me["pending"] if c in ms]
    return {"me": public(me), "friends": friends, "incoming": incoming, "pool": pool,
            "public_url": PUBLIC_URL}


def api(path, b):
    with LOCK:
        if path == "/api/register":
            code = new_code()
            m = {"code": code, "secret": secrets.token_urlsafe(24),
                 "name": str(b.get("name", "My laptop"))[:30] or "My laptop",
                 "specs": clean_specs(b.get("specs")), "last_seen": time.time(),
                 "friends": [], "pending": []}
            DB["machines"][code] = m
            joined = None
            tok = str(b.get("invite", ""))
            inv = INVITES.get(tok)
            if inv and inv["exp"] > time.time():
                owner = DB["machines"].get(inv["owner"])
                if owner:
                    owner["friends"].append(code)
                    m["friends"].append(owner["code"])
                    joined = owner["name"]
                del INVITES[tok]
            save()
            return 200, {"code": code, "secret": m["secret"], "joined": joined}

        me = by_secret(b.get("secret", ""))
        if not me:
            return 401, {"error": "unknown machine, register again"}

        if path == "/api/heartbeat":
            me["specs"] = clean_specs(b.get("specs"))
            me["last_seen"] = time.time()
            me["sharing"] = bool(b.get("sharing"))
            try:
                sc = int(b.get("share_cores") or 0)
            except Exception:
                sc = 0
            me["share_cores"] = max(0, min(sc, me["specs"]["cores"]))
            return 200, {"ok": True}
        if path == "/api/crew":
            return 200, crew_view(me)
        if path == "/api/connect":
            code = str(b.get("code", "")).strip().upper()
            target = DB["machines"].get(code)
            if not target or target is me:
                return 404, {"error": "no machine with that code"}
            if me["code"] in target["friends"]:
                return 200, {"status": "already connected"}
            if code in me["pending"]:  # they already asked me: connect both
                me["pending"].remove(code)
                me["friends"].append(code)
                target["friends"].append(me["code"])
                save()
                return 200, {"status": "connected"}
            if me["code"] not in target["pending"]:
                target["pending"].append(me["code"])
                save()
            return 200, {"status": "request sent, waiting for them to accept"}
        if path == "/api/respond":
            code = str(b.get("code", "")).strip().upper()
            if code not in me["pending"]:
                return 404, {"error": "no such request"}
            me["pending"].remove(code)
            if b.get("accept") and code in DB["machines"]:
                me["friends"].append(code)
                DB["machines"][code]["friends"].append(me["code"])
            save()
            return 200, {"ok": True}
        if path == "/api/invite":
            now = time.time()
            for k in [k for k, v in INVITES.items() if v["exp"] < now]:
                del INVITES[k]
            if sum(1 for v in INVITES.values() if v["owner"] == me["code"]) >= 10:
                return 429, {"error": "too many open invites; use one or wait for them to expire"}
            tok = secrets.token_urlsafe(9)
            INVITES[tok] = {"owner": me["code"], "exp": now + INVITE_TTL}
            return 200, {"token": tok, "public_url": PUBLIC_URL}
        if path == "/api/job/submit":
            target = DB["machines"].get(str(b.get("to", "")).strip().upper())
            if not target or (target is not me and target["code"] not in me["friends"]):
                return 403, {"error": "you can only send jobs to connected friends"}
            if not target.get("sharing") or time.time() - target["last_seen"] >= ONLINE_SECS:
                return 409, {"error": "that machine is offline or not sharing"}
            if b.get("task") not in ALLOWED_TASKS:
                return 400, {"error": "unknown task"}
            try:
                lo, hi = int(b["args"]["lo"]), int(b["args"]["hi"])
            except Exception:
                return 400, {"error": "bad args"}
            if not (0 <= lo < hi <= 10**9 and hi - lo <= MAX_RANGE):
                return 400, {"error": "range too large"}
            now = time.time()
            for k in [k for k, j in JOBS.items() if now - j["created"] > 3600]:
                del JOBS[k]
            if sum(1 for j in JOBS.values() if j["to"] == target["code"]
                   and j["status"] in ("queued", "running")) >= 20:
                return 429, {"error": "that machine has too many queued jobs"}
            jid = secrets.token_hex(8)
            JOBS[jid] = {"id": jid, "from": me["code"], "to": target["code"], "task": b["task"],
                         "args": {"lo": lo, "hi": hi}, "status": "queued", "result": None,
                         "created": now}
            return 200, {"job": jid}
        if path == "/api/job/poll":
            if not me.get("sharing"):
                return 200, {"job": None}
            for j in JOBS.values():
                if j["to"] == me["code"] and j["status"] == "queued":
                    j["status"] = "running"
                    return 200, {"job": {"id": j["id"], "task": j["task"], "args": j["args"]}}
            return 200, {"job": None}
        if path == "/api/job/result":
            j = JOBS.get(str(b.get("job", "")))
            if not j or j["to"] != me["code"] or j["status"] != "running":
                return 404, {"error": "no such running job"}
            try:
                if b.get("error"):
                    j["status"], j["result"] = "error", str(b["error"])[:200]
                else:
                    r = b["result"]
                    j["result"] = {"count": int(r["count"]), "seconds": float(r["seconds"]),
                                   "cores": int(r["cores"])}
                    j["status"] = "done"
            except Exception:
                j["status"], j["result"] = "error", "bad result"
            return 200, {"ok": True}
        if path == "/api/job/status":
            j = JOBS.get(str(b.get("job", "")))
            if not j or j["from"] != me["code"]:
                return 404, {"error": "no such job"}
            if j["status"] in ("queued", "running"):
                t = DB["machines"].get(j["to"])
                if not t or time.time() - t["last_seen"] >= ONLINE_SECS:
                    j["status"] = "lost"
            return 200, {"status": j["status"], "result": j["result"], "to": j["to"]}
        if path == "/api/disconnect":
            code = str(b.get("code", "")).strip().upper()
            if code in me["friends"]:
                me["friends"].remove(code)
            other = DB["machines"].get(code)
            if other and me["code"] in other["friends"]:
                other["friends"].remove(me["code"])
            save()
            return 200, {"ok": True}
    return 404, {"error": "not found"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, name, folder="web"):
        try:
            with open(os.path.join(HERE, folder, name) if folder else os.path.join(HERE, name), "rb") as f:
                data = f.read()
        except OSError:
            return self._send(404, {"error": "not found"})
        ctype = "text/html; charset=utf-8" if name.endswith(".html") else "text/plain; charset=utf-8"
        self._send(200, data, ctype)

    def _base(self):
        if PUBLIC_URL:
            return PUBLIC_URL
        host = self.headers.get("Host", "")
        if not HOST_RE.match(host):
            return None
        proto = self.headers.get("X-Forwarded-Proto", "http")
        return f"{proto if proto in ('http', 'https') else 'http'}://{host}"

    def _install(self, path):
        tok = (parse_qs(urlparse(self.path).query).get("token") or [""])[0]
        base = self._base()
        if (tok and not TOKEN_RE.match(tok)) or not base:
            return self._send(400, b"bad request", "text/plain")
        script = INSTALL_SH if path.endswith(".sh") else INSTALL_PS1
        script = script.replace("__HUB__", base).replace("__INVITE__", f"--invite {tok} " if tok else "")
        self._send(200, script.encode(), "text/plain; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            return self._file("index.html")
        if path.startswith("/join/"):
            return self._file("join.html")
        if path.startswith("/api/invite/"):
            tok = path[len("/api/invite/"):]
            owner = None
            with LOCK:
                inv = INVITES.get(tok) if TOKEN_RE.match(tok) else None
                if inv and inv["exp"] > time.time():
                    owner = DB["machines"].get(inv["owner"])
            return self._send(200, {"valid": bool(owner), "owner": owner["name"] if owner else None})
        if path.startswith("/dl/") and path[4:] in DL_FILES:
            return self._file(path[4:], folder="")
        if path in ("/install.sh", "/install.ps1"):
            return self._install(path)
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 20000:
                return self._send(413, {"error": "too big"})
            b = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(b, dict):
                raise ValueError("bad body")
        except Exception:
            return self._send(400, {"error": "bad request"})
        code, out = api(self.path, b)
        self._send(code, out)


def set_public(url):
    global PUBLIC_URL
    PUBLIC_URL = url.rstrip("/")


def start_tunnel(port):
    """Free Cloudflare quick tunnel: gives the hub an internet address, no account needed."""
    import atexit
    import platform
    import shutil
    import subprocess
    import urllib.request
    home = os.path.join(os.path.expanduser("~"), ".manifold")
    os.makedirs(home, exist_ok=True)
    local = os.path.join(home, "cloudflared")
    exe = shutil.which("cloudflared") or (local if os.path.exists(local) else None)
    if not exe:
        arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
        if platform.system() != "Linux" or not arch:
            print("Can't set up the tunnel automatically on this system.")
            print("Install cloudflared or ngrok yourself, then start the hub with --public-url https://YOUR-ADDRESS")
            return
        print("Downloading the free Cloudflare tunnel tool (one time, about 40 MB)...")
        try:
            urllib.request.urlretrieve(
                f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}", local)
            os.chmod(local, 0o755)
            exe = local
        except Exception as e:
            print("Couldn't download it:", e)
            print("Use ngrok instead and start the hub with --public-url https://YOUR-ADDRESS")
            return
    proc = subprocess.Popen([exe, "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    atexit.register(proc.terminate)

    def watch():
        for line in proc.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not PUBLIC_URL:
                set_public(m.group(0))
                print(f"\nPublic address: {PUBLIC_URL}")
                print("Open your page, click 'Create invite link' and send it to a friend.\n")
    threading.Thread(target=watch, daemon=True).start()
    print("Starting the internet tunnel, the address shows up in a few seconds...")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--public", action="store_true", help="open a free internet tunnel so friends can join")
    p.add_argument("--public-url", default="", help="your own public address (ngrok etc.)")
    a = p.parse_args()
    load()
    if a.public_url:
        set_public(a.public_url)
    elif a.public:
        start_tunnel(a.port)
    print(f"Manifold Hub on http://{a.host}:{a.port}  (open it in your browser)")
    try:
        ThreadingHTTPServer((a.host, a.port), H).serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
