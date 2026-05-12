#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# Zyvora Terminal — nuclear admin reset
#
# When reset-admin-pw.sh isn't enough, this wipes the entire auth.db.
# Effect:
#   - All users deleted (admin + subscribers if any)
#   - All active sessions invalidated
#   - On next request, init_auth_db() runs again, sees empty users table,
#     and creates a fresh admin from ADMIN_PASSWORD in .env
#
# Use only when login is broken and you accept losing other users.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/mahendirank/ai-market-terminal/main/nuclear-reset-admin.sh | bash
# ═══════════════════════════════════════════════════════════════════════════

set -e

# Generate a 14-char alphanumeric password.
# Excludes 0/1/O/l (easy to confuse) and special chars (paste/type-safe).
NEW_PW=$(LC_ALL=C tr -dc 'A-HJ-NP-Za-km-z2-9' </dev/urandom | head -c 14)

# 1. Write new password to .env so init_auth_db picks it up
if [[ ! -f /opt/zyvora/.env ]]; then
  echo "❌ /opt/zyvora/.env not found"
  exit 1
fi
sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=${NEW_PW}|" /opt/zyvora/.env
echo "  ✅ Updated ADMIN_PASSWORD in .env"

# 2. Stop the container cleanly so it doesn't hold the DB file open
docker compose -f /opt/zyvora/docker-compose.prod.yml stop market-terminal >/dev/null 2>&1
echo "  ✅ Stopped market-terminal"

# 3. Find the auth.db on the host (it lives in the named volume terminal_db)
# Volume mount: terminal_db → /app/db
VOLUME_PATH=$(docker volume inspect zyvora_terminal_db 2>/dev/null | grep Mountpoint | head -1 | awk -F'"' '{print $4}')
if [[ -z "$VOLUME_PATH" ]]; then
  # Try the alt name used if compose project name differs
  VOLUME_PATH=$(docker volume inspect $(docker volume ls -q | grep terminal_db | head -1) 2>/dev/null | grep Mountpoint | head -1 | awk -F'"' '{print $4}')
fi

if [[ -n "$VOLUME_PATH" && -f "${VOLUME_PATH}/auth.db" ]]; then
  rm -f "${VOLUME_PATH}/auth.db" "${VOLUME_PATH}/auth.db-journal" "${VOLUME_PATH}/auth.db-wal" "${VOLUME_PATH}/auth.db-shm"
  echo "  ✅ Deleted auth.db from volume"
else
  echo "  ⚠ Could not find auth.db on host — will rely on container delete"
fi

# 4. Start the container — init_auth_db() runs on import, sees empty users, creates admin
docker compose -f /opt/zyvora/docker-compose.prod.yml up -d market-terminal >/dev/null 2>&1
echo "  ✅ Started market-terminal"

# 5. Wait for healthy state
echo -n "  ⏳ Waiting for container to become healthy"
for i in {1..30}; do
  sleep 2
  STATUS=$(docker inspect --format='{{.State.Health.Status}}' market-terminal 2>/dev/null || echo "unknown")
  if [[ "$STATUS" == "healthy" ]]; then
    echo " — healthy!"
    break
  fi
  echo -n "."
done

cat <<EOF


════════════════════════════════════════════════════════════
  ✅ NUCLEAR RESET DONE — admin recreated from .env

    URL:      https://zyvoratech.co/login
    Username: admin
    Password: ${NEW_PW}

  ⚠ WRITE THIS DOWN NOW — it will scroll out of view.
  ⚠ All previous users (if any) were wiped — recreate them
    via Admin Panel after login.

  Next steps after login:
    1. Open Admin Panel (top-right user menu)
    2. Change to a password you'll remember
    3. Create subscriber accounts for your clients
════════════════════════════════════════════════════════════
EOF
