"""Exact 195M JAX policy on its native observation and legal-action interface."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import time
import traceback

os.environ.setdefault("JAX_PLATFORMS", "cpu")
os.environ.setdefault("XLA_PYTHON_CLIENT_PREALLOCATE", "false")
os.environ.setdefault(
    "XLA_FLAGS", "--xla_cpu_multi_thread_eigen=false intra_op_parallelism_threads=2"
)

import flax
import jax
import jax.numpy as jnp
import numpy as np
import ygoenv
from semantic_loader import inject_semantic_constants, load_structured_semantics
from ygoai.rl.env import EnvPreprocess
from ygoai.rl.jax.agent import RNNAgent
from ygoenv.ygopro import init_module
from policy_ipc import (PolicyClient, observation_schema, send_frame,
                        serve_connection)


ROOT = Path(os.environ.get("AGENT195M_ROOT", "/app"))
PROJECT = ROOT / "project"
CHECKPOINT = ROOT / "model/195M_step195035136.flax_model"
MODEL_DECK = PROJECT / "assets/deck/BestowedDragon.ydk"


def verify_snapshot():
    manifest = json.loads((ROOT / "manifest.json").read_text())
    native_build = json.loads((ROOT / "native-build.json").read_text())
    for name, expected in manifest["files"].items():
        if name.endswith((".flax_model", ".npz", ".so", "code_list.txt", ".cdb")):
            if name == native_build["module"]:
                expected = native_build["sha256"]
            actual = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            if actual != expected:
                raise RuntimeError(f"Snapshot integrity failure: {name}")


def make_env(args, mode, seat):
    options = dict(
        task_id="YGOPro-v1", env_type="gymnasium", num_envs=1, num_threads=1,
        thread_affinity_offset=-1, seed=args.seed, player=seat,
        deck1="Human" if seat == 1 else "Agent195M",
        deck2="Agent195M" if seat == 1 else "Human",
        deck_schedule="independent", anchor_deck="", anchor_probability_percent=0,
        max_options=128, max_cards=80, max_steps=1000, n_history_actions=32,
        async_reset=False, greedy_reward=False, play_mode=mode,
        timeout=1800, oppo_info=False, verbose=False,
    )
    if mode == "windbot":
        options.update(
            record=True,
            windbot_root=str(ROOT),
            windbot_executable=str(ROOT / "relay.py"),
            windbot_executor="Human",
            windbot_deck_file=str(args.human_deck),
            windbot_cards_db=str(PROJECT / "assets/locale/en/cards.cdb"),
            windbot_log_path=str(args.output / "relay"),
            windbot_seed=args.seed,
            windbot_timeout_ms=900000,
        )
    env = ygoenv.make(**options)
    env.num_envs = 1
    return EnvPreprocess(env, skip_mask=True)


def init_decks(args):
    init_module(
        str(PROJECT / "assets/locale/en/cards.cdb"),
        str(PROJECT / "scripts/code_list.txt"),
        {"Agent195M": str(args.model_deck), "Human": str(args.human_deck)},
    )


def load_policy(args):
    verify_snapshot()
    init_decks(args)
    tables, shape, metadata = load_structured_semantics(
        str(PROJECT / "assets/structured/frozen_semantics_v1.npz"),
        str(PROJECT / "scripts/code_list.txt"),
    )
    template_env = make_env(args, "self", 0)
    try:
        sample, _ = template_env.reset()
    finally:
        template_env.close()
    sample = jax.tree.map(
        lambda x: None if x is None else jnp.asarray(x), sample,
        is_leaf=lambda x: x is None,
    )
    agent = RNNAgent(structured=True, semantic_shape=shape)
    template = flax.core.unfreeze(
        agent.init(jax.random.PRNGKey(0), sample, agent.init_rnn_state(1))
    )
    inject_semantic_constants(template, tables)
    variables = flax.core.unfreeze(
        flax.serialization.from_bytes(template, CHECKPOINT.read_bytes())
    )
    inject_semantic_constants(variables, tables)
    variables = jax.device_put(flax.core.freeze(variables))

    @jax.jit
    def infer(state, obs):
        next_state, logits = agent.apply(variables, obs, state)[:2]
        return next_state, jnp.argmax(logits, axis=-1), logits

    warmup = infer(agent.init_rnn_state(1), sample)
    jax.block_until_ready(warmup[1])
    print(json.dumps({"event": "model_ready", "steps": 195035136,
                      "backend": jax.default_backend(), "semantic_shape": shape}), flush=True)
    return agent, infer, observation_schema(sample)


def serve_policy(args):
    started = time.monotonic()
    agent, infer, schema = load_policy(args)
    address = args.serve_policy
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(address))
        os.chmod(address, 0o600)
        listener.listen(2)
        print(json.dumps({"event": "policy_service_ready", "pid": os.getpid(),
                          "seconds": time.monotonic() - started}), flush=True)
        session = 0
        while True:
            connection, _ = listener.accept()
            session += 1
            # State belongs to this connection only, never to the persistent weights.
            states = [jax.device_put(agent.init_rnn_state(1)) for _ in range(2)]

            def predict(actor, count, observation):
                tick = time.monotonic()
                states[actor], action, logits = infer(states[actor], observation)
                jax.block_until_ready(action)
                elapsed = time.monotonic() - tick
                index = int(np.asarray(action)[0])
                if not 0 <= index < count or not np.isfinite(np.asarray(logits)).all():
                    raise RuntimeError("Invalid policy output; no heuristic fallback is used")
                return {"action": index, "inference_ms": elapsed * 1000}

            with connection:
                connection.settimeout(900)
                try:
                    serve_connection(connection, predict, schema, {
                        "checkpoint_steps": 195035136, "pid": os.getpid(),
                        "policy_session": session,
                    })
                except (EOFError, ConnectionError, socket.timeout):
                    pass
                except Exception as error:
                    traceback.print_exc()
                    try:
                        send_frame(connection, json.dumps({"error": str(error)}).encode("ascii"))
                    except OSError:
                        pass


def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "replay").mkdir(exist_ok=True)
    started = time.monotonic()
    deck_identity = {
        actor + "_deck_sha256": hashlib.sha256(path.read_bytes()).hexdigest()
        for actor, path in (("human", args.human_deck), ("model", args.model_deck))
    }
    print(json.dumps({"event": "decks_loaded", **deck_identity}), flush=True)
    remote = None
    if args.policy_socket:
        init_decks(args)
        remote = PolicyClient(args.policy_socket)
        print(json.dumps({"event": "policy_connected", **remote.identity}), flush=True)
    else:
        agent, infer, _ = load_policy(args)
    if args.warmup_only:
        print(json.dumps({"event": "warmup_complete", "seconds": time.monotonic() - started}))
        return
    seat = 1 - args.human_seat
    env = None
    states = None if remote else [jax.device_put(agent.init_rnn_state(1)) for _ in range(2)]
    decisions = 0
    inference_seconds = 0.0
    try:
        env = make_env(args, "self" if args.selftest else "windbot", seat)
        obs, info = env.reset()
        with (args.output / "decisions.jsonl").open("w") as log:
            while True:
                actor = int(np.asarray(info["to_play"])[0])
                if not args.selftest and actor != seat:
                    raise RuntimeError("Human decision escaped the native bridge")
                count = int(np.asarray(info["num_options"])[0])
                if remote:
                    index, elapsed = remote.infer(actor, count, obs)
                else:
                    tick = time.monotonic()
                    states[actor], action, logits = infer(states[actor], obs)
                    jax.block_until_ready(action)
                    elapsed = time.monotonic() - tick
                    index = int(np.asarray(action)[0])
                    if not 0 <= index < count or not np.isfinite(np.asarray(logits)).all():
                        raise RuntimeError("Invalid policy output; no heuristic fallback is used")
                inference_seconds += elapsed
                log.write(json.dumps({
                    "decision": decisions, "actor": actor, "action": index,
                    "legal_count": count, "inference_ms": elapsed * 1000,
                }) + "\n")
                log.flush()
                obs, reward, terminated, truncated, info = env.step(
                    np.asarray([index], dtype=np.int32)
                )
                decisions += 1
                if bool(np.asarray(terminated)[0] or np.asarray(truncated)[0]):
                    result = {
                        "event": "complete", "checkpoint_steps": 195035136,
                        "selftest": args.selftest, "model_seat": seat,
                        "decisions": decisions, "reward": float(np.asarray(reward)[0]),
                        "win_reason": int(np.asarray(info["win_reason"])[0]),
                        "inference_seconds": inference_seconds,
                        "wall_seconds": time.monotonic() - started,
                        **deck_identity,
                    }
                    if remote:
                        result["policy_identity"] = remote.identity
                    (args.output / "result.json").write_text(json.dumps(result, indent=2))
                    print(json.dumps(result), flush=True)
                    break
    finally:
        if remote:
            remote.close()
        if env is not None:
            env.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--human-deck", type=Path, default=MODEL_DECK)
    parser.add_argument("--model-deck", type=Path, default=MODEL_DECK)
    parser.add_argument("--human-seat", type=int, choices=(0, 1), default=0)
    parser.add_argument("--seed", type=int, default=195035136)
    parser.add_argument("--output", type=Path,
                        default=Path(os.environ.get("AGENT195M_DATA", "/data")) / "warmup")
    parser.add_argument("--warmup-only", action="store_true")
    parser.add_argument("--selftest", action="store_true")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--policy-socket", type=Path)
    modes.add_argument("--serve-policy", type=Path)
    args = parser.parse_args()
    if args.policy_socket and args.warmup_only:
        parser.error("--warmup-only cannot use --policy-socket")
    if args.serve_policy:
        serve_policy(args)
    else:
        run(args)
