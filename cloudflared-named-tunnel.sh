#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Turnkey NAMED Cloudflare Tunnel for the Zyvora terminal.
#
# Gives a STABLE, branded HTTPS URL (e.g. terminal.yourdomain.com) that
# survives Mac reboots — unlike the random *.trycloudflare.com quick tunnel.
#
# ── DO THESE ONCE, IN YOUR BROWSER (they need YOUR accounts — cannot be
#    automated for you):
#   1. Free Cloudflare account:        https://dash.cloudflare.com/sign-up
#   2. Add your domain to Cloudflare   (Websites → Add a site → Free plan).
#      Cloudflare shows you TWO nameservers.
#   3. At GoDaddy: Domain → Nameservers → "I'll use my own" → paste the two
#      Cloudflare nameservers. (Keeps GoDaddy as registrar; reversible;
#      propagation ~30 min–a few hours. Cloudflare emails you when active.)
#   4. Authorize THIS Mac:             cloudflared tunnel login
#      (opens a browser; pick your domain)
#
# ── THEN run this script with your chosen hostname:
#      bash cloudflared-named-tunnel.sh terminal.yourdomain.com
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HOST="${1:?usage: bash cloudflared-named-tunnel.sh terminal.yourdomain.com}"
TUNNEL="zyvora-terminal"
LOCAL="http://localhost:8001"
CFG="$HOME/.cloudflared"

command -v cloudflared >/dev/null || { echo "cloudflared not installed (brew install cloudflared)"; exit 1; }
[ -f "$CFG/cert.pem" ] || { echo "Run 'cloudflared tunnel login' first (step 4 above)."; exit 1; }

echo "▸ Creating tunnel '$TUNNEL' (reused if it already exists)…"
cloudflared tunnel create "$TUNNEL" 2>/dev/null || true
TUNNEL_ID="$(cloudflared tunnel list | awk -v n="$TUNNEL" '$2==n{print $1; exit}')"
[ -n "$TUNNEL_ID" ] || { echo "Could not resolve tunnel id"; exit 1; }

echo "▸ Routing DNS: $HOST → tunnel $TUNNEL_ID …"
cloudflared tunnel route dns "$TUNNEL" "$HOST"

echo "▸ Writing $CFG/config.yml …"
cat > "$CFG/config.yml" <<EOF
tunnel: $TUNNEL_ID
credentials-file: $CFG/$TUNNEL_ID.json
ingress:
  - hostname: $HOST
    service: $LOCAL
  - service: http_status:404
EOF

echo "▸ Installing as a background service (auto-starts on boot)…"
sudo cloudflared service install 2>/dev/null || cloudflared service install 2>/dev/null || \
  echo "  (service install needs sudo — or run manually: cloudflared tunnel run $TUNNEL)"

echo ""
echo "✓ Done. Your terminal will be live at:  https://$HOST"
echo "  (allow a minute for the cert; if DNS was just changed, up to a few hours)"
