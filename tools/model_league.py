"""Run a hash-pinned native-engine model league.

This is an offline compute-host runner. It uses model_tournament.py for every
pairing, then derives BO2 points only from the recorded native MSG_WIN result.
It does not write to a production tournament room or accept model-side claims.

The default schedule is the formal five-player format:

* round robin: every pairing plays ten BO2s;
* each BO2 has two games with swapped first player;
* a BO2 win is worth 3 points, a draw 1 point each;
* the top two play a ten-BO2 final;
* a tied final continues with two-game swap blocks until one model wins both.

Use --bo2-count 1 for a short end-to-end smoke run.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
import time
from itertools import combinations

try:
    from tools.model_tournament import (
        BO2_GAMES,
        MAX_BO2,
        manifest_model,
        read_json,
        sha256,
    )
except ModuleNotFoundError:
    try:
        # Direct execution as "python tools/model_league.py" starts with
        # tools/ on sys.path, so add the repository root.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from tools.model_tournament import (
            BO2_GAMES,
            MAX_BO2,
            manifest_model,
            read_json,
            sha256,
        )
    except ModuleNotFoundError:
        # The compute host may copy both runners into one directory instead
        # of deploying the full repository package.
        from model_tournament import (
            BO2_GAMES,
            MAX_BO2,
            manifest_model,
            read_json,
            sha256,
        )


@dataclass(frozen=True)
class LeagueModel:
    model_id: str
    label: str
    steps: int

    def public(self) -> dict[str, object]:
        return {
            "id": self.model_id,
            "label": self.label,
            "steps": self.steps,
        }


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_models(
    manifest: dict,
    app_root: Path,
    requested_ids: list[str] | None,
    player_count: int,
) -> list[LeagueModel]:
    rows = manifest.get("items")
    if not isinstance(rows, list) or len(rows) != 5:
        raise ValueError("corrected Top5 manifest must contain exactly five items")
    ids = requested_ids or [row.get("id") for row in rows[:player_count]]
    if len(ids) != player_count or len(set(ids)) != player_count:
        raise ValueError("selected model count or model IDs are invalid")
    models = []
    for model_id in ids:
        model = manifest_model(manifest, model_id, app_root)
        models.append(LeagueModel(model.model_id, model.label, model.steps))
    return models


def model_map(models: list[LeagueModel]) -> dict[str, LeagueModel]:
    return {model.model_id: model for model in models}


def model_name(model_id: str, models: dict[str, LeagueModel]) -> str:
    model = models[model_id]
    return f"{model.label} ({model.model_id[:8]})"


def game_winner(game: dict, model_a_id: str, model_b_id: str) -> str | None:
    """Map an engine seat winner to the model identity for this pairing."""
    outbound = game.get("outbound") or {}
    winner = outbound.get("winner")
    model_b_seat = outbound.get("model_seat")
    if winner == 2:
        return None
    if winner not in (0, 1) or model_b_seat not in (0, 1):
        raise ValueError(f"invalid authoritative game result: {game.get('game')}")
    return model_b_id if winner == model_b_seat else model_a_id


def summarize_results(
    results: list[dict],
    model_a_id: str,
    model_b_id: str,
    models: dict[str, LeagueModel],
) -> dict[str, object]:
    if len(results) % BO2_GAMES:
        raise ValueError("pairing result does not contain complete BO2s")
    bo2s = []
    points = {model_a_id: 0, model_b_id: 0}
    bo2_wins = {model_a_id: 0, model_b_id: 0}
    bo2_draws = 0
    game_wins = {model_a_id: 0, model_b_id: 0}
    for index in range(0, len(results), BO2_GAMES):
        games = results[index:index + BO2_GAMES]
        winners = [
            game_winner(game, model_a_id, model_b_id)
            for game in games
        ]
        for winner in winners:
            if winner is not None:
                game_wins[winner] += 1
        if winners[0] is not None and winners[0] == winners[1]:
            winner = winners[0]
            loser = model_b_id if winner == model_a_id else model_a_id
            points[winner] += 3
            bo2_wins[winner] += 1
            bo2_result = "a" if winner == model_a_id else "b"
        elif winners[0] is None and winners[1] is None:
            points[model_a_id] += 1
            points[model_b_id] += 1
            bo2_draws += 1
            bo2_result = "draw"
            winner = None
            loser = None
        else:
            points[model_a_id] += 1
            points[model_b_id] += 1
            bo2_draws += 1
            bo2_result = "draw"
            winner = None
            loser = None
        bo2s.append({
            "bo2": index // BO2_GAMES + 1,
            "games": [game.get("game") for game in games],
            "game_winners": winners,
            "winner": winner,
            "loser": loser,
            "result": bo2_result,
            "points": {
                model_a_id: 3 if winner == model_a_id else 1 if winner is None else 0,
                model_b_id: 3 if winner == model_b_id else 1 if winner is None else 0,
            },
        })
    return {
        "model_a": models[model_a_id].public(),
        "model_b": models[model_b_id].public(),
        "games": len(results),
        "bo2_count": len(bo2s),
        "bo2s": bo2s,
        "points": points,
        "bo2_wins": bo2_wins,
        "bo2_draws": bo2_draws,
        "game_wins": game_wins,
    }


def pairing_key(model_a_id: str, model_b_id: str) -> str:
    return f"{model_a_id[:12]}-vs-{model_b_id[:12]}"


def run_pairing(
    *,
    runner: Path,
    app_root: Path,
    manifest_path: Path,
    deck_path: Path,
    output: Path,
    model_a: LeagueModel,
    model_b: LeagueModel,
    runner_python: Path,
    bo2_count: int,
    port_base: int,
    label: str,
    resume: bool,
    models: dict[str, LeagueModel],
) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    results_path = output / "results.json"
    summary_path = output / "summary.json"
    if resume and results_path.is_file() and summary_path.is_file():
        saved = read_json(summary_path)
        if saved.get("status") == "complete":
            return saved
    if output.exists() and any(output.iterdir()):
        if results_path.exists() or summary_path.exists():
            raise RuntimeError(f"incomplete or conflicting pairing output: {output}")
    command = [
        str(runner_python),
        str(runner),
        "--app-root", str(app_root),
        "--manifest", str(manifest_path),
        "--deck", str(deck_path),
        "--output", str(output),
        "--model-a", model_a.model_id,
        "--model-b", model_b.model_id,
        "--bo2-count", str(bo2_count),
        "--port-base", str(port_base),
    ]
    log_path = output / "runner.log"
    started_at = time.time()
    print(json.dumps({
        "event": "pairing_started",
        "label": label,
        "model_a": model_name(model_a.model_id, models),
        "model_b": model_name(model_b.model_id, models),
        "bo2_count": bo2_count,
        "output": str(output),
    }, ensure_ascii=False), flush=True)
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            log.flush()
            line = line.strip()
            if line:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict) and event.get("game"):
                    print(json.dumps({
                        "event": "game_complete",
                        "label": label,
                        "game": event["game"],
                        "outcome": event.get("outcome"),
                        "went_first": event.get("went_first"),
                        "replay_received": event.get("replay_received"),
                    }, ensure_ascii=False), flush=True)
        return_code = process.wait()
    if return_code != 0:
        raise RuntimeError(
            f"pairing failed with exit code {return_code}; see {log_path}"
        )
    raw = read_json(results_path)
    if raw.get("status") != "complete":
        raise RuntimeError(f"pairing did not complete: {results_path}")
    summary = summarize_results(raw.get("games") or [], model_a.model_id,
                                model_b.model_id, models)
    summary.update({
        "status": "complete",
        "label": label,
        "output": str(output),
        "started_at": started_at,
        "completed_at": time.time(),
    })
    atomic_json(summary_path, summary)
    print(json.dumps({
        "event": "pairing_complete",
        "label": label,
        "points": summary["points"],
        "bo2_wins": summary["bo2_wins"],
        "bo2_draws": summary["bo2_draws"],
    }, ensure_ascii=False), flush=True)
    return summary


def empty_standings(models: list[LeagueModel]) -> dict[str, dict[str, object]]:
    return {
        model.model_id: {
            "model": model.public(),
            "points": 0,
            "bo2_wins": 0,
            "bo2_draws": 0,
            "bo2_losses": 0,
            "game_wins": 0,
            "game_losses": 0,
            "games": 0,
        }
        for model in models
    }


def apply_summary(
    standings: dict[str, dict[str, object]],
    summary: dict[str, object],
) -> None:
    points = summary["points"]
    bo2_wins = summary["bo2_wins"]
    game_wins = summary["game_wins"]
    bo2_count = int(summary["bo2_count"])
    games = int(summary["games"])
    for model_id in (summary["model_a"]["id"], summary["model_b"]["id"]):
        row = standings[model_id]
        row["points"] += int(points[model_id])
        row["bo2_wins"] += int(bo2_wins[model_id])
        row["bo2_draws"] += int(summary["bo2_draws"])
        row["bo2_losses"] += bo2_count - int(bo2_wins[model_id]) - int(summary["bo2_draws"])
        row["game_wins"] += int(game_wins[model_id])
        row["game_losses"] += games - int(game_wins[model_id])
        row["games"] += games


def ranked_standings(
    standings: dict[str, dict[str, object]],
    models: list[LeagueModel],
) -> list[dict[str, object]]:
    order = {model.model_id: index for index, model in enumerate(models)}
    rows = list(standings.values())
    rows.sort(key=lambda row: (
        -int(row["points"]),
        -int(row["bo2_wins"]),
        -int(row["game_wins"]),
        order[row["model"]["id"]],
    ))
    ranked = []
    for rank, row in enumerate(rows, 1):
        ranked.append({"rank": rank, **row})
    return ranked


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--deck", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--player-count", type=int, choices=(4, 5), default=5)
    parser.add_argument("--model-id", action="append", dest="model_ids")
    parser.add_argument("--bo2-count", type=int, default=MAX_BO2)
    parser.add_argument("--final-bo2-count", type=int)
    parser.add_argument("--port-base", type=int, default=26000)
    parser.add_argument(
        "--runner-python",
        type=Path,
        help="Python interpreter used for native room/model subprocesses",
    )
    parser.add_argument("--skip-final", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--plan-only", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.bo2_count <= MAX_BO2:
        parser.error(f"--bo2-count must be between 1 and {MAX_BO2}")
    final_bo2_count = args.final_bo2_count or args.bo2_count
    if not 1 <= final_bo2_count <= MAX_BO2:
        parser.error(f"--final-bo2-count must be between 1 and {MAX_BO2}")
    if args.model_ids and len(args.model_ids) != args.player_count:
        parser.error("--model-id must be supplied exactly player-count times")
    app_root = args.app_root.resolve()
    manifest_path = args.manifest.resolve()
    deck_path = args.deck.resolve()
    if not app_root.is_dir() or not manifest_path.is_file() or not deck_path.is_file():
        parser.error("app root, manifest, and deck must exist")
    manifest = read_json(manifest_path)
    models_list = load_models(manifest, app_root, args.model_ids, args.player_count)
    models = model_map(models_list)
    runner_python = (args.runner_python or Path(sys.executable)).resolve()
    if not runner_python.is_file():
        parser.error(f"runner Python does not exist: {runner_python}")
    pairings = list(combinations(models_list, 2))
    if args.plan_only:
        print(json.dumps({
            "ok": True,
            "plan_only": True,
            "player_count": len(models_list),
            "players": [model.public() for model in models_list],
            "pairings": [
                {
                    "index": index,
                    "model_a": model_a.public(),
                    "model_b": model_b.public(),
                    "bo2_count": args.bo2_count,
                    "games": args.bo2_count * BO2_GAMES,
                }
                for index, (model_a, model_b) in enumerate(pairings, 1)
            ],
            "round_robin_games": len(pairings) * args.bo2_count * BO2_GAMES,
            "final_bo2_count": final_bo2_count,
            "deck_sha256": sha256(deck_path),
        }, ensure_ascii=False, indent=2), flush=True)
        return 0
    args.output.mkdir(parents=True, exist_ok=True)
    runner = Path(__file__).with_name("model_tournament.py").resolve()
    if not runner.is_file():
        parser.error(f"missing pairing runner: {runner}")
    atomic_json(args.output / "manifest.json", {
        "status": "running",
        "format": "native-model-league-v1",
        "manifest": str(manifest_path),
        "manifest_sha256": sha256(manifest_path),
        "deck": str(deck_path),
        "deck_sha256": sha256(deck_path),
        "player_count": len(models_list),
        "players": [model.public() for model in models_list],
        "round_robin_bo2_count": args.bo2_count,
        "final_bo2_count": final_bo2_count,
        "runner_python": str(runner_python),
        "created_at": time.time(),
    })
    standings = empty_standings(models_list)
    completed_pairs = []
    for pair_index, (model_a, model_b) in enumerate(pairings, 1):
        label = f"round-robin-{pair_index:02d}"
        summary = run_pairing(
            runner=runner,
            app_root=app_root,
            manifest_path=manifest_path,
            deck_path=deck_path,
            output=args.output / label,
            model_a=model_a,
            model_b=model_b,
            runner_python=runner_python,
            bo2_count=args.bo2_count,
            port_base=args.port_base + pair_index * 100,
            label=label,
            resume=args.resume,
            models=models,
        )
        if label not in completed_pairs:
            apply_summary(standings, summary)
            completed_pairs.append(label)
        atomic_json(args.output / "standings.json", {
            "status": "running",
            "phase": "round_robin",
            "completed_pairs": completed_pairs,
            "standings": ranked_standings(standings, models_list),
        })
    ranked = ranked_standings(standings, models_list)
    final = None
    champion = None
    if not args.skip_final:
        finalists = [ranked[0]["model"], ranked[1]["model"]]
        final_a = models[finalists[0]["id"]]
        final_b = models[finalists[1]["id"]]
        final = run_pairing(
            runner=runner,
            app_root=app_root,
            manifest_path=manifest_path,
            deck_path=deck_path,
            output=args.output / "final",
            model_a=final_a,
            model_b=final_b,
            runner_python=runner_python,
            bo2_count=final_bo2_count,
            port_base=args.port_base + 1000,
            label="final",
            resume=args.resume,
            models=models,
        )
        if final["points"][final_a.model_id] != final["points"][final_b.model_id]:
            champion = (
                final_a if final["points"][final_a.model_id] > final["points"][final_b.model_id]
                else final_b
            )
        else:
            block = 1
            while champion is None:
                block_summary = run_pairing(
                    runner=runner,
                    app_root=app_root,
                    manifest_path=manifest_path,
                    deck_path=deck_path,
                    output=args.output / f"final-tiebreak-{block:03d}",
                    model_a=final_a,
                    model_b=final_b,
                    runner_python=runner_python,
                    bo2_count=1,
                    port_base=args.port_base + 1000 + block * 100,
                    label=f"final-tiebreak-{block:03d}",
                    resume=args.resume,
                    models=models,
                )
                bo2 = block_summary["bo2s"][0]
                winners = bo2["game_winners"]
                if winners[0] is not None and winners[0] == winners[1]:
                    champion = models[winners[0]]
                block += 1
    final_payload = None
    if final is not None:
        final_payload = {
            "summary": final,
            "champion": champion.public() if champion else None,
        }
    document = {
        "status": "complete",
        "format": "native-model-league-v1",
        "player_count": len(models_list),
        "players": [model.public() for model in models_list],
        "round_robin": {
            "pairing_count": len(pairings),
            "bo2_count": args.bo2_count,
            "game_count": len(pairings) * args.bo2_count * BO2_GAMES,
            "standings": ranked,
        },
        "final": final_payload,
        "completed_at": time.time(),
    }
    atomic_json(args.output / "league.json", document)
    atomic_json(args.output / "standings.json", {
        "status": "complete",
        "phase": "finished",
        "completed_pairs": completed_pairs,
        "standings": ranked,
        "champion": champion.public() if champion else None,
    })
    atomic_json(args.output / "manifest.json", {
        **read_json(args.output / "manifest.json"),
        "status": "complete",
        "completed_at": time.time(),
    })
    print(json.dumps({
        "ok": True,
        "output": str(args.output),
        "player_count": len(models_list),
        "pairings": len(pairings),
        "round_robin_games": len(pairings) * args.bo2_count * BO2_GAMES,
        "champion": champion.public() if champion else None,
    }, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
