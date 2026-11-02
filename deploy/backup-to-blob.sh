#!/bin/bash
# Nightly backup of the CogniCore SQLite data to Azure Blob storage.
#
# Install on the VM:
#   sudo install -m 755 deploy/backup-to-blob.sh /usr/local/bin/cognicore-backup.sh
#   sudo nano /root/.cognicore-backup-sas   # container SAS URL, Create permission only
#   sudo chmod 600 /root/.cognicore-backup-sas
#   echo '30 20 * * * root /usr/local/bin/cognicore-backup.sh || logger -t cognicore-backup FAILED' | sudo tee /etc/cron.d/cognicore-backup
#
# Create-only means this host can add backups but cannot read, overwrite or
# delete them. A lifecycle rule on the container deletes them after 30 days.
set -euo pipefail
SRC=/mnt/cognicore-data/cognicore/remote
SAS_URL=$(tr -d "[:space:]" < /root/.cognicore-backup-sas)
BASE=${SAS_URL%%\?*}
QS=${SAS_URL#*\?}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/remote"
# sqlite3 online backup gives a consistent copy while the server keeps running.
for db in "$SRC"/*.db; do
  python3 - "$db" "$TMP/remote/$(basename "$db")" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close(); src.close()
PY
done
NAME="cognicore-$(date -u +%Y%m%dT%H%M%SZ).tgz"
tar czf "$TMP/$NAME" -C "$TMP" remote
curl -fsS --retry 3 -T "$TMP/$NAME" -H "x-ms-blob-type: BlockBlob" "$BASE/$NAME?$QS"
logger -t cognicore-backup "uploaded $NAME ($(stat -c %s "$TMP/$NAME") bytes)"
