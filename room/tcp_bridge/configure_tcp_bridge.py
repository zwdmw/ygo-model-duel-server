"""Generate high-entropy native room passwords and a private bridge config."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from deck_config import read_ydk
from tcp_bridge import NativeBridge


def create_config(invite, presets):
    config = {"endpoint": invite["endpoint"], "token": invite["token"], "rooms": []}
    codes = []
    for identity, profile, filename in presets:
        password = secrets.token_urlsafe(12)
        model = Path(filename)
        config["rooms"].append({
            "id": identity, "profile": profile,
            "password_sha256": hashlib.sha256(password.encode()).hexdigest(),
            "model_name": model.stem, "model_deck": read_ydk(model),
        })
        codes.append({"room": identity, "profile": profile, "model": model.stem,
                      "password": password})
    NativeBridge(config)
    return config, codes


def write_private(path, value):
    # Exclusive create prevents accidental invitation/password rotation.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        json.dump(value, target, indent=2, ensure_ascii=True)
        target.write("\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--invite", type=Path, required=True)
    parser.add_argument("--room", nargs=3, action="append", required=True,
                        metavar=("ID", "PROFILE", "MODEL_YDK"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--codes", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.out.resolve() == args.codes.resolve() or args.out.exists() or args.codes.exists():
            raise ValueError("Choose two distinct, new output files")
        config, codes = create_config(
            json.loads(args.invite.read_text(encoding="utf-8")), args.room)
        write_private(args.out, config)
        write_private(args.codes, codes)
        print("Created private config and room codes; no credentials printed.")
    except (OSError, ValueError, TypeError, KeyError):
        parser.exit(1, "Configuration failed; check inputs, paths and existing files.\n")
