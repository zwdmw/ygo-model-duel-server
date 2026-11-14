"""One isolated external room worker backed by a shared GPU policy service."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import time


def now():
    return datetime.now(timezone.utc).isoformat()


# Preserve the host's original replay bytes for both supported containers.
REPLAY_CONTAINERS = (b"yrp1", b"yrp2")
REPLAY_LIMIT = 16 * 1024 * 1024
SHA256 = re.compile(r"[0-9a-f]{64}")
TOURNAMENT_DUEL_IDLE_SECONDS = 11 * 60


def validate_native_binding(native, metadata):
    """Require a checkpoint's recorded native module when it declares one."""
    runtime_sha = native.get("sha256") if isinstance(native, dict) else None
    if not SHA256.fullmatch(runtime_sha or ""):
        raise ValueError("Runtime native module hash is missing or invalid")
    expected_sha = metadata.get("native_module_sha256") if isinstance(metadata, dict) else None
    if expected_sha is not None:
        if not SHA256.fullmatch(expected_sha):
            raise ValueError("Checkpoint native module hash is invalid")
        if expected_sha != runtime_sha:
            raise ValueError(
                "Checkpoint native module differs from runtime: "
                f"{expected_sha} != {runtime_sha}"
            )
    return {
        "runtime_native_module_sha256": runtime_sha,
        "runtime_native_observation_revision": native.get("observation_revision"),
        "model_native_module_sha256": expected_sha,
    }


def replay_container(raw):
    """Check container magic and bounds without rewriting its payload."""
    magic = raw[:4]
    if not 32 <= len(raw) <= REPLAY_LIMIT or magic not in REPLAY_CONTAINERS:
        raise ValueError(f"Unsupported external replay format: {magic!r}, {len(raw)} bytes")
    return magic.decode("ascii")


def archive_replay(output, entries, raw, games, match_mode=False, nickname=None):
    """Hosts may defer the ordered game replay stream until the match ends."""
    from replay_store import archive_copy

    container = replay_container(raw)
    digest = hashlib.sha256(raw).hexdigest()
    prior = next((row for row in entries if row["sha256"] == digest), None)
    if prior is not None:
        return prior
    recorded = {row["game"] for row in entries}
    pending = [game for game in games if game["game"] not in recorded]
    if not pending:
        raise ValueError("Replay has no pending completed game")
    game = pending[0]
    game_index = game["game"]
    if match_mode and game.get("initial_deck_counts") is not None:
        from .opponent_deck import replay_decks
        names, decks = replay_decks(raw)
        counts = [[len(deck["main"]), len(deck["extra"])] for deck in decks]
        if counts != game["initial_deck_counts"]:
            raise ValueError("Replay does not match the pending game's starting deck counts")
        seat = game["our_player"]
        if nickname is not None and (names[seat] != nickname or names[1 - seat] == nickname):
            raise ValueError("Replay does not match the pending game's player identity")
    path = output / (f"server-game-{game_index:02}.yrp" if match_mode else "server.yrp")
    if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise FileExistsError("Refusing to overwrite a different original replay")
    with path.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    target = output.parent.parent / "recordings" / f"{output.name}_game{game_index:02}_complete.yrp"
    archive_copy(path, target, digest)
    entry = {
        "archive": str(target.relative_to(output.parent.parent)), "game": game_index,
        "model_seat": game["our_player"], "bytes": len(raw), "sha256": digest,
        "container": container, "association": "ordered_completed_games"}
    entries.append(entry)
    return entry


def execute(config):
    app, output = Path(config["app"]), Path(config["output"])
    sys.path[:0] = [str(app), str(app / "project")]
    os.environ.update(AGENT195M_ROOT=str(app), AGENT_MODEL_SNAPSHOT=json.dumps(config["model"]),
                      JAX_PLATFORMS="cpu", XLA_PYTHON_CLIENT_PREALLOCATE="false",
                      OMP_NUM_THREADS="2", OPENBLAS_NUM_THREADS="2")
    from replay_store import atomic_json
    from .client import ExternalClient
    from .structured import StructuredEncoder, CONTRACT, OBSERVATION_VERSION
    from .cards import CardPool
    from .vendor.netduel.cards import load_ydk

    output.mkdir(parents=True, exist_ok=True)
    random_deck = config.get("deck_source", "local") == "server_random"
    metadata = dict(config["model"])
    runtime_binding = {}
    metadata.update(session=output.name, source="external", client_profile="outbound-legacy",
                    host=config["display_host"], port=config["port"],
                    model_deck_name=config["deck_name"], human_deck_name="Unknown",
                    model_deck_sha256=(None if random_deck else
                        hashlib.sha256((output / "model.ydk").read_bytes()).hexdigest()),
                    deck_source=config.get("deck_source", "local"), nickname=config.get("nickname", "ModelBot"),
                    preference=config.get("preference", "auto"), started_at=now(),
                    selftest=config.get("selftest", False),
                    network_observation=OBSERVATION_VERSION, seed=config["seed"])
    atomic_json(output / "session.json", metadata)
    started = time.monotonic()
    old_status = {"state": "warming", "session": output.name, "updated_at": now()}
    atomic_json(output / "status.json", old_status)
    replay_entries = []
    container = ""
    client = remote_policy = None
    outcome = "incomplete"
    error = ""
    result_document = {}
    recording_started = now()
    atomic_json(output / "replay-status.json", {
        "status": "recording", "started_at": recording_started, "session": output.name})

    def status(state, **fields):
        if state != "reconnecting":
            for key in ("attempt", "retry_in", "previous_state"):
                old_status.pop(key, None)
        old_status.update(state=state, updated_at=now(), **fields)
        atomic_json(output / "status.json", old_status)

    def save_replay(raw):
        nonlocal container
        entry = archive_replay(output, replay_entries, raw, client.games, client.match_mode,
                               config.get("nickname", "ModelBot") if random_deck else None)
        container = entry["container"]
        atomic_json(output / "replay-status.json", {
            "status": "recording", "started_at": recording_started, "session": output.name,
            "recording_available": True, "replays": replay_entries, "trace_available": True})

    def game_finished(game):
        atomic_json(output / "games.json", {"games": client.games})

    def stop(signum, frame):
        if client is not None:
            client.cancelled = True
            client.surrender()
        raise InterruptedError("stopped_by_owner")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    trace = (output / "network.jsonl").open("a", encoding="utf-8", buffering=1)
    decisions = (output / "decisions.jsonl").open("a", encoding="utf-8", buffering=1)

    def journal(direction, opcode, payload, **fields):
        if trace.tell() > 8 * 1024 * 1024:
            raise ValueError("Network trace size limit reached")
        trace.write(json.dumps({"t": now(), "direction": direction, "opcode": opcode,
                                "payload": payload.hex(), **fields}) + "\n")
        trace.flush()
        os.fsync(trace.fileno())

    try:
        native = json.loads((app / "native-build.json").read_text())
        if native.get("source_hashes", {}).get("ygopro.h") != CONTRACT["source_sha256"]:
            raise ValueError("Runtime encoder contract changed; parity revalidation required")
        import worker
        from .policy_ipc import PolicyClient
        from .pool_assets import PoolAssets
        pool_config = config.get("pool")
        assets = (PoolAssets(pool_config["path"], app, pool_config["sha256"])
                  if pool_config is not None else None)
        architecture = None
        sidecar_document = {}
        sidecar = worker.CHECKPOINT.with_name(worker.CHECKPOINT.name + ".json")
        if sidecar.is_file() and not sidecar.is_symlink():
            try:
                sidecar_document = json.loads(sidecar.read_text())
                architecture = sidecar_document.get("architecture")
            except ValueError:
                architecture = None
        runtime_binding = validate_native_binding(native, sidecar_document)
        metadata.update(runtime_binding)
        atomic_json(output / "session.json", metadata)
        policy_socket = config.get("policy_socket")
        if not isinstance(policy_socket, str) or not policy_socket:
            raise ValueError("Shared GPU policy socket is unavailable")
        remote_policy = PolicyClient(policy_socket, expected=worker.IDENTITY)
        if remote_policy.identity.get("backend") != "gpu":
            raise RuntimeError("Shared outbound policy is not using the GPU backend")
        pool_metadata = remote_policy.identity.get("card_pool")
        if not isinstance(pool_metadata, dict):
            raise RuntimeError("Shared outbound policy did not report its card pool")

        metadata["card_pool"] = pool_metadata
        metadata["policy"] = {
            "backend": remote_policy.identity.get("backend"),
            "pid": remote_policy.identity.get("pid"),
            "policy_session": remote_policy.identity.get("policy_session"),
            "shared": True,
        }
        atomic_json(output / "session.json", metadata)
        pool = CardPool(assets.path / "cards.cdb" if assets else app / "project/assets/locale/en/cards.cdb",
                        assets.path / "code_list.txt" if assets else app / "project/scripts/code_list.txt")
        pool.semantic_pending = assets.pending if assets else set()
        main, extra, side = ([], [], []) if random_deck else load_ydk(output / "model.ydk")
        encoder = StructuredEncoder(pool)
        inference_seconds = 0.0

        class Policy:
            def bind(self, bound):
                self.client = bound
                remote_policy.reset()
                encoder.reset(bound.result.our_player, config["seed"])

            def observe_game_message(self, msg, body):
                if client.result.our_player in (0, 1):
                    encoder.observe_message(msg, body, client.result.turns, client.ctx.current_phase)

            def choose(self, state):
                nonlocal inference_seconds
                observation = encoder.encode_state(state, self.client.selector)
                index, elapsed = remote_policy.infer(
                    state.our_player, state.n, observation
                )
                inference_seconds += elapsed
                response = remote_policy.last_response
                unmapped = response.get("unmapped_count", 0)
                if unmapped and decisions.tell() < 4 * 1024 * 1024:
                    decisions.write(json.dumps({
                        "t": now(),
                        "event": "official_unmapped_ids",
                        "count": unmapped,
                    }) + "\n")
                decisions.write(json.dumps({"msg": state.msg, "turn": state.turn,
                    "game": len(client.games) + 1,
                    "action": index, "legal_count": state.n,
                    "inference_ms": elapsed * 1000,
                    "queue_ms": response.get("queue_ms", 0)}) + "\n")
                decisions.flush()
                os.fsync(decisions.fileno())
                status("dueling", decisions=sum(g["decisions"] for g in client.games) +
                       client.result.decisions + 1, game=len(client.games) + 1)
                return index

            def record(self, state, action, selector, index, forced):
                encoder.record(state, action, selector, index, forced)

        atomic_json(output / "startup.json", {"startup_seconds": time.monotonic() - started})
        client = ExternalClient(
            config["address"], config["port"], config.get("nickname", "ModelBot"),
            main, extra, Policy(), side=side, version=config.get("version", 0x1362),
            seed=config["seed"], timeout=TOURNAMENT_DUEL_IDLE_SECONDS,
            max_options=128, card_pool=pool,
            password=config.pop("password", ""), preference=config.get("preference", "auto"),
            deck_source=config.get("deck_source", "local"), auth_mode=config.get("auth_mode", "none"),
            account_password=config.pop("account_password", ""), game_finished=game_finished,
            journal=journal, state_change=status, save_replay=save_replay)
        result = client.run()
        outcome = client.outcome()
        settled = outcome != "incomplete"
        error = result.error
        metadata["human_seat"] = 1 - result.our_player if result.our_player in (0, 1) else None
        atomic_json(output / "session.json", metadata)
        result_document = {
            **worker.IDENTITY, **runtime_binding, "source": "external",
            "event": "complete" if settled else "incomplete",
            "terminated": settled, "truncated": not settled, "selftest": config.get("selftest", False),
            "model_seat": result.our_player, "reward": {"win": 1, "loss": -1}.get(outcome, 0),
            "winner": result.winner, "win_reason": result.win_reason,
            "decisions": sum(g["decisions"] for g in client.games) + (0 if client.terminal_seen else result.decisions),
            "inference_seconds": inference_seconds, "wall_seconds": result.seconds,
            "went_first": result.went_first, "rps_wins": client.rps_wins,
            "outcome": outcome, "error": error, "network_residuals": encoder.network_residuals,
            "card_pool": pool_metadata,
            "result_scope": "match" if client.match_mode else "duel",
            "match_ended": client.match_done, "match_kill": client.match_kill, "games": client.games,
            "replay_errors": client.replay_errors,
        }
    except BaseException as exc:
        error = "stopped_by_owner" if isinstance(exc, (InterruptedError, KeyboardInterrupt)) else (
            type(exc).__name__ + ": " + str(exc)[:250])
        result_document = {**runtime_binding, "source": "external",
                           "event": "incomplete", "outcome": "incomplete",
                           "error": error, "selftest": config.get("selftest", False),
                           "wall_seconds": time.monotonic() - started}
    finally:
        if client is not None:
            result_document.setdefault("games", client.games)
            result_document.setdefault("result_scope", "match" if client.match_mode else "duel")
        atomic_json(output / "result.json", result_document)
        if client is not None:
            client.close()
        if remote_policy is not None:
            remote_policy.close()
        entries = replay_entries
        replay_error = "; ".join(client.replay_errors) if client is not None else ""
        recording_state = "complete" if outcome in ("win", "loss", "draw") else "incomplete"
        if client is not None and len(entries) < len(client.games):
            replay_error = (replay_error + "; " if replay_error else "") + (
                f"Missing game replays: saved {len(entries)} of {len(client.games)}")
        atomic_json(output / "replay-status.json", {
            "session": output.name, "status": recording_state, "started_at": recording_started,
            "finalized_at": now(), "reason": "duel_complete" if recording_state == "complete" else error,
            "recording_available": bool(entries), "replays": entries, "trace_available": True,
            "archive_error": replay_error,
            "format": f"server_original_{container}" if entries else "network_trace_only"})
        status("finished" if recording_state == "complete" else "stopped" if error == "stopped_by_owner"
               else "failed", outcome=outcome, error=error, replay_available=bool(entries))
        for stream in (trace, decisions):
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
    print(json.dumps({"session": output.name, "outcome": outcome, "error": error}), flush=True)


if __name__ == "__main__":
    execute(json.loads(sys.stdin.buffer.read(128 * 1024)))
