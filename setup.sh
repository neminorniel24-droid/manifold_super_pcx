#!/usr/bin/env bash
# Checks that this WSL machine is ready to be a Manifold worker.
ok()   { echo "  [ok]   $1"; }
warn() { echo "  [fix]  $1"; }

echo "Manifold setup check"
command -v python3 >/dev/null && ok "python3 $(python3 -V 2>&1 | cut -d' ' -f2)" || warn "install python3: sudo apt install -y python3"
command -v nvidia-smi >/dev/null && ok "GPU visible: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)" || warn "no GPU in WSL: install the NVIDIA driver on Windows"
if command -v tailscale >/dev/null; then
  ip=$(tailscale ip -4 2>/dev/null | head -1)
  [ -n "$ip" ] && ok "Tailscale address: $ip" || warn "run: sudo tailscale up"
else
  warn "install Tailscale: curl -fsSL https://tailscale.com/install.sh | sh"
fi
echo
echo "Then start the worker:  python3 manifold.py worker --token <token>"
