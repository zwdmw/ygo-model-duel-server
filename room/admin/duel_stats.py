"""Read-only model-perspective statistics from finalized server session evidence."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import threading
import time

SESSION = re.compile(r"\d{8}-\d{6}-\d{4}")
SHA = re.compile(r"[0-9a-f]{64}")
LIMIT = 20000
OUTCOMES = ("win", "loss", "draw", "incomplete", "unknown", "recording", "selftest")
BINS = {
    "duration": (60, 180, 300, 600),
    "decisions": (20, 50, 100, 200),
    "inference": (20, 50, 100, 250),
    "startup": (1, 3, 10, 30),
}
DIMENSIONS = (*BINS, "model_deck", "human_deck", "profile", "reason")


def read_object(path):
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_size > 128 * 1024:
        raise ValueError("Unsafe or oversized session metadata")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Expected session metadata object")
    return value


def finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo else None
    except (ValueError, OverflowError):
        return None


def matches(metadata, result):
    for key in ("checkpoint_sha256", "checkpoint_steps", "model_label",
                "human_deck_sha256", "model_deck_sha256"):
        if key in metadata and key in result and metadata[key] != result[key]:
            return False
    return True


def outcome(metadata, saved, result, resigned, started):
    if result.get("selftest") is True:
        return "selftest", "selftest"
    if saved.get("status") == "recording" and not saved.get("finalized_at"):
        return "recording", "recording"
    if not matches(metadata, saved) or not matches(metadata, result):
        return "unknown", "identity_mismatch"
    if result.get("policy_identity") and (
            not isinstance(result["policy_identity"], dict)
            or not matches(metadata, result["policy_identity"])):
        return "unknown", "identity_mismatch"
    if result.get("event") == "complete":
        if result.get("terminated") is False or result.get("truncated") is True:
            return "incomplete", "truncated"
        if result.get("terminated") is not True or result.get("truncated") is not False:
            return "unknown", "missing_terminal_flags"
        human_seat, model_seat = metadata.get("human_seat"), result.get("model_seat")
        if (type(human_seat) is not int or human_seat not in (0, 1)
                or type(model_seat) is not int or model_seat != 1 - human_seat
                or result.get("selftest") is not False):
            return "unknown", "unverified_perspective"
        reward = result.get("reward")
        if type(reward) not in (int, float) or reward not in (-1, 0, 1):
            return "unknown", "invalid_reward"
        # The legacy engine also reports zero reward at its 1000-decision cap.
        if reward == 0 and (type(result.get("decisions")) is not int
                            or not 0 <= result["decisions"] < 1000):
            return "incomplete", "decision_limit"
        return {1: "win", -1: "loss", 0: "draw"}[reward], "engine"
    if (saved.get("reason") == "surrender" and timestamp(saved.get("finalized_at")) is not None and started
            and type(metadata.get("human_seat")) is int and metadata["human_seat"] in (0, 1)
            and type(resigned.get("human_seat")) is int
            and resigned["human_seat"] == metadata["human_seat"]):
        return "win", "human_surrender"
    if saved.get("status") == "incomplete":
        return "incomplete", saved.get("reason") or "interrupted"
    return "unknown", "missing_result"


def session_row(folder):
    metadata = read_object(folder / "session.json")
    if not metadata:
        raise ValueError("Missing session identity")
    saved = read_object(folder / "replay-status.json")
    result = read_object(folder / "result.json")
    startup = read_object(folder / "startup.json")
    resigned = read_object(folder / "resigned.json")
    sha = metadata.get("checkpoint_sha256")
    verified_sha = sha if isinstance(sha, str) and SHA.fullmatch(sha) else None
    steps = metadata.get("checkpoint_steps")
    steps = steps if type(steps) is int and steps > 0 else None
    label = str(metadata.get("model_label") or (f"{steps // 1000000}M" if steps else "Unknown"))[:120]
    model_id = verified_sha or "unknown"
    state, reason = outcome(metadata, saved, result, resigned, finite(startup.get("startup_seconds")))
    metrics = {name: None for name in BINS}
    metrics["startup"] = startup.get("startup_seconds") if finite(startup.get("startup_seconds")) else None
    valid_result = (result.get("event") == "complete" and matches(metadata, result)
                    and reason not in ("identity_mismatch", "unverified_perspective", "invalid_reward"))
    decisions = result.get("decisions")
    if valid_result:
        metrics["duration"] = result.get("wall_seconds") if finite(result.get("wall_seconds")) else None
        metrics["decisions"] = decisions if type(decisions) is int and 0 <= decisions <= 1000 else None
        inference = result.get("inference_seconds")
        if metrics["decisions"] and finite(inference):
            metrics["inference"] = inference * 1000 / decisions
    decks = {}
    for actor in ("model", "human"):
        value = metadata.get(actor + "_deck_sha256")
        decks[actor + "_deck"] = {
            "id": value if isinstance(value, str) and SHA.fullmatch(value) else "unknown",
            "label": str(metadata.get(actor + "_deck_name") or "Unknown")[:120],
        }
    return {"session": folder.name, "model_id": model_id, "model": label,
            "checkpoint_sha256": verified_sha, "checkpoint_steps": steps,
            "started_at": saved.get("started_at") if timestamp(saved.get("started_at")) is not None else None,
            "timestamp": timestamp(saved.get("started_at")),
            "outcome": state, "reason": reason, "metrics": metrics, **decks,
            "profile": str(metadata.get("client_profile") or "unknown")[:60],
            "recording_status": saved.get("status", "unknown")}


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    return ordered[lower] + (ordered[math.ceil(position)] - ordered[lower]) * (position - lower)


def summarize(rows):
    counts = Counter(row["outcome"] for row in rows)
    result = {name: counts[name] for name in OUTCOMES}
    result.update(total=len(rows), settled=sum(counts[name] for name in ("win", "loss", "draw")),
                  surrender_wins=sum(row["reason"] == "human_surrender" for row in rows),
                  engine_settled=sum(row["reason"] == "engine" for row in rows),
                  engine_wins=sum(row["reason"] == "engine" and row["outcome"] == "win" for row in rows))
    result["win_rate"] = result["win"] / result["settled"] if result["settled"] else None
    result["engine_win_rate"] = (result["engine_wins"] / result["engine_settled"]
                                 if result["engine_settled"] else None)
    return result


def model_summary(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row["model_id"]].append(row)
    return sorted(({"id": key, "name": group[0]["model"] if key != "unknown" else "Unknown",
                    "checkpoint_steps": group[0]["checkpoint_steps"] if key != "unknown" else None,
                    **summarize(group)} for key, group in groups.items()),
                  key=lambda row: (-(row["checkpoint_steps"] or 0), row["id"]))


def buckets(rows, dimension):
    if dimension in BINS:
        edges = BINS[dimension]
        groups = {str(i): [] for i in range(len(edges) + 1)}
        groups["missing"] = []
        for row in rows:
            value = row["metrics"][dimension]
            key = "missing" if value is None else str(sum(value >= edge for edge in edges))
            groups[key].append(row)
        return [{"id": key, "lower": (0 if key == "0" else edges[int(key) - 1]) if key != "missing" else None,
                 "upper": edges[int(key)] if key != "missing" and int(key) < len(edges) else None,
                 **summarize(group)} for key, group in groups.items()]
    groups = defaultdict(list)
    for row in rows:
        key = row[dimension]["id"] if dimension.endswith("_deck") else row[dimension]
        groups[key].append(row)
    ordered = sorted(groups, key=lambda key: (-len(groups[key]), key))
    result = []
    for key in ordered[:30]:
        group = groups[key]
        label = group[0][dimension]["label"] if dimension.endswith("_deck") else key
        result.append({"id": key, "label": label, **summarize(group)})
    if len(ordered) > 30:
        result.append({"id": "other", "label": "Other",
                       **summarize([row for key in ordered[30:] for row in groups[key]])})
    return result


class DuelStatistics:
    def __init__(self, data):
        self.folder = Path(data) / "sessions"
        self.lock = threading.Lock()
        self.cached = None
        self.cached_at = 0

    def scan(self):
        with self.lock:
            if self.cached is not None and time.monotonic() - self.cached_at < 5:
                return self.cached
            folders = sorted((path for path in self.folder.glob("*") if SESSION.fullmatch(path.name)
                              and path.is_dir() and not path.is_symlink()), reverse=True)
            rows, invalid = [], 0
            for folder in folders[:LIMIT]:
                try:
                    rows.append(session_row(folder))
                except (OSError, ValueError, TypeError, OverflowError):
                    invalid += 1
            self.cached = (rows, {"scanned": min(len(folders), LIMIT), "invalid": invalid,
                                  "omitted": max(0, len(folders) - LIMIT)})
            self.cached_at = time.monotonic()
            return self.cached

    def report(self, model="all", days="all", dimension="duration", now=None):
        if model not in ("all", "unknown") and not SHA.fullmatch(model):
            raise ValueError("Invalid statistics model")
        if days not in ("all", "1", "7", "30", "90") or dimension not in DIMENSIONS:
            raise ValueError("Invalid statistics filter")
        now = time.time() if now is None else now
        rows, coverage = self.scan()
        eligible = [row for row in rows if row["outcome"] != "selftest"]
        cutoff = now - int(days) * 86400 if days != "all" else None
        in_range = [row for row in eligible if cutoff is None or (
            row["timestamp"] is not None and cutoff <= row["timestamp"] <= now)]
        selected = [row for row in in_range if model == "all" or row["model_id"] == model]
        metrics = {}
        for name in BINS:
            values = [row["metrics"][name] for row in selected if row["metrics"][name] is not None]
            metrics[name] = {"samples": len(values), "missing": len(selected) - len(values),
                             "mean": sum(values) / len(values) if values else None,
                             "p50": percentile(values, 0.5), "p95": percentile(values, 0.95)}
        return {"sampled_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "filters": {"model": model, "days": days, "bucket": dimension},
                "summary": summarize(selected), "models": model_summary(in_range),
                "available_models": model_summary(eligible), "metrics": metrics,
                "buckets": buckets(selected, dimension),
                "recent": [{key: value for key, value in row.items() if key not in ("timestamp",)}
                           for row in selected[:20]],
                "coverage": {**coverage, "selftest_excluded": len(rows) - len(eligible),
                             "missing_dates": sum(row["timestamp"] is None for row in eligible)},
                "scope": "server_sessions_including_acceptance"}
