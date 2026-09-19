# Remote Room Deployment

## 285M Selection (September 15, 2026)

The remote deployment selects the pinned 285,212,672-step checkpoint with
SHA-256 `c9b6dd64fc98144d0be59fb98e0f3fa2ed2c328b1dd61e26284431170c678833`.
This is training progress, not a 285-million-parameter architecture.
The isolated staged runtime is `app-285m-20260915/`; the original `app/`,
195M weights and original manifest remain unchanged.

Launch the replacement device with:

```bash
venv/bin/python room_service.py --model 285M --app-dir app-285m-20260915 \
  --tcp-config bridge.json --tcp-port 6008
```

The no-selection model deck is `BestowedDragon` (Tenpai). Native room presets
`ygo2-tenpai` and `mdpro3-tenpai` select that same deck. Other explicit deck
presets remain available and their passwords are not rotated.
The rebuilt Windows picker pins both checkpoint steps and SHA-256. Its
one-time default migration selects the bundled `Agent285M_Tenpai.ydk`, preserves
the human deck, and respects later user changes to either deck.
Build output is `release-285m/Agent195MRemote/`; its historical internal
executable/data-directory names remain for compatibility, but the UI says 285M.
Previously distributed 195M-only pickers must be replaced.

Verification evidence is in `../data/remote-deployment/285m-verification.json`:
97 unit tests, two full native-protocol games and one full public-WSS game
using the rebuilt Windows connector. All three verified both loaded decks,
the exact checkpoint SHA and the same resident policy PID. Disconnects during
startup and active play released the room. Startup times were 1.46, 8.08 and
1.39 seconds, respectively; do not treat a single run as a latency guarantee.
The credential-free distribution is `Agent285MRemote-20260915.zip`.

`deploy_285m.py` checks paired assets, preflights CPU inference and refuses to
switch an occupied room. `venv/bin/python deploy_285m.py rollback` switches
back to the untouched original app when idle; use the old picker for 195M.
Do not run `prepare` again over the existing staged directory.

For native server-address/port access without a player connector, see
`README-tcp-bridge.md`. On September 15, 2026 the room was restored on the
replacement device via SSH port 42801. The new HTTPS gateway is verified,
and the native bridge is running on internal port 6008. Public native TCP
access still needs a real TCP mapping. Earlier results below are historical;
new-device evidence is under `../data/remote-deployment/new-device-tcp-*`.

The existing AutoDL HTTPS mapping was verified with an actual binary WebSocket
roundtrip. The gateway uses port 6006; the native TCP bridge uses port 6008.
YGOPro2 does not speak HTTPS/WSS, so the portable connector exposes loopback TCP
27915 and a loopback-only selection API on 27916.

Remote installation:

```text
/root/autodl-tmp/agent195m-room-20260915
```

- `app/`: immutable paired snapshot plus the room adapter.
- `venv/`: isolated Python packages; no training packages were changed.
- `room_service.py`: private supervisor, four-CPU affinity, nice 10, CPU-only
  inference, 4 GiB aggregate RSS watchdog (checked every two seconds).
- `gateway.py`: only authenticated health/catalog/prepare/duel routes.
- Internal model endpoints bind to `127.0.0.1:7915/7916`.
- The public endpoint is configured in `client.py` and per-user invitations.
- The policy stays resident. Every duel gets a fresh engine and recurrent state.
- The supervisor retries a failed room up to three times; logs remain available.
- Services stop when the rented instance is shut down. No account startup policy
  or existing supervisor configuration was modified.

## Invitations

These commands run over the owner's existing SSH access, not through the gateway:

```bash
cd /root/autodl-tmp/agent195m-room-20260915
venv/bin/python invite.py guest01
venv/bin/python invite.py guest01 --revoke
```

Creation writes `invite-guest01.json` with an endpoint and a room-only token.
The authentication store retains SHA-256 hashes, not plaintext tokens.
Revocation affects subsequent requests/connections; an already-connected duel
is not forcibly terminated. Keep the generated invitation private.
The distributable ZIP intentionally contains no invitations or SSH credentials.

Start/restart the room using `venv/bin/python room_service.py` as a detached
process with output redirected to `service.log`. A file lock prevents duplicate
supervisors. To stop it, verify the PID in `service.pid` belongs to this exact
script and send SIGTERM. The supervisor shuts down only its own descendants.
For the active 285M service use the full launch command above, including
the explicit model/app directory and native bridge. Check that the room is idle before a
restart. Endpoint migration uses `migrate_endpoint.py`; new invitations read
`public-endpoint.json` or accept an explicit `--endpoint`.

## Verification

- 41 existing protocol, selection, widget and startup tests passed.
- 13 gateway/configuration tests passed: authentication, origin rejection,
  route allowlist, bounded requests/frames, binary roundtrip, single occupancy,
  disconnect cleanup, and invitation revocation.
- Real external HTTPS checks rejected missing/invalid invitations.
- Packaged Windows connector completed YGOPro2 and legacy network games.
- The first completed YGOPro2-profile game started in 1.92 seconds; the next
  legacy game started in 1.91 seconds. Both used one resident policy PID.
- External disconnect tests released both starting and active rooms.
- Both completed games had verified deck contents and matching loaded hashes
  (23 and 65 model decisions). Their resident policy PID was unchanged.
- Remote direct inference and two consecutive resident sessions produced the
  same 61 fixed-seed decisions, also identical to the saved local baseline.
- Artifacts are under `../data/remote-deployment/`; invitations there are private.
- Network tests use WindBot only as the independent external test opponent,
  never as the model policy or a fallback.

## Client Build

Use an isolated Windows build environment with `aiohttp==3.14.3` and the verified
PyInstaller version 6.22.3. Include the shared `deck_picker`, `deck_config`, `relay`
modules and `picker_zh.json`. The compiled client does not import JAX or contain
the model, engine, training data, or game executable.
