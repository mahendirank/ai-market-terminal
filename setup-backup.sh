#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# Zyvora Terminal — install daily backup cron
#
# Runs once. Sets up:
#   - /opt/backups/ directory
#   - Cron entry: 22:00 UTC daily (≈ 3:30 AM IST) → /opt/zyvora/backup.sh
#   - Log file: /var/log/zyvora-backup.log
#
# Runs an immediate backup at the end so you can confirm it worked.
# Idempotent — safe to run again to re-install.
# ═══════════════════════════════════════════════════════════════════════════

set -e

INSTALL_DIR="/opt/zyvora"
BACKUP_DIR="/opt/backups"
LOG_FILE="/var/log/zyvora-backup.log"

[[ -x "$INSTALL_DIR/backup.sh" ]] || { echo "❌ $INSTALL_DIR/backup.sh missing or not executable"; exit 1; }

mkdir -p "$BACKUP_DIR"
touch "$LOG_FILE"

# Cron line — 22:00 UTC = 03:30 IST (server runs in UTC by default)
CRON_LINE="0 22 * * * $INSTALL_DIR/backup.sh >> $LOG_FILE 2>&1"

# Replace any existing zyvora backup line (idempotent), append fresh
( crontab -l 2>/dev/null | grep -v 'zyvora.*backup\.sh' ; echo "$CRON_LINE" ) | crontab -

echo "  ✅ Cron installed:  $CRON_LINE"
echo "  ✅ Backup dir:      $BACKUP_DIR"
echo "  ✅ Log file:        $LOG_FILE"
echo
echo "Running an immediate backup as a smoke test..."
"$INSTALL_DIR/backup.sh"
echo
echo "Backup files now present:"
ls -lh "$BACKUP_DIR" | tail -5
echo
cat <<EOF
════════════════════════════════════════════════════════════
  ✅ DAILY BACKUPS ENABLED

  Schedule:  03:30 AM IST every day (silently)
  Storage:   $BACKUP_DIR  (~5 MB per backup, 7 kept)
  Logs:      tail -f $LOG_FILE
  Manual:    bash $INSTALL_DIR/backup.sh   (anytime)

  Restore a backup:
    cd /tmp && tar -xzf $BACKUP_DIR/zyvora-db-YYYY-MM-DD_*.tar.gz
    docker compose -f $INSTALL_DIR/docker-compose.prod.yml stop market-terminal
    # copy *.db files into the terminal_db volume mountpoint
    docker compose -f $INSTALL_DIR/docker-compose.prod.yml up -d
════════════════════════════════════════════════════════════
EOF
