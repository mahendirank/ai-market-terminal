#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# Zyvora Terminal — one-command production deploy
#
# Run on a fresh Ubuntu VPS (Hostinger, DigitalOcean, AWS Lightsail, etc.)
# Works from Hostinger's Browser Console — no SSH needed.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mahendirank/ai-market-terminal/main/bootstrap.sh | bash
#
# Note (2026-05-26): the per-deploy flow has moved to deploy.sh, which
# pulls a pre-built image from GHCR instead of rebuilding on the VPS.
# Use bootstrap.sh ONCE on a fresh server; use deploy.sh for every update
# after that.
# ═══════════════════════════════════════════════════════════════════════════

set -e

REPO_URL="https://github.com/mahendirank/ai-market-terminal.git"
INSTALL_DIR="/opt/zyvora"

step()   { echo; echo "════════════════════════════════════════════════════════════"; echo "▸ $1"; echo "════════════════════════════════════════════════════════════"; }
ok()     { echo "  ✅ $1"; }
warn()   { echo "  ⚠️  $1"; }
err()    { echo "  ❌ $1"; exit 1; }

if [[ $EUID -ne 0 ]]; then
  err "Run as root.  sudo bash bootstrap.sh"
fi

# ─── 1. Update + essentials ─────────────────────────────────────
step "Step 1/6 — Updating Ubuntu + installing essentials"
apt-get update -y >/dev/null 2>&1
DEBIAN_FRONTEND=noninteractive apt-get install -y git curl ca-certificates ufw nano htop >/dev/null 2>&1
ok "System packages installed"

# ─── 2. Docker ──────────────────────────────────────────────────
step "Step 2/6 — Installing Docker"
if command -v docker &>/dev/null; then
  ok "Docker already installed ($(docker --version))"
else
  curl -fsSL https://get.docker.com | sh >/dev/null 2>&1
  systemctl enable --now docker >/dev/null
  ok "Docker installed: $(docker --version)"
fi

# ─── 3. Firewall ────────────────────────────────────────────────
step "Step 3/6 — Configuring firewall (ports 22, 80, 443)"
ufw allow 22/tcp >/dev/null 2>&1 || true
ufw allow 80/tcp >/dev/null 2>&1 || true
ufw allow 443/tcp >/dev/null 2>&1 || true
echo "y" | ufw --force enable >/dev/null 2>&1 || true
ok "Firewall enabled"

# ─── 4. Clone / update repo ─────────────────────────────────────
step "Step 4/6 — Downloading terminal code"
if [ -d "$INSTALL_DIR/.git" ]; then
  cd "$INSTALL_DIR" && git pull origin main >/dev/null 2>&1
  ok "Code updated to latest version"
else
  rm -rf "$INSTALL_DIR" 2>/dev/null || true
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone "$REPO_URL" "$INSTALL_DIR" >/dev/null 2>&1
  ok "Code downloaded to $INSTALL_DIR"
fi
cd "$INSTALL_DIR"

# ─── 5. .env setup ──────────────────────────────────────────────
step "Step 5/6 — Environment configuration"
if [ ! -f .env ]; then
  cp .env.production.example .env
  warn ".env created from template — you MUST add your real API keys NOW"
  echo
  echo "Opening .env editor in 5 seconds."
  echo
  echo "  Fill in these 4 values (replace the 'your-…-here' placeholders):"
  echo "    GROQ_API_KEY        → your Groq API key"
  echo "    TELEGRAM_BOT_TOKEN  → your Telegram bot token"
  echo "    TELEGRAM_CHAT_ID    → your Telegram chat ID"
  echo "    ADMIN_PASSWORD      → pick a strong random password"
  echo
  echo "  Move with arrow keys. Type to overwrite. Then:"
  echo "    Ctrl+O   → press Enter to save"
  echo "    Ctrl+X   → exit"
  echo
  sleep 5
  nano .env
  # Sanity check: did they actually edit it?
  if grep -q "your-groq-api-key-here\|your-real-groq-key-here\|set-a-strong-random-password-here" .env 2>/dev/null; then
    warn "⚠ .env still contains placeholder values — the AI features may fail."
    warn "Re-run: nano $INSTALL_DIR/.env  then  bash $INSTALL_DIR/bootstrap.sh"
  else
    ok ".env saved"
  fi
else
  ok ".env already exists — skipping editor"
fi

# ─── 6. Pull image + start ──────────────────────────────────────
step "Step 6/6 — Pulling image from GHCR and starting the terminal stack"
echo "Pulling ghcr.io/mahendirank/ai-market-terminal:latest..."
docker compose -f docker-compose.prod.yml pull 2>&1 | tail -10
echo "Starting stack..."
docker compose -f docker-compose.prod.yml up -d 2>&1 | tail -10

# Wait + status
sleep 20
echo
echo "Container status:"
docker compose -f docker-compose.prod.yml ps

# ─── Done ───────────────────────────────────────────────────────
SERVER_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

step "✅ DEPLOYMENT COMPLETE"
cat <<EOF

  Your terminal is running on this server.

  ────────────────────────────────────────────────────────
  📌 DNS — point zyvoratech.co at this server in GoDaddy
  ────────────────────────────────────────────────────────
    Type   Name   Value             TTL
     A     @      $SERVER_IP   600
     A     www    $SERVER_IP   600

    (Delete any other A records for @ or www.)

  ────────────────────────────────────────────────────────
  🌐 Once DNS propagates (5-30 min), open:
    → https://zyvoratech.co
    → Login as admin / the ADMIN_PASSWORD you set
  ────────────────────────────────────────────────────────

  📋 Useful commands later (run from $INSTALL_DIR):
    docker compose -f docker-compose.prod.yml logs -f       # tail logs
    docker compose -f docker-compose.prod.yml restart       # restart stack
    ./deploy.sh                                             # update (pulls latest from GHCR)
    ./deploy.sh sha-718bd77                                 # rollback to a specific build

EOF
