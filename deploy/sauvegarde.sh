#!/bin/sh
# Sauvegarde quotidienne du dossier data (base + photos). À planifier :
#   crontab -e  →  15 3 * * * /home/pi/SharedFleet/deploy/sauvegarde.sh /mnt/usb/sauvegardes
set -eu
DEST="${1:?Dossier de destination manquant}"
SRC="$(cd "$(dirname "$0")/.." && pwd)/data"
mkdir -p "$DEST"
# Copie cohérente de la base SQLite même si l'application écrit pendant la sauvegarde
python3 -c "import sqlite3, sys; s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" \
  "$SRC/sharedfleet.db" "$DEST/sharedfleet-$(date +%F).db"
tar -czf "$DEST/fichiers-$(date +%F).tar.gz" -C "$SRC" --exclude='sharedfleet.db*' .
# On garde 14 jours
find "$DEST" -name 'sharedfleet-*.db' -mtime +14 -delete
find "$DEST" -name 'fichiers-*.tar.gz' -mtime +14 -delete
