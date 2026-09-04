#!/bin/sh
# Weekly backup of the illusion inventory database. Runs on the VM as the
# illusion user. See backups.md.
set -u

DB=/home/illusion/inventory.db
LOCAL=/home/illusion/backups
LOG=/home/illusion/logs/backup.log
KIOSK_DEST=easternedge@eer-inventory:/easternedge/illusion-backups/

STAMP=$(date +%F)
OUT="$LOCAL/inventory-$STAMP.db"

log() { echo "$(date -Iseconds) $*" >> "$LOG"; }

mkdir -p "$LOCAL"

# .backup, not cp: the database is in WAL mode, so a byte-for-byte copy of a
# live file is only crash-consistent. This writes a clean file that opens with
# no recovery needed.
if ! sqlite3 "$DB" ".backup '$OUT'"; then
	log "FAILED to create $OUT"
	exit 1
fi

# Verify before keeping it. A backup nobody has opened is a guess.
if ! sqlite3 "$OUT" "PRAGMA integrity_check;" | grep -q '^ok$'; then
	log "FAILED integrity check on $OUT"
	exit 1
fi

log "created $OUT ($(wc -c < "$OUT") bytes)"

# The laptop is often unreachable, so a failure here is logged and ignored. It
# must never stop the local copy from being kept.
if rsync -a "$OUT" "$KIOSK_DEST"; then
	log "copied to kiosk"
else
	log "FAILED copy to kiosk (unreachable?)"
fi

find "$LOCAL" -name 'inventory-*.db' -mtime +365 -delete
