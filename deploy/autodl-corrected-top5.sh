#!/bin/bash
# Install as /etc/autodl.sh only when that hook is absent; otherwise merge.
root=/root/autodl-tmp/agent195m-room-20260915
experiment=/root/autodl-tmp/ygo-agent-main-gpu-20260824/experiments/expert-sky-striker-corrected-20260920
mkdir -p "$root/data/admin"
nohup /bin/bash "$root/admin/supervise_corrected_top5.sh" "$root" "$experiment" \
    >> "$root/data/admin/corrected-top5-sync.log" 2>&1 < /dev/null &
