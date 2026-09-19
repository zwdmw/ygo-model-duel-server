# Native TCP Server

Players use the game's normal server address, port and room password fields.
There is no player-side connector. The bridge must run on a machine with an
actual inbound public TCP mapping:

```text
YGOPro2 / MDPro3 -> public TCP bridge -> authenticated WSS -> existing GPU-rental room
```

The model stays in its existing room. The bridge contains no model, inference,
rule changes or alternative policy. One upstream room still permits one duel
at a time; multiple room passwords are presets, not additional concurrent rooms.

## Deployment Status: September 15, 2026

The room and bridge have been restored on the replacement device reached
through SSH port 42801. Existing snapshot files and the isolated environment
survived the move; the checkpoint SHA-256 is unchanged.

The gateway now uses:

```text
https://u1069556-l3la-2eb9c5df.westb.seetacloud.com:8443
```

The bridge listens on `0.0.0.0:6008` and is supervised with the model room.
The model/API still bind only to `127.0.0.1:7915/7916`. The supervisor uses
four CPU cores, nice 10 and a combined 4 GiB RSS limit, with GPU use disabled.
No training packages, checkpoints, jobs or GPU settings were changed.

Two complete native-protocol model games passed through a temporary SSH
forward to the bridge and then the public WSS gateway. The YGOPro2/Burn and
legacy/BlueEyes games started in approximately 1.41 seconds, used the same
resident policy PID, and had their actual deck files and hashes verified.
Startup cancellation and active-duel disconnect both released the room.
This is NOT verification of a public native TCP ingress.

There is no confirmed public TCP address/port. An HTTPS mapping cannot be
entered as a raw game server address. Preserve the existing SSH mapping and
HTTPS port 6006. Use either:

1. A real TCP mapping to port 6008 on the existing rental, where the bridge is
   already listening; or
2. Another public TCP host running this bridge, with the existing rental still
   reached through its HTTPS/WSS gateway.

Do not switch the existing 6006 gateway to TCP: that would break the WSS
upstream and the existing portable connector.

The two HTTPS addresses supplied for the replacement device are not a native
TCP mapping. The second HTTPS address may return a proxy error while its
internal port 6008 is serving native game packets.

## Existing Device

Deployment root:

```text
/root/autodl-tmp/agent195m-room-20260915
```

The verified launch command, run only when its previous supervisor is stopped:

```bash
venv/bin/python room_service.py --tcp-config bridge.json --tcp-port 6008
```

`bridge.json` uses a dedicated upstream invitation. `room-codes.json` contains
six presets: BlueEyes, Burn and BestowedDragon, each for YGOPro2 and legacy.
The existing owner/friends invitations were migrated without changing tokens.
`public-endpoint.json` is now the default endpoint for new `invite.py` calls.
The local owner shortcut configuration was also updated. A fresh portable
client should be launched with `--config` pointing to a migrated invitation;
older binaries may otherwise retain the old default server address.

## Install

Python 3.10 or newer is required. The archive contains an explicit source
allowlist, no invitations, room passwords, SSH credentials, weights or decks.
On the TCP host, extract it to a private service directory and run:

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
```

Create a dedicated upstream room invitation with the existing `invite.py`
over owner SSH after access has been restored. Transfer only that invitation
to the TCP host, not the SSH key/password or the model.

Create passwords and choose the model decks. Example filenames are placeholders
for operator-supplied YDK files:

```bash
venv/bin/python configure_tcp_bridge.py \
  --invite /private/invite-tcp-bridge.json \
  --room ygo2-blue YGOPro2 /private/decks/BlueEyes.ydk \
  --room mdpro3-blue legacy /private/decks/BlueEyes.ydk \
  --room ygo2-burn YGOPro2 /private/decks/Burn.ydk \
  --room mdpro3-burn legacy /private/decks/Burn.ydk \
  --out /private/bridge.json --codes /private/room-codes.json
venv/bin/python tcp_bridge.py --config /private/bridge.json --check
venv/bin/python tcp_bridge.py --config /private/bridge.json --host 0.0.0.0 --port 7915
```

Use `--port 6008` when deploying behind a verified native TCP mapping to that
port on the rental. `--host` defaults to loopback for safe local smoke tests.
Configuration validation is offline and does not prove upstream reachability
or card-pool support. The existing backend validates cards on each preparation.

For an initial detached Linux run:

```bash
nohup venv/bin/python tcp_bridge.py --config /private/bridge.json \
  --host 0.0.0.0 --port 7915 > tcp-bridge.log 2>&1 < /dev/null &
```

This command does not install boot-time startup or a restart supervisor.
Do not run a second copy on another port against the same upstream as a way
to increase capacity. Do not open or change a firewall until the intended
address, TCP port and mapping are confirmed.

## Player Settings

- Server: the TCP host's public hostname or IP, without an HTTPS prefix.
- Port: the external TCP mapping's port, which may differ from the listen port.
- Password: one entry from `room-codes.json`, matching the player's client
  profile and the desired model deck.
- Human deck: choose any snapshot-supported deck in the native game's lobby.

`YGOPro2` and `legacy` use the existing adapter profiles. The latter is the
profile already used for MDPro3. This does not establish compatibility with
every YGOPro fork or official Master Duel.

To add a different model deck, the operator creates a matching preset. Native
clients do not gain a new model-deck file picker. The existing portable
connector remains available for arbitrary dual-deck selection.

The bridge supplies a valid placeholder human deck only for the preparation
API. The existing room then loads the actual human CTOS_UPDATE_DECK packet,
including lobby changes. Session human-deck metadata is `Room selection`;
the actual saved human/model YDK files and their hashes remain authoritative.

## Authentication And Limits

- Wrong, missing or malformed room passwords make zero upstream requests.
- Generated passwords have 96 random bits and fit the native 20-character
  UTF-16 field with a terminator. The bridge configuration stores their hashes.
- The upstream bearer token is server-only; never distribute `bridge.json`.
  `room-codes.json` contains shareable room access, not administrative access.
- Ordinary native TCP is not encrypted. Room passwords can be observed on
  that connection. Never reuse account/SSH passwords. Use WSS connector access
  when player-to-server encryption is required.
- HTTPS certificate verification remains enabled. No insecure TLS mode exists.
- Limits: 32 pending connections, 4 per source IP, 10 attempts/minute/IP,
  120 attempts/minute globally, 10-second handshake, 25-second setup,
  64 KiB setup buffer, 15-minute game-data idle timeout and 4-hour session cap.
- Disconnects during preparation cancel setup; active disconnects close both
  TCP and WSS. Existing remote duels are not intentionally interrupted.
- Repeated game packets are forwarded unchanged after the rewritten join.
- Configuration is loaded at startup. Rotate/remove room codes between duels
  and restart this bridge. Upstream invite revocation follows the existing
  gateway behavior and does not forcibly terminate a current duel.
- POSIX generated private files use mode 0600. On Windows, store configuration
  in an account-private directory and verify its NTFS permissions.

## Verification

```bash
venv/bin/python -m unittest test_tcp_bridge test_gateway -v
```

Tests use actual loopback TCP, the existing authenticated gateway and an echo
backend. They cover framing, fragmentation, preset profiles, pipeline
preservation, capacity, upstream failures, cancellation, idle/deadline cleanup,
and multi-frame binary transfer. The TLS test generates an ephemeral certificate
using OpenSSL, first verifies rejection by the default trust store, then checks
real HTTPS/WSS transfer with the test certificate explicitly trusted.

The automated transport tests do not replace real games. The complete-game
evidence from the replacement device is under
`../data/remote-deployment/new-device-tcp-ygo2/` and `new-device-tcp-legacy/`.
The opponent is an independent WindBot test client, not a model fallback.
The live tests also caught and corrected version-info-before-name ordering
and the three padding bytes in the eight-byte native STOC_ERROR_MSG structure.
Regressions for both protocol details are included.

After a public TCP mapping is provided, verify native game access from a
separate network without the temporary SSH forward. Do not announce a working
public native TCP address before that verification.
