# Manifold

Pool your laptops (and your friends' laptops) into one compute cluster.
Each machine runs a small worker. One main machine sees everyone's specs
and sends out jobs.

**Honest expectations:** this does not make one PC "faster" for normal apps or games.
It speeds up jobs that can be split into chunks (hash cracking, rendering, test runs,
data jobs), and it lets you pool GPU memory to run bigger AI models than one laptop can hold.
Three 6 GB GPUs = 18 GB of VRAM for models, but the network is slower than one big GPU.

## Quick start (WSL2 on each machine)

1. Install the NVIDIA driver on **Windows** (not inside WSL). `nvidia-smi` then works in WSL.
2. Get everyone on one private network with **Tailscale** (free), installed *inside WSL*:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up
   tailscale ip -4        # this is the machine's address, like 100.x.y.z
   ```
   If `tailscale up` complains about the daemon, enable systemd: add
   `[boot]` / `systemd=true` to `/etc/wsl.conf`, then run `wsl --shutdown` from Windows and reopen.
   (Plain WSL2 sits behind NAT, so other machines can't reach it without Tailscale.)
3. On your main machine:
   ```bash
   python3 manifold.py init
   ```
   Edit `workers.json` and add each friend's Tailscale IP, e.g. `"100.64.1.2:8765"`.
4. On **every** machine (including yours), start the worker with the token from `init`:
   ```bash
   python3 manifold.py worker --token <token>
   ```
5. On your main machine:
   ```bash
   python3 manifold.py status            # combined cores / RAM / VRAM
   python3 manifold.py run --n 2000000 --compare   # demo job split across everyone
   python3 manifold.py llm --model mymodel.gguf    # commands to split an LLM over all GPUs
   ```

## Security

- Workers only accept requests with the shared token. Don't share `workers.json` (it's in `.gitignore`).
- Only expose workers over Tailscale or a trusted LAN, never the open internet.
- Workers only run built-in tasks. They don't execute arbitrary commands.

## Roadmap

- [ ] More built-in tasks (hash cracking for CTF labs, file/data processing)
- [ ] Task queue with retries when a worker drops
- [ ] Auto-discovery so you don't edit `workers.json`
- [ ] Live dashboard
- [ ] Ray / llama.cpp RPC auto-launch

## v0.2: Crew website (profile cards + pairing codes)

Each laptop shows up as a card with a creature based on its power score
(CPU cores x3 + RAM GB x3 + GPU VRAM GB x20): Hatchling, Wolf, Bull, Tiger, Dragon.
Laptops connect OUT to the hub, so it works on any network, including mobile data.

1. Run the hub somewhere everyone can reach (your PC for testing, a cheap VPS later):
   `python3 hub.py --port 8080`
2. On each laptop: `python3 agent.py --hub http://HUB_ADDRESS:8080 --name "My laptop"`
   It prints your pairing code and a private link. Open the link to see your card.
3. Type a friend's code on the website. They get a request and must accept.

Only CPU model, cores, RAM and GPU name/VRAM are shared. Never share the private link.

## v0.3: Send work to friends' laptops

Sharing is opt-in. A laptop only runs work if its owner starts the agent with `--allow-jobs`.
Only connected friends can send jobs, only built-in tasks run (never arbitrary code),
and the owner caps how many cores friends can use.

```bash
# on each machine that wants to share power
python3 agent.py --hub http://HUB:8080 --name "My laptop" --allow-jobs --max-cores 4

# on the machine that wants the combined power
python3 crew_run.py --hub http://HUB:8080 --n 5000000
```

`crew_run.py` splits the job across you and your online, sharing friends. If a machine drops
mid-job, its pieces are re-sent to someone else.

## Easy mode (recommended)

**You (the host):** one command. It starts the hub, opens a free public address, joins your own
laptop to the crew and opens your page.

```bash
python3 host.py
```

On your page, click **Create invite link** and send the link to a friend (it works once, for 48 hours).

**Your friend:** opens your link, copies the one command shown for their system (Windows, or Linux/WSL)
and pastes it. It saves two small files to a `manifold` folder, joins your crew automatically and
starts sharing. Closing the window or pressing Ctrl+C stops sharing at once.

**Running work:** click **Run power test** on your page. It splits a real job across everyone who is
sharing and times your machine alone for comparison. No terminal needed.

Notes: invite links are kept in memory, so restarting the hub clears unused ones. The Windows
install path has not been tested on a real Windows machine yet.

## Bigger crews

There is no fixed limit on how many machines can join. Tested with 40 online laptops plus 5 that dropped out.

- **One link, many people.** When you create an invite link, pick how many people can use it (1, 5, 10 or 25). It expires after 48 hours, and "Cancel all open invite links" ends every open link at once. Invites are saved, so restarting the hub keeps them.
- **Only the host can use the crew's power.** People who join through your link can't send work to you or to each other. Machines paired by code can still send work both ways.
- **Quiet when idle.** Agents wait on the hub for work instead of asking twice a second, which cuts idle traffic to a few requests per second even with dozens of machines.
- **Slow machines don't hold the job up.** When all pieces are handed out, idle machines race the slowest pieces and the first answer wins.
- **Page for many machines.** Cards sit in a grid, online machines come first, offline ones can be hidden, and a Remove button drops a machine from your crew.

Old copies of `agent.py` keep working with the new hub, but they ask for work twice a second. Re-run the join command to update them.

## Console (workflows)

Click **Console** in the page header, or press the backtick key (`` ` ``), to open a terminal
at the bottom of the page. It talks to your crew with short commands:

```
status [--all]                  the crew and its combined power
run primes <n> [--compare]      split a job across the crew (500k, 5m, 1e6; max 200m)
     [--on <machine>]           one machine only (name, pairing code, or me)
ping                            round trips to the hub
invite [people]                 a join link for 1 to 25 people
remove <machine> [--yes]        drop a machine from your crew
workflow list | save <name> "<commands>" | run <name> | rm <name>
```

Chain commands with `;`. A workflow is just a saved chain, kept in your browser:

```
workflow save nightly "status; run primes 20m --compare"
workflow run nightly
```

Keys: Up/Down history, Tab completes commands and machine names, Ctrl+C cancels a running job,
Ctrl+L clears. Cancelling stops pieces nobody has started; a piece a machine is already
crunching finishes first. Update the hub (`hub.py`) as well as the page, since cancel needs a new endpoint.
