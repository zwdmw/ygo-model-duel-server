# External Duel Client

Owner-started, one-session-at-a-time client for compatible YGOPro TCP rooms.
The management page is `/#servers`.

1. Save the host, port, verified model, and model deck in a server preset.
2. Select a protocol and optional one-session password.
3. Start the external duel. Saving a preset alone never connects.
4. Stop explicitly to surrender/disconnect and finalize incomplete records.

`auto` identifies Tenpai from the submitted main deck, not the file name:
at least two distinct Tenpai core cards and four total core copies.
When the server asks the RPS winner for turn order, Tenpai chooses second.
An opponent who wins RPS remains free to choose. Explicit first/second/random
settings override the automatic preference.

## Runtime

- Each session loads the selected verified checkpoint on CPU in a separate process.
- The serving room's checkpoint, recurrent state, default deck, and training job
  are not changed.
- Public unicast destinations only; resolved addresses are pinned before launch.
- Authentication, CSRF, preset revision checking, bounded downloads, and a
  single-active-session guard apply to the management API.
- Passwords are sent through the subprocess stdin, never command arguments.
  JOIN_GAME is redacted from the network journal.
- YGOPro2 1.036.2 and the supported legacy wire format are explicit choices.
  Unsupported match/tag rooms are rejected; no auto-rematch or side-decking.

## Recordings And Statistics

Files are under `data/outbound/sessions`, separate from hosted-room statistics.
Network messages and decisions are flushed and fsynced as they arrive.
Original `STOC_REPLAY` payloads are saved unchanged when supplied by the host,
in whichever container that host recorded: `yrp1` for the legacy wire format,
`yrp2` for YGOPro2 hosts such as YGO233. An unknown magic, a payload below 32
bytes, or one above 16 MiB is refused with an explicit error instead of being
stored under a guessed container.
The receiver drains replay packets after the authoritative win message.
No missing opponent choices are invented to fabricate a replay.

An interruption is recorded as incomplete. If the host has not supplied a YRP,
only the real network journal is available; it is not a playable YRP.
Only authoritative completed wins/losses/draws enter the win-rate denominator.
Selftests are excluded. Download filenames distinguish complete and incomplete.

## Model-Input Limits

This is an observable-network adapter, not a claim of full native encoder parity.
Structured action/history/IR fields follow the pinned runtime contract.
The protocol and board tracker are vendored from the user's local Mirrorforce
project; their source hashes are in `vendor/sources.json`.

A network player cannot recover shuffled deck order, an opponent's private
selection prompts, or all native engine queries. Opponent public actions are
reconstructed from observable messages. Own-deck selection candidates now bind
to canonical rows of the actual card ID supplied in the prompt, not to a
seeded approximation of hidden shuffle positions. Distinct copies reserve
separate rows across all substeps of a selection. Missing card identities or
insufficient copies fail explicitly; undisclosed identities stay anonymous.
Prompt-local numeric references are not reused for unrelated history events.
These differences are included in result metadata.
They can change policy quality relative to native evaluation. Controlled duel
completion proves integration, not equal playing strength.

This corrects candidate identity; it intentionally does not reproduce the
native legacy deck-position lookup. The inspected training source discards
the candidate code in non-verbose `MSG_SELECT_CARD`, then resolves the prompt
spec through engine-position rows. Training/runtime alignment and strength
evaluation remain separate work, not a claim made by this correction.
New sessions record the seed and
`network_observation=structured-observable-v2-deck-identity`.

Unknown card data, deck-count disagreement, invalid model actions, unsupported
deck/grave swaps, and schema/contract mismatches stop with an explicit error.
There is no heuristic action fallback.

External deck validation rejects Token cards in every submitted section before
loading a model or connecting. The old builtin deck contains a side-deck Token
and is not suitable for external submission. Use an edited library deck.
The target host still makes the authoritative card-pool and banlist decision.

The YGO233 preset was changed to the existing
`Tenpai OCG2026.07 going-second` library entry after the 2026-09-17 deck rejection;
the hosted-room builtin and default were not edited. Actual host messages state
that blank passwords enter single mode, `M` match mode, and `T` tag mode.
The host also discourages competitive decks in its random single queue.
Use an agreed room and disclose that the opponent is a model.
Match remains unsupported by this release.

## External Card Pool

The following describes the first extension's historical build. The active
`card-pools/current.json` pointer and its manifest determine current readiness;
later revisions can supply effects that were pending in this initial build.

The external-only pool `official-network-20260917` contains 15,014 card codes,
up from 14,949, including alternate artwork and Token identities. This is not
a count of unique tournament-legal cards. The latest inspected database commit
is `mycard/ygopro-database@5f4f1d61f168d797c4c181da951a20e8540ba777`.
The script commit is `mycard/ygopro-scripts@5864b6f6e58d49738e0996b94e96655b51f420bf`.

Of 65 appended codes, 32 have complete extractor inputs; 33 have no
corresponding effect script in the inspected upstreams and remain pending.
The additional pinned ProjectIgnis tree was inspected, but did not provide
any of the 33 missing exact passcodes. Do not infer a match from similar names.
The manifest records the complete pending list and source/file hashes.

`build_pool.py` produces immutable CDB, code-list, and semantic assets.
`pool_assets.py` checks hashes, original index prefix, dimensions, zero hidden
sentinel, finite values, and metadata index hashes. `pool_policy.py` verifies
the selected checkpoint and restores every learned tensor unchanged while
injecting extended frozen semantic constants. No new learned card embedding
is generated. Old Lua semantic rows can change with updated scripts (101 in
this release), so unchanged weights do not imply identical old-card decisions.

Only the external client reads `card-pools/current.json`. The native room,
worker, card database, scripts, training, and checkpoints are not replaced.
Management re-reads that pointer and its manifest on every request, so activating
a rebuilt pool - or repairing a damaged one - takes effect without an agent
restart. `PoolCache` re-validates the assets only when the pointer or manifest
changed; while the active pool fails validation a duel refuses to start, and the
page keeps rendering the last good pool with the reason attached.
The target host executes card scripts; the external process uses its original
native engine only to obtain a schema/sample with a known builtin deck.
Thus cards accepted externally are not automatically executable by the hosted
room or local offline clients. The editor marks external-only and pending cards.

Pending cards are rejected before launching an own deck. If a pending or
unknown card becomes visible during play, execution stops explicitly, saves
the real received trace, and labels the result incomplete. Hidden opponent
cards stay hidden. There is no silent zero-feature or heuristic-action fallback.
These checks do not guarantee support for future cards, custom server cards,
new network operations, or the target host's banlist, and do not establish
playing strength on newly added cards.

Activation uses `admin/deploy_card_pool.py`: it requires a matching real-policy
proof and controlled completed duel, checks live file hashes, preserves newer
replay fixes, and restarts only management when no external duel is active.
`--wait` waits up to 180 seconds for an idle window. Backups and activation
proofs are retained; the deployment refuses concurrent source changes. The
restart is a convenience, not a correctness requirement: a pool activated without
it is picked up on the next request, and the previous failure mode - sessions
silently judged against the superseded pool - is gone.

## Verification

Local protocol/feature tests:

```powershell
python -m unittest outbound.test_outbound outbound.test_pool outbound.test_deck_identity outbound.test_pool_refresh -v
```

`verify_deck_identity.py` reconstructs recorded network messages entirely
offline, checks exact historical response bytes, and verifies candidate IDs
and references across seven encoder seeds. Its optional `--inference` check
loads unchanged learned parameters and checks finite legal outputs on the
recorded trajectories. It neither plays new games nor establishes new wins.

Management tests live in `admin/test_outbound_admin.py`; browser start/stop tests
are `admin/test_outbound_browser.cjs` against an isolated fixture.
`selftest.py` starts a private loopback room and a separate real model client,
then verifies that the production policy PID/checkpoint did not change.
No public lobby is used for development tests.

Controlled acceptance on 2026-09-17 used the serving 640M-step checkpoint:
new-protocol seat 0 completed 29 decisions; legacy seat 1 completed 88 decisions.
Both were losses in isolated model-vs-model tests, not human-play measurements.
The controlled room did not supply network YRP packets; replay draining is
covered separately by protocol tests, not claimed as live-server replay proof.

Expanded-pool validation additionally exercised all 32 ready new codes from
both own-card and visible-opponent-card perspectives: 64 synthetic observations
per checkpoint, on 195M-step and serving 640M-step snapshots. Encoded card/action
IDs matched the appended rows and inference returned finite legal outputs.
These are input/inference checks, not full card-effect execution tests.
A loopback Tenpai duel using the expanded 640M-step policy completed 24
decisions and a normal loss; production policy identity remained unchanged.

The old-pool live session `20260917-181145-8535` failed after 32 model decisions
when code 1498450 became visible. `verify_failed_trace.py` replays the actual
received packets and historical responses entirely offline. With the expanded
pool, the exact previously failing turn-2 chain prompt encoded card ID 14950
and the unchanged 640M-step weights produced a finite legal answer.
This verifies the reported failing position, not a continuation of that match.
No public matchmaking is performed by this check.
