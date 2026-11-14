#!/bin/bash
# Independent of the room, policy, training and evaluation supervisors.
set -u
umask 077

root=${1:?Expected room root}
experiment=${2:?Expected corrected experiment}
app=${3:-"$root/app-305m-admin-20260915"}
directory=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd) || exit 1
state="$root/data/admin/corrected-top5-sync.json"
mkdir -p -- "$(dirname -- "$state")" || exit 1
exec 9>"$state.service.lock"
flock -n 9 || exit 0

stopping=0
child=
stop() {
    stopping=1
    if [ -n "$child" ]; then
        kill -TERM "$child" 2>/dev/null || true
        wait "$child" 2>/dev/null || true
    fi
}
trap stop TERM INT

while [ "$stopping" -eq 0 ]; do
    "$root/venv/bin/python" -u "$directory/sync_corrected_top5.py" \
        --experiment "$experiment" --app "$app" --state "$state" --interval 60 9>&- &
    child=$!
    wait "$child"
    code=$?
    child=
    [ "$stopping" -eq 0 ] || break
    printf 'Corrected Top 5 watcher exited (%s); retrying in 5 seconds.\n' "$code"
    sleep 5 &
    child=$!
    wait "$child"
    child=
done
