#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# Zyvora Terminal — interactive .env filler
#
# Skips nano. Asks 4 questions. Updates .env. Restarts the terminal.
# Run AFTER deploy.sh, from any directory.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mahendirank/ai-market-terminal/main/fill-env.sh | bash
# ═══════════════════════════════════════════════════════════════════════════

set -e

ENV_FILE="/opt/zyvora/.env"
COMPOSE_FILE="/opt/zyvora/docker-compose.prod.yml"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "❌ $ENV_FILE not found. Run deploy.sh first."
  exit 1
fi

cat <<'HEAD'

════════════════════════════════════════════════════════════
  Zyvora Terminal — interactive .env setup
════════════════════════════════════════════════════════════

I'll ask for 4 values. Paste each, then press Enter.
To SKIP a value (fix it later), just press Enter without typing.

For the password: use letters, digits, and these symbols only:
  ! @ # $ % ^ & * ( ) - _ = +
Avoid spaces, quotes, /, \, |.

HEAD

read -p "1. Groq API key (gsk_... — free at https://console.groq.com): " GROQ_KEY </dev/tty
read -p "2. Telegram bot token (blank to skip): " TG_TOKEN </dev/tty
read -p "3. Telegram chat ID (e.g. -1001379475837 or blank to skip): " TG_CHAT </dev/tty
read -p "4. Admin dashboard password (pick strong, write it down): " ADMIN_PW </dev/tty
echo

upd() {
  local key="$1" val="$2"
  [[ -z "$val" ]] && { echo "  ⏭  ${key} skipped"; return; }
  sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  echo "  ✅ ${key} set"
}

upd GROQ_API_KEY       "$GROQ_KEY"
upd TELEGRAM_BOT_TOKEN "$TG_TOKEN"
upd TELEGRAM_CHAT_ID   "$TG_CHAT"
upd ADMIN_PASSWORD     "$ADMIN_PW"

echo
echo "Restarting terminal container so new keys take effect..."
cd /opt/zyvora
docker compose -f docker-compose.prod.yml restart market-terminal 2>&1 | tail -3 || \
  docker compose -f docker-compose.prod.yml up -d --build 2>&1 | tail -5

echo
SERVER_IP=$(curl -s --max-time 5 ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')
cat <<EOF

✅ DONE.

  Open in browser:
    https://zyvoratech.co     (once DNS is pointed here)
    http://${SERVER_IP}        (works right now, no HTTPS)

  Login as:
    Username: admin
    Password: [the password you just set above]

  Useful later:
    nano /opt/zyvora/.env                                            # edit anything
    docker compose -f /opt/zyvora/docker-compose.prod.yml logs -f    # tail logs
    docker compose -f /opt/zyvora/docker-compose.prod.yml restart    # restart all

EOF
