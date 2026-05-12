#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# Zyvora Terminal — daily backup of application data
#
# What's backed up:
#   - All SQLite databases (auth.db = users, signal_memory.db, etc.)
#   - .env (your API keys + admin password)
#
# What's NOT backed up (not needed — rebuildable from git):
#   - The Docker image
#   - Caddy / Redis configs
#   - Python dependencies
#
# Restore is just: untar into /opt/zyvora/db + restart container.
#
# Designed to run via cron — silent on success, logs failures.
# ═══════════════════════════════════════════════════════════════════════════

set -e

BACKUP_DIR="/opt/backups"
TIMESTAMP=$(date +%Y-%m-%d_%H%M)
RETENTION_DAYS=7

mkdir -p "$BACKUP_DIR"

# Find the named volume that holds the SQLite DBs
VOLUME_PATH=$(docker volume inspect zyvora_terminal_db 2>/dev/null | grep Mountpoint | awk -F'"' '{print $4}')
if [[ -z "$VOLUME_PATH" ]]; then
  VOL_NAME=$(docker volume ls -q | grep terminal_db | head -1)
  [[ -n "$VOL_NAME" ]] && VOLUME_PATH=$(docker volume inspect "$VOL_NAME" 2>/dev/null | grep Mountpoint | awk -F'"' '{print $4}')
fi

if [[ -z "$VOLUME_PATH" || ! -d "$VOLUME_PATH" ]]; then
  echo "[$(date -Iseconds)] ❌ BACKUP FAILED: could not find db volume mountpoint" >&2
  exit 1
fi

# Bundle databases
DB_ARCHIVE="${BACKUP_DIR}/zyvora-db-${TIMESTAMP}.tar.gz"
cd "$VOLUME_PATH"
if ! ls *.db >/dev/null 2>&1; then
  echo "[$(date -Iseconds)] ❌ BACKUP FAILED: no .db files in $VOLUME_PATH" >&2
  exit 1
fi
tar -czf "$DB_ARCHIVE" *.db
DB_SIZE=$(du -h "$DB_ARCHIVE" | cut -f1)

# Bundle .env config (separate, has secrets)
CFG_ARCHIVE=""
if [[ -f /opt/zyvora/.env ]]; then
  CFG_ARCHIVE="${BACKUP_DIR}/zyvora-cfg-${TIMESTAMP}.tar.gz"
  tar -czf "$CFG_ARCHIVE" -C /opt/zyvora .env 2>/dev/null
  chmod 600 "$CFG_ARCHIVE"  # Only root can read (it has secrets)
fi

# Prune old backups
find "$BACKUP_DIR" -name "zyvora-*.tar.gz" -mtime +${RETENTION_DAYS} -delete 2>/dev/null || true

# Log success
COUNT=$(ls "$BACKUP_DIR"/zyvora-db-*.tar.gz 2>/dev/null | wc -l)
echo "[$(date -Iseconds)] ✅ Backup OK: ${DB_ARCHIVE##*/} (${DB_SIZE}) — ${COUNT} backups retained"
