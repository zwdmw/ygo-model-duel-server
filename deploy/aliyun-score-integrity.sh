#!/usr/bin/env bash
set -euo pipefail

stage="${1:?provide the uploaded patch directory}"
test -f "$stage/server.py"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="/opt/ygo-tournament/backups/score-integrity-$stamp"
sudo -n install -d -m 755 "$backup"
sudo -n cp -a \
    /opt/ygo-tournament/app/tournament/server.py \
    "$backup/server.py"

/opt/ygo-tournament/venv/bin/python -m py_compile \
    "$stage/server.py"
sudo -n install -m 644 -o ygo-tournament -g ygo-tournament \
    "$stage/server.py" /opt/ygo-tournament/app/tournament/server.py
sudo -n /opt/ygo-tournament/venv/bin/python -m py_compile \
    /opt/ygo-tournament/app/tournament/server.py

sudo -n systemctl restart ygo-tournament
healthy=0
for _ in $(seq 1 30); do
    if curl -fsS --max-time 2 \
        http://127.0.0.1:8787/health >/tmp/ygo-tournament-health.json; then
        healthy=1
        break
    fi
    sleep 1
done
test "$healthy" = 1

cat /tmp/ygo-tournament-health.json
printf '\nbackup=%s\n' "$backup"
sudo -n sha256sum /opt/ygo-tournament/app/tournament/server.py
sudo -n systemctl is-active ygo-tournament ygo-pvp ygo-evolution
