# Remote Room Deployment

For native server-address/port access without a player connector, see
`README-tcp-bridge.md`. On September 15, 2026 the room was restored on the
replacement device via SSH port 42801. The new HTTPS gateway is verified,
and the native bridge is running on internal port 6008. Public native TCP
access still needs a real TCP mapping. Earlier results below are historical;
new-device evidence is under `../data/remote-deployment/new-device-tcp-*`.

The existing AutoDL HTTPS mapping was verified with an actual binary WebSocket
roundtrip. The room uses port 6006; port 6008 is intentionally unused.
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
On the replacement device include `--tcp-config bridge.json --tcp-port 6008`
to also supervise the native bridge. Check that the room is idle before a
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
