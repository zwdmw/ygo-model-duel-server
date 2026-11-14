# Corrected Sky Striker Top 5

`tools/sync_corrected_top5.py` publishes the five highest-scoring completed
evaluations from the corrected-input experiment. Ties use descending training
steps, then checkpoint SHA-256. These are 64-game native evaluations against the
fixed official checkpoint, not online match win rates.

The publisher requires the corrected production native revision
`native-own-deck-candidates-v2-protocol-v3`. It verifies the training/runtime
card assets, checkpoint sidecars, corrected observation provenance, paired
game records, seed and evaluation identities before atomically publishing
`APP/model/compatible-experts.json`. A partial or failed publication retains
the previous manifest. Previously published artifacts are not deleted, including
models still in use after they leave the Top 5.

The existing evaluation-backed `model_run` catalog reads the manifest on each
request; its `sky_corrected` group stays separate from general evaluations.
Explicit `model_catalog` registration remains a separate, unranked mode.
The unmodified 2026-09-19 native release is not compatible with corrected inputs;
do not merely change `native-build.json` to bypass the runtime check.

## Production Layout

On the compute host, install the publisher and its shell supervisor together
under `ROOM_ROOT/admin/`. The room virtualenv is `ROOM_ROOT/venv`, and the default
app is `ROOM_ROOT/app-305m-admin-20260915`.

```bash
bash ROOM_ROOT/admin/supervise_corrected_top5.sh ROOM_ROOT EXPERIMENT_ROOT
```

This foreground supervisor uses `flock` to prevent duplicate supervisors,
restarts only the publisher after failure, and checks results every 60 seconds.
The Python publisher has its own writer lock, shared with `--once` mode.
Run the supervisor under the host's existing startup mechanism.

`deploy/autodl-corrected-top5.sh` is the existing installation's AutoDL boot hook
template. Inspect `/etc/autodl.sh` before installation and merge with any existing
commands. Never replace an unrelated startup hook. No systemd or platform
Supervisor restart is required.

Production diagnostics are in `ROOM_ROOT/data/admin/corrected-top5-sync.json`
and `corrected-top5-sync.log`. An unchanged index only advances `checked_at`.
The publisher does not activate models, restart rooms, modify passwords or
spectator settings, or start/stop training and evaluation workers.

## Verification

```bash
python -m unittest discover -s tests -p test_sync_corrected_top5.py -v
```

Tests cover ranking, replacement by new results, dynamic catalog reads,
staging, invalid evaluations, asset drift, failed copy, protocol drift, corrupted
catalog artifacts and recovery from an earlier sync error.

On 2026-09-21, production validation observed the 283M checkpoint automatically
replacing 142M in fifth place. The Aliyun panel's configured upstream returned
the same five entries. The independent supervisor passed duplicate-start,
single-writer and watcher-restart checks without changing the room server PID
or active model. The AutoDL hook was installed, but an actual container reboot
was not performed. The existing production catalog consumer was left running;
the local catalog integration does not overwrite newer production room code.
