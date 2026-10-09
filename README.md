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
