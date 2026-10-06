#!/usr/bin/env bash
# Public HTTPS address for the API, so the Vercel site (HTTPS) can call it.
# The server only makes an OUTBOUND connection to Cloudflare — no open ports,
# no root, works behind the university firewall.
#
# Quick tunnel (default): free, no account, but the URL changes on every restart.
# Named tunnel: set CF_TUNNEL_TOKEN in config.env for a fixed hostname
# (Cloudflare dashboard → Zero Trust → Networks → Tunnels → create → copy token).
set -euo pipefail
cd "$(dirname "$0")"
set -a; [ -f config.env ] && . ./config.env; set +a
PORT="${PORT:-8800}"
mkdir -p bin data/logs

if [ ! -x bin/cloudflared ]; then
  echo "== downloading cloudflared"
  curl -fsSL -o bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
  chmod +x bin/cloudflared
fi

if [ -n "${CF_TUNNEL_TOKEN:-}" ]; then
  nohup bin/cloudflared tunnel --no-autoupdate run --token "$CF_TUNNEL_TOKEN" > data/logs/tunnel.log 2>&1 &
  echo $! > data/tunnel.pid
  echo "Named tunnel started — use your fixed hostname as VITE_GPU_API"
else
  nohup bin/cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" > data/logs/tunnel.log 2>&1 &
  echo $! > data/tunnel.pid
  for i in $(seq 1 30); do
    url=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' data/logs/tunnel.log | head -1 || true)
    [ -n "$url" ] && break; sleep 1
  done
  echo "$url" > data/public_url.txt
  echo "Public URL: $url"
  # Tell the webapp where we are now (R2 → data backend /api/gpu-server); no redeploy needed.
  [ -n "$url" ] && .venv/bin/python publish_url.py "$url" || true
fi
