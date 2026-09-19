"""Container test: exact actions must survive IPC and consecutive duel resets."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def run(args):
    root = args.output
    root.mkdir(parents=True, exist_ok=False)
    logs = []
    identities = []
    for label in ("direct", "resident_first", "resident_second"):
        output = root / label
        worker = Path(os.environ.get("AGENT195M_ROOT", "/app")) / "worker.py"
        command = [sys.executable, str(worker), "--selftest", "--seed", "195035136",
                   "--human-deck", str(args.human_deck), "--model-deck", str(args.model_deck),
                   "--output", str(output)]
        if label != "direct":
            command += ["--policy-socket", str(args.policy_socket)]
        with (root / f"{label}.log").open("w") as log:
            subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                           timeout=240, check=True)
        records = [json.loads(line) for line in (output / "decisions.jsonl").read_text().splitlines()]
        logs.append([(row["actor"], row["action"], row["legal_count"]) for row in records])
        result = json.loads((output / "result.json").read_text())
        if label != "direct":
            identities.append(result["policy_identity"])
    assert logs[0] and logs[0] == logs[1] == logs[2], "Policy actions changed"
    assert identities[0]["pid"] == identities[1]["pid"], "Policy process was not reused"
    assert identities[0]["policy_session"] != identities[1]["policy_session"]
    report = {"identical_decisions": len(logs[0]), "policy_pid": identities[0]["pid"],
              "consecutive_sessions": [item["policy_session"] for item in identities]}
    (root / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy-socket", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--human-deck", type=Path, required=True)
    parser.add_argument("--model-deck", type=Path, required=True)
    run(parser.parse_args())
