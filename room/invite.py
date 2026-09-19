"""Create/revoke room-only invitations. Requires filesystem access, not HTTP."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets

from connector import validate_config


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("identity")
    parser.add_argument("--revoke", action="store_true")
    parser.add_argument("--endpoint", help="HTTPS gateway; otherwise use public-endpoint.json")
    args = parser.parse_args()
    if not args.identity.replace("-", "").replace("_", "").isalnum():
        parser.error("Use an alphanumeric invitation identity")
    os.umask(0o077)
    root = Path(__file__).resolve().parent
    store = root / "invitations.json"
    entries = json.loads(store.read_text()) if store.exists() else {}
    if args.revoke:
        entries.pop(args.identity, None)
    else:
        token = secrets.token_urlsafe(32)
        endpoint = args.endpoint
        if endpoint is None:
            settings = root / "public-endpoint.json"
            if not settings.exists():
                parser.error("Pass --endpoint or migrate public-endpoint.json first")
            endpoint = json.loads(settings.read_text())["endpoint"]
        endpoint, _ = validate_config({"endpoint": endpoint, "token": token})
        entries[args.identity] = {"sha256": hashlib.sha256(token.encode()).hexdigest(),
                                  "enabled": True}
        (root / f"invite-{args.identity}.json").write_text(json.dumps({
            "endpoint": endpoint,
            "token": token,
        }, indent=2))
    temporary = store.with_suffix(".tmp")
    temporary.write_text(json.dumps(entries, indent=2))
    temporary.replace(store)
    print(json.dumps({"identity": args.identity, "revoked": args.revoke,
                      "token_printed": False}))


if __name__ == "__main__":
    main()
