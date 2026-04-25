#!/usr/bin/env bash
#
# Wave 14 W14-H17 — Dragon state snapshot, driven by tinkerclaw-backup.timer.
#
# Captures (in one timestamped directory under /home/radxa/backups):
#   tinkerclaw-<stamp>.db       — main state DB (sessions/memory/docs/facts/
#                                  embeddings).  Captured via sqlite3 .backup
#                                  so WAL-concurrent writes are safe.
#   notes-<stamp>.db            — notes service DB
#   tinkerclaw-cfg-<stamp>.tgz  — /home/radxa/.tinkerclaw/ + /home/radxa/.env
#                                  (auth tokens, OpenRouter key, gateway config)
#
# Retention: last KEEP copies of each artifact type (default 14 = ~14 hrs).
# Deleting older backups keeps the backup dir bounded.
#
# Location: /home/radxa/bin/tinkerclaw-backup.sh (symlinked from
# TinkerBox repo's scripts/ on deploy — copy/symlink it manually now
# that the legacy install-services.sh helper is gone, or invoke it via
# systemd/tinkerclaw-backup.service + .timer).

set -euo pipefail

BACKUP_ROOT="/home/radxa/backups"
KEEP="${TINKERCLAW_BACKUP_KEEP:-14}"
STAMP="$(date +%Y%m%d-%H%M%S)"

mkdir -p "$BACKUP_ROOT"

# sqlite3 .backup is WAL-aware — works on live DBs without a --read-only copy
if [ -f /home/radxa/tinkerclaw/tinkerclaw.db ]; then
  sqlite3 /home/radxa/tinkerclaw/tinkerclaw.db ".backup '$BACKUP_ROOT/tinkerclaw-$STAMP.db'"
fi
if [ -f /home/radxa/tinkerclaw/notes/notes.db ]; then
  sqlite3 /home/radxa/tinkerclaw/notes/notes.db ".backup '$BACKUP_ROOT/notes-$STAMP.db'"
fi

# Config tarball — includes .env which holds DRAGON_API_TOKEN, OpenRouter key,
# TinkerClaw gateway token.  600 on the tarball so only radxa can read.
tar_tmp="$BACKUP_ROOT/tinkerclaw-cfg-$STAMP.tgz"
tar --ignore-failed-read -czf "$tar_tmp" \
  -C /home/radxa \
  .tinkerclaw 2>/dev/null \
  .env 2>/dev/null || true
chmod 600 "$tar_tmp" 2>/dev/null || true

# Retention: keep the most recent $KEEP of each artifact class
for prefix in "tinkerclaw" "notes" "tinkerclaw-cfg"; do
  case "$prefix" in
    tinkerclaw-cfg) ext="tgz" ;;
    *)              ext="db"  ;;
  esac
  # shellcheck disable=SC2012  # ls for mtime sorting is acceptable here
  ls -1t "$BACKUP_ROOT/${prefix}-"*".$ext" 2>/dev/null \
    | tail -n +$((KEEP + 1)) \
    | xargs -r rm -f
done

# Print a short receipt so `journalctl -u tinkerclaw-backup` shows progress
# with du + file counts rather than silent success.
tinkerclaw_count=$(ls -1 "$BACKUP_ROOT/tinkerclaw-"*.db 2>/dev/null | wc -l)
notes_count=$(ls -1 "$BACKUP_ROOT/notes-"*.db 2>/dev/null | wc -l)
cfg_count=$(ls -1 "$BACKUP_ROOT/tinkerclaw-cfg-"*.tgz 2>/dev/null | wc -l)
total_bytes=$(du -sb "$BACKUP_ROOT" 2>/dev/null | awk '{print $1}')
echo "tinkerclaw-backup: stamp=$STAMP tinkerclaw_db=$tinkerclaw_count notes_db=$notes_count cfg=$cfg_count total_bytes=$total_bytes"
