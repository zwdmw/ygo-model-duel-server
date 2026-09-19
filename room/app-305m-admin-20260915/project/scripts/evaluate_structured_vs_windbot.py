from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

os.environ.setdefault("XLA_PYTHON_CLIENT_PREALLOCATE", "false")

import flax
import jax
import jax.numpy as jnp
import numpy as np
import ygoenv

from scripts.cleanba import (
    inject_semantic_constants,
    load_structured_semantics,
)
from ygoai.rl.env import EnvPreprocess
from ygoai.rl.jax.agent import RNNAgent
from ygoai.utils import get_root_directory


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = Path("/root/autodl-tmp/ygo-agent-main-gpu-20260824")
DEFAULT_CHECKPOINT = (
    RUNTIME_ROOT
    / "experiments/r0-structured-uniform-resume-50m-to1000m-20260826"
    / "checkpoints/20260826_104M.flax_model"
)
DEFAULT_WINDBOT_ROOT = Path(
    "/root/autodl-tmp/ygo-transformer/"
    "windbot-seeded-rng-direct-v1/stock-opponent/Release"
)
DEFAULT_CARDS_DB = Path(
    "/root/autodl-tmp/ygo-transformer/"
    "a100-temporal-deploy-20260728/repository/project/mdpro/"
    "Data/locales/en-US/cards.cdb"
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate a native structured JAX policy against WindBot while "
            "keeping observation construction inside ygoenv."
        )
    )
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=DEFAULT_CHECKPOINT,
    )
    parser.add_argument(
        "--jax-deck",
        type=Path,
        default=PROJECT_ROOT / "assets/deck/BlueEyes.ydk",
    )
    parser.add_argument(
        "--windbot-deck",
        type=Path,
        default=DEFAULT_WINDBOT_ROOT / "Decks/AI_BlueEyes.ydk",
    )
    parser.add_argument(
        "--windbot-root",
        type=Path,
        default=DEFAULT_WINDBOT_ROOT,
    )
    parser.add_argument("--windbot-executor", default="Blue-Eyes")
    parser.add_argument(
        "--cards-db",
        type=Path,
        default=DEFAULT_CARDS_DB,
    )
    parser.add_argument(
        "--code-list",
        type=Path,
        default=PROJECT_ROOT / "scripts/code_list.txt",
    )
    parser.add_argument(
        "--semantic-cache",
        type=Path,
        default=PROJECT_ROOT / "assets/structured/frozen_semantics_v1.npz",
    )
    parser.add_argument("--games", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260909)
    parser.add_argument("--max-options", type=int, default=128)
    parser.add_argument("--max-steps", type=int, default=1000)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--windbot-timeout-ms", type=int, default=30000)
    parser.add_argument(
        "--replay-dir",
        type=Path,
        default=PROJECT_ROOT / "Replay",
        help="Directory where completed duel YRP files are saved.",
    )
    parser.add_argument(
        "--verbose-env",
        action="store_true",
        help="Print native OCGCore decisions and state transitions.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=PROJECT_ROOT / "results/104m-vs-windbot-blueeyes.json",
    )
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def wilson_interval(successes: int, total: int) -> list[float] | None:
    if total <= 0:
        return None
    z = 1.959963984540054
    probability = successes / total
    z2 = z * z
    denominator = 1 + z2 / total
    center = (probability + z2 / (2 * total)) / denominator
    radius = (
        z
        * math.sqrt(
            probability * (1 - probability) / total
            + z2 / (4 * total * total)
        )
        / denominator
    )
    return [max(0.0, center - radius), min(1.0, center + radius)]


def validate_paths(args: argparse.Namespace) -> None:
    files = (
        args.checkpoint,
        args.jax_deck,
        args.windbot_deck,
        args.cards_db,
        args.code_list,
        args.semantic_cache,
        args.windbot_root / "WindBot.exe",
    )
    for path in files:
        if not path.is_file():
            raise FileNotFoundError(path)
    if args.games <= 0 or args.games % 2:
        raise ValueError("--games must be a positive even number")


def initialize_ygoenv(args: argparse.Namespace) -> None:
    from ygoenv.ygopro import init_module

    environment_db = (
        Path(get_root_directory()) / "assets/locale/en/cards.cdb"
    )
    init_module(
        str(environment_db),
        str(args.code_list),
        {
            "JAXBlueEyes": str(args.jax_deck),
            "WindBotBlueEyes": str(args.windbot_deck),
        },
    )


def make_base_env(
    args: argparse.Namespace,
    *,
    seed: int,
    jax_seat: int,
    play_mode: str,
    log_prefix: Path | None = None,
    windbot_seed: int | None = None,
):
    deck1 = "JAXBlueEyes" if jax_seat == 0 else "WindBotBlueEyes"
    deck2 = "WindBotBlueEyes" if jax_seat == 0 else "JAXBlueEyes"
    options: dict[str, Any] = {
        "task_id": "YGOPro-v1",
        "env_type": "gymnasium",
        "num_envs": 1,
        "num_threads": 1,
        "seed": seed,
        "player": jax_seat,
        "deck1": deck1,
        "deck2": deck2,
        "deck_schedule": "independent",
        "anchor_deck": "",
        "anchor_probability_percent": 0,
        "max_options": args.max_options,
        "max_cards": 80,
        "max_steps": args.max_steps,
        "n_history_actions": 32,
        "async_reset": False,
        "greedy_reward": False,
        "play_mode": play_mode,
        "timeout": args.timeout,
        "oppo_info": False,
        "verbose": args.verbose_env,
    }
    if play_mode == "windbot":
        if log_prefix is None or windbot_seed is None:
            raise ValueError("WindBot mode requires a log prefix and seed")
        options.update(
            {
                "record": True,
                "replay_dir": str(args.replay_dir),
                "windbot_root": str(args.windbot_root),
                "windbot_executable": str(
                    args.windbot_root / "WindBot.exe"
                ),
                "windbot_executor": args.windbot_executor,
                "windbot_deck_file": str(args.windbot_deck),
                "windbot_cards_db": str(args.cards_db),
                "windbot_log_path": str(log_prefix),
                "windbot_seed": windbot_seed,
                "windbot_timeout_ms": args.windbot_timeout_ms,
            }
        )
    env = ygoenv.make(**options)
    env.num_envs = 1
    return EnvPreprocess(env, skip_mask=True)


def load_model(args: argparse.Namespace):
    semantic_tables, semantic_shape, semantic_metadata = (
        load_structured_semantics(
            str(args.semantic_cache),
            str(args.code_list),
        )
    )
    template_env = make_base_env(
        args,
        seed=args.seed ^ 0x42A1,
        jax_seat=0,
        play_mode="self",
    )
    try:
        sample_observation, _ = template_env.reset()
    finally:
        template_env.close()
    sample_observation = jax.tree.map(
        lambda value: None if value is None else jnp.asarray(value),
        sample_observation,
        is_leaf=lambda value: value is None,
    )

    agent = RNNAgent(
        structured=True,
        semantic_shape=semantic_shape,
    )
    key = jax.random.PRNGKey(args.seed)
    template = flax.core.unfreeze(
        agent.init(
            key,
            sample_observation,
            agent.init_rnn_state(1),
        )
    )
    inject_semantic_constants(template, semantic_tables)
    variables = flax.serialization.from_bytes(
        template,
        args.checkpoint.read_bytes(),
    )
    variables = flax.core.unfreeze(variables)
    inject_semantic_constants(variables, semantic_tables)
    variables = jax.device_put(flax.core.freeze(variables))

    @jax.jit
    def infer(rstate, observation):
        next_rstate, logits = agent.apply(
            variables,
            observation,
            rstate,
        )[:2]
        probabilities = jax.nn.softmax(logits, axis=-1)
        action = jnp.argmax(logits, axis=-1)
        top1 = jnp.max(probabilities, axis=-1)
        entropy = -jnp.sum(
            jnp.where(
                probabilities > 0,
                probabilities * jax.nn.log_softmax(logits, axis=-1),
                0,
            ),
            axis=-1,
        )
        finite = jnp.all(jnp.isfinite(logits))
        return next_rstate, action, top1, entropy, finite

    warmup = infer(
        agent.init_rnn_state(1),
        sample_observation,
    )
    jax.block_until_ready(warmup[1])
    return agent, infer, semantic_shape, semantic_metadata


def find_windbot_log(prefix: Path) -> Path | None:
    candidates = sorted(
        prefix.parent.glob(prefix.name + ".*.log"),
        key=lambda path: path.stat().st_mtime_ns,
    )
    return candidates[-1] if candidates else None


def windbot_log_stats(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {
            "path": None,
            "sha256": None,
            "decision_requests": 0,
            "reported_result": None,
        }
    text = path.read_text(encoding="utf-8", errors="replace")
    results = re.findall(
        r"Duel finished against .*?, result: (Win|Lose|Draw)",
        text,
        re.IGNORECASE,
    )
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "decision_requests": len(
            re.findall(r"\[AISelection\].*stage=received", text)
        ),
        "reported_result": results[-1].lower() if results else None,
    }


def replay_files(directory: Path) -> set[Path]:
    if not directory.is_dir():
        return set()
    return {path.resolve() for path in directory.glob("*.yrp")}


def find_new_replay(
    before: set[Path],
    directory: Path,
) -> Path | None:
    created = sorted(
        replay_files(directory) - before,
        key=lambda path: path.stat().st_mtime_ns,
    )
    if not created:
        return None
    if len(created) > 1:
        raise RuntimeError(
            "WindBot replay directory received multiple new files: "
            + ", ".join(str(path) for path in created)
        )
    return created[0]


def replay_destination(
    args: argparse.Namespace,
    *,
    game_index: int,
    seat_label: str,
    seat: int,
    engine_seed: int,
    windbot_seed: int,
) -> Path:
    components = (
        args.output.stem,
        "vs",
        args.windbot_executor,
        f"game-{game_index:04d}",
        f"{seat_label}-seat-{seat}",
        f"engine-seed-{engine_seed}",
        f"windbot-seed-{windbot_seed}",
    )
    stem = "-".join(
        re.sub(r"[^A-Za-z0-9_.-]+", "-", component).strip("-")
        for component in components
    )
    candidate = args.replay_dir / f"{stem}.yrp"
    suffix = 1
    while candidate.exists():
        candidate = args.replay_dir / f"{stem}-run-{suffix:02d}.yrp"
        suffix += 1
    return candidate


def archive_replay(
    args: argparse.Namespace,
    *,
    before: set[Path],
    game_index: int,
    seat_label: str,
    seat: int,
    engine_seed: int,
    windbot_seed: int,
) -> dict[str, Any]:
    replay_path = find_new_replay(before, args.replay_dir)
    if replay_path is None:
        raise RuntimeError(
            "WindBot duel completed without producing a replay in "
            f"{args.replay_dir}"
        )
    destination = replay_destination(
        args,
        game_index=game_index,
        seat_label=seat_label,
        seat=seat,
        engine_seed=engine_seed,
        windbot_seed=windbot_seed,
    )
    shutil.move(str(replay_path), destination)
    return {
        "replay": str(destination),
        "replay_size": destination.stat().st_size,
        "replay_sha256": sha256_file(destination),
    }


def run_game(
    args: argparse.Namespace,
    agent: RNNAgent,
    infer,
    *,
    game_index: int,
    pair_index: int,
    jax_seat: int,
) -> dict[str, Any]:
    engine_seed = args.seed + pair_index
    windbot_seed = args.seed + 1_000_000 + pair_index
    args.replay_dir.mkdir(parents=True, exist_ok=True)
    replay_before = replay_files(args.replay_dir)
    log_prefix = (
        args.output.parent
        / f"{args.output.stem}-windbot-logs"
        / f"game-{game_index:04d}-jax-seat-{jax_seat}"
    )
    log_prefix.parent.mkdir(parents=True, exist_ok=True)
    for stale in log_prefix.parent.glob(log_prefix.name + ".*.log"):
        stale.unlink()

    env = make_base_env(
        args,
        seed=engine_seed,
        jax_seat=jax_seat,
        play_mode="windbot",
        log_prefix=log_prefix,
        windbot_seed=windbot_seed,
    )
    rstate = jax.device_put(agent.init_rnn_state(1))
    model_seconds = 0.0
    environment_seconds = 0.0
    top1_sum = 0.0
    entropy_sum = 0.0
    decisions = 0
    started = time.monotonic()
    game: dict[str, Any] | None = None
    try:
        observation, info = env.reset()
        while decisions < args.max_steps:
            to_play = int(np.asarray(info["to_play"])[0])
            if to_play != jax_seat:
                raise RuntimeError(
                    "WindBot decision escaped the bridge: "
                    f"to_play={to_play}, jax_seat={jax_seat}"
                )
            num_options = int(np.asarray(info["num_options"])[0])
            if num_options <= 0:
                raise RuntimeError("JAX observation has no legal actions")
            jax_observation = jax.tree.map(
                lambda value: (
                    None if value is None else jnp.asarray(value)
                ),
                observation,
                is_leaf=lambda value: value is None,
            )
            model_started = time.monotonic()
            rstate, action, top1, entropy, finite = infer(
                rstate,
                jax_observation,
            )
            jax.block_until_ready(action)
            model_seconds += time.monotonic() - model_started
            if not bool(np.asarray(finite)):
                raise FloatingPointError("model produced non-finite logits")
            action_index = int(np.asarray(action)[0])
            if not 0 <= action_index < num_options:
                raise RuntimeError(
                    f"model action {action_index} is outside "
                    f"{num_options} legal actions"
                )
            top1_sum += float(np.asarray(top1)[0])
            entropy_sum += float(np.asarray(entropy)[0])

            env_started = time.monotonic()
            result = env.step(np.asarray([action_index], dtype=np.int32))
            environment_seconds += time.monotonic() - env_started
            observation, reward, terminated, truncated, info = result
            done = bool(
                np.asarray(terminated)[0] or np.asarray(truncated)[0]
            )
            decisions += 1
            if not done:
                continue
            terminal_reward = float(np.asarray(reward)[0])
            outcome = (
                "win"
                if terminal_reward > 0
                else "loss"
                if terminal_reward < 0
                else "draw"
            )
            game = {
                "game_index": game_index,
                "pair_index": pair_index,
                "jax_seat": jax_seat,
                "engine_seed": engine_seed,
                "windbot_seed": windbot_seed,
                "outcome": outcome,
                "reward": terminal_reward,
                "win_reason": int(np.asarray(info["win_reason"])[0]),
                "jax_decisions": decisions,
                "mean_top1_probability": top1_sum / decisions,
                "mean_entropy": entropy_sum / decisions,
                "model_seconds": model_seconds,
                "environment_seconds": environment_seconds,
                "wall_seconds": time.monotonic() - started,
            }
            break
        if game is None:
            raise RuntimeError(
                f"game exceeded {args.max_steps} JAX decisions"
            )
    finally:
        env.close()
    if game is None:
        raise RuntimeError("WindBot duel ended without a result")
    game.update(archive_replay(
        args,
        before=replay_before,
        game_index=game_index,
        seat_label="jax",
        seat=jax_seat,
        engine_seed=engine_seed,
        windbot_seed=windbot_seed,
    ))
    return game


def summarize(games: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(game["outcome"] for game in games)
    decisive = counts["win"] + counts["loss"]
    seat_summaries = {}
    for seat in (0, 1):
        seat_games = [game for game in games if game["jax_seat"] == seat]
        seat_counts = Counter(game["outcome"] for game in seat_games)
        seat_decisive = seat_counts["win"] + seat_counts["loss"]
        seat_summaries[str(seat)] = {
            "games": len(seat_games),
            "wins": seat_counts["win"],
            "draws": seat_counts["draw"],
            "losses": seat_counts["loss"],
            "win_rate": (
                seat_counts["win"] / seat_decisive
                if seat_decisive
                else None
            ),
        }
    return {
        "games": len(games),
        "wins": counts["win"],
        "draws": counts["draw"],
        "losses": counts["loss"],
        "decisive_games": decisive,
        "win_rate": counts["win"] / decisive if decisive else None,
        "win_rate_ci95": wilson_interval(counts["win"], decisive),
        "seat": seat_summaries,
        "mean_jax_decisions": float(
            np.mean([game["jax_decisions"] for game in games])
        ),
        "mean_wall_seconds": float(
            np.mean([game["wall_seconds"] for game in games])
        ),
        "mean_model_milliseconds_per_decision": (
            1000
            * sum(game["model_seconds"] for game in games)
            / sum(game["jax_decisions"] for game in games)
        ),
    }


def main() -> None:
    args = parse_args()
    for name in (
        "checkpoint",
        "jax_deck",
        "windbot_deck",
        "windbot_root",
        "cards_db",
        "code_list",
        "semantic_cache",
        "replay_dir",
        "output",
    ):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    validate_paths(args)
    initialize_ygoenv(args)
    agent, infer, semantic_shape, semantic_metadata = load_model(args)

    payload: dict[str, Any] = {
        "schema": "ygo-structured-vs-windbot-eval/v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "architecture": {
            "engine": "single native ygoenv OCGCore duel",
            "jax_observation": "native ygoenv tensors",
            "windbot_input": (
                "player-filtered STOC_GAME_MSG plus decision-time "
                "field refresh"
            ),
            "windbot_output": "raw CTOS_RESPONSE injected into OCGCore",
        },
        "configuration": {
            "games": args.games,
            "seed": args.seed,
            "policy": "deterministic_argmax",
            "max_options": args.max_options,
            "max_steps": args.max_steps,
            "replay_dir": str(args.replay_dir),
            "verbose_env": args.verbose_env,
            "semantic_shape": semantic_shape,
            "semantic_schema": semantic_metadata.get("schema"),
        },
        "artifacts": {
            "checkpoint": {
                "path": str(args.checkpoint.resolve()),
                "sha256": sha256_file(args.checkpoint),
            },
            "jax_deck": {
                "path": str(args.jax_deck.resolve()),
                "sha256": sha256_file(args.jax_deck),
            },
            "windbot_deck": {
                "path": str(args.windbot_deck.resolve()),
                "sha256": sha256_file(args.windbot_deck),
            },
            "windbot_executable": {
                "path": str(
                    (args.windbot_root / "WindBot.exe").resolve()
                ),
                "sha256": sha256_file(
                    args.windbot_root / "WindBot.exe"
                ),
                "executor": args.windbot_executor,
            },
            "cards_db": {
                "path": str(args.cards_db.resolve()),
                "sha256": sha256_file(args.cards_db),
            },
            "code_list": {
                "path": str(args.code_list.resolve()),
                "sha256": sha256_file(args.code_list),
            },
            "semantic_cache": {
                "path": str(args.semantic_cache.resolve()),
                "sha256": sha256_file(args.semantic_cache),
            },
        },
        "games": [],
    }
    atomic_write_json(args.output, payload)

    for game_index in range(args.games):
        pair_index = game_index // 2
        jax_seat = game_index % 2
        game = run_game(
            args,
            agent,
            infer,
            game_index=game_index,
            pair_index=pair_index,
            jax_seat=jax_seat,
        )
        prefix = (
            args.output.parent
            / f"{args.output.stem}-windbot-logs"
            / f"game-{game_index:04d}-jax-seat-{jax_seat}"
        )
        game["windbot_log"] = windbot_log_stats(
            find_windbot_log(prefix)
        )
        payload["games"].append(game)
        payload["summary"] = summarize(payload["games"])
        atomic_write_json(args.output, payload)
        print(
            f"game={game_index + 1}/{args.games} "
            f"jax_seat={jax_seat} outcome={game['outcome']} "
            f"decisions={game['jax_decisions']} "
            f"wall_seconds={game['wall_seconds']:.2f}",
            flush=True,
        )

    summary = payload["summary"]
    print(
        f"complete W-D-L={summary['wins']}-{summary['draws']}-"
        f"{summary['losses']} win_rate={summary['win_rate']}",
        flush=True,
    )
    print(f"output={args.output}", flush=True)


if __name__ == "__main__":
    main()
