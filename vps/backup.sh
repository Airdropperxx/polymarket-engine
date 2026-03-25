#!/bin/bash
# vps/backup.sh — Run daily via cron to back up trades.db
# Add to crontab: 0 3 * * * /opt/polymarket-engine/vps/backup.sh
APP_DIR="/opt/polymarket-engine"
BACKUP_DIR="$APP_DIR/backups"
DATE=$(date +%Y%m%d)

mkdir -p "$BACKUP_DIR"

# SQLite online backup (safe while DB is live)
sqlite3 "$APP_DIR/data/trades.db" ".backup $BACKUP_DIR/trades_$DATE.db"

# Keep last 30 daily backups
find "$BACKUP_DIR" -name "trades_*.db" -mtime +30 -delete

echo "Backup complete: $BACKUP_DIR/trades_$DATE.db"