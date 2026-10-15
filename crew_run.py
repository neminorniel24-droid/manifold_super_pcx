#!/usr/bin/env python3
"""Split a job across your crew (yourself + connected friends who are sharing).

  python3 crew_run.py --hub http://HUB:8080 --n 5000000

Uses the private key saved by agent.py (agent_state.json). If a machine drops
mid-job, its chunk is re-sent to another machine.
"""
import argparse
import json
import time
import urllib.error
import urllib.request


def post(hub, path, body):
    req = urllib.request.Request(hub.rstrip("/") + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.load(e).get("error", str(e))}
        except Exception:
            return {"error": str(e)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hub", required=True)
    p.add_argument("--state", default="agent_state.json")
    p.add_argument("--n", type=int, default=2_000_000, help="count primes below this number")
    p.add_argument("--chunks", type=int, default=0, help="pieces to split into (default: 4 per machine)")
    a = p.parse_args()

    secret = json.load(open(a.state))["secret"]
    crew = post(a.hub, "/api/crew", {"secret": secret})
    if "error" in crew:
        raise SystemExit("hub says: " + crew["error"])
    workers = [m for m in [crew["me"]] + crew["friends"] if m["online"] and m["sharing"] and m.get("can_run", True)]
    if not workers:
        raise SystemExit("Nobody is sharing. Start agents with --allow-jobs.")
    print("Workers:", ", ".join(f"{w['name']} ({w['share_cores']} cores)" for w in workers))

    nchunks = a.chunks or len(workers) * 4
    step = -(-a.n // nchunks)
    queue = [(lo, min(lo + step, a.n)) for lo in range(0, a.n, step)]
    alive = {w["code"]: w for w in workers}
    names = {w["code"]: w["name"] for w in workers}
    pending, done = {}, []  # job id -> (chunk, worker code)
    attempts = {}
    t0 = time.time()

    def feed():
        busy = {}
        for _, wc in pending.values():
            busy[wc] = busy.get(wc, 0) + 1
        while queue and alive:
            # give next chunk to the least-loaded worker, relative to its core share
            wc = min(alive, key=lambda c: busy.get(c, 0) / max(1, alive[c]["share_cores"]))
            if busy.get(wc, 0) >= 2:
                break
            lo, hi = queue.pop(0)
            r = post(a.hub, "/api/job/submit", {"secret": secret, "to": wc, "task": "count_primes",
                                                 "args": {"lo": lo, "hi": hi}})
            if "job" in r:
                pending[r["job"]] = ((lo, hi), wc)
                busy[wc] = busy.get(wc, 0) + 1
            else:
                print(f"  can't use {alive[wc]['name']}: {r.get('error')}")
                del alive[wc]
                queue.insert(0, (lo, hi))

    feed()
    while pending or queue:
        if not alive and (queue or pending) and not pending:
            raise SystemExit("All workers dropped. Job incomplete.")
        time.sleep(0.5)
        batch = post(a.hub, "/api/job/statuses", {"secret": secret, "jobs": list(pending)}).get("jobs", {}) if pending else {}
        for jid in list(pending):
            chunk, wc = pending[jid]
            r = batch.get(jid, {"error": "job vanished"})
            st = r.get("status")
            if st == "done":
                done.append((chunk, wc, r["result"]))
                del pending[jid]
            elif st in ("lost", "error") or "error" in r:
                name = names.get(wc, wc)
                print(f"  {name} failed on {chunk[0]:,}-{chunk[1]:,} ({st}); re-sending to someone else")
                alive.pop(wc, None)
                del pending[jid]
                attempts[chunk] = attempts.get(chunk, 0) + 1
                if attempts[chunk] > 3:
                    raise SystemExit("Chunk failed too many times.")
                queue.insert(0, chunk)
        feed()
    wall = time.time() - t0

    per = {}
    for chunk, wc, res in done:
        d = per.setdefault(wc, [0, 0, 0.0])
        d[0] += 1
        d[1] += res["count"]
        d[2] += res["seconds"]
    print(f"\nPrimes below {a.n:,}:")
    for wc, (c, cnt, sec) in per.items():
        print(f"  {names[wc]}: {c} chunk(s), {cnt:,} primes, {sec:.1f}s of compute")
    total = sum(r["count"] for _, _, r in done)
    print(f"\nTotal: {total:,} primes in {wall:.1f}s across {len(per)} machine(s)")


if __name__ == "__main__":
    main()
