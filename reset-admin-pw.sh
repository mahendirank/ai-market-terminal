#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# Zyvora Terminal — admin password reset
#
# Generates a strong 16-character password and writes it directly into the
# running container's auth database, bypassing the "ADMIN_PASSWORD env var
# only used on first init" limitation.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mahendirank/ai-market-terminal/main/reset-admin-pw.sh | bash
#
# Or if already in /opt/zyvora:
#   bash reset-admin-pw.sh
# ═══════════════════════════════════════════════════════════════════════════

set -e

# Generate a 16-char alphanumeric password.
# Excludes 0/1 (look like O/l) — easy to read aloud and type without typos.
NEW_PW=$(LC_ALL=C tr -dc 'A-HJ-NP-Za-km-z2-9' </dev/urandom | head -c 16)

if ! docker ps --format '{{.Names}}' | grep -q '^market-terminal$'; then
  echo "❌ market-terminal container is not running."
  echo "   Start it: docker compose -f /opt/zyvora/docker-compose.prod.yml up -d"
  exit 1
fi

# Use docker exec -e to pass the password safely (no shell interpolation).
RESULT=$(docker exec -e NEW_PW="$NEW_PW" market-terminal python3 -c "
import os, sys
sys.path.insert(0, '/app')
import auth
print('OK' if auth.change_password('admin', os.environ['NEW_PW']) else 'FAILED')
" 2>&1)

if [[ "$RESULT" != *"OK"* ]]; then
  echo "❌ Password change failed inside container:"
  echo "$RESULT"
  exit 1
fi

# Also update .env so future container rebuilds use the same password
if [[ -f /opt/zyvora/.env ]]; then
  sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${NEW_PW}|" /opt/zyvora/.env
fi

cat <<EOF

════════════════════════════════════════════════════════════
  ✅ ADMIN PASSWORD RESET — login now

    URL:      https://zyvoratech.co/login
    Username: admin
    Password: ${NEW_PW}

  ⚠ Write this down NOW. It will scroll out of view.
  ⚠ Treat it like a bank password — don't paste it anywhere
    public or share via chat.
════════════════════════════════════════════════════════════

To reset again later, run:  bash /opt/zyvora/reset-admin-pw.sh
EOF
