"""Extract fixed feature IDs from the deployed runtime's pinned C++ source."""
import argparse
import hashlib
import json
from pathlib import Path
import re

from .vendor.netduel import constants


def block(source, name):
    match = re.search(r"\b" + re.escape(name) + r"\s*=\s*\{(.*?)\n\};",
                      source, re.S)
    if match is None:
        raise ValueError("Missing native table: " + name)
    return re.sub(r"//[^\n]*", "", match[1])


def extract(path):
    source = path.read_text(encoding="utf-8")
    systems = {}
    for name in ("system_strings", "system_string_extensions",
                 "script_system_string_extensions"):
        for key in sorted(set(map(int, re.findall(r"\{\s*(\d+)\s*,", block(source, name))))):
            if key in systems:
                raise ValueError("Duplicate native system description")
            systems[key] = len(systems) + 16
    for key in map(int, re.findall(r"\d+", block(source, "generic_system_string_extensions"))):
        if key in systems:
            raise ValueError("Duplicate native generic description")
        systems[key] = len(systems) + 16
    messages = {getattr(constants, name): index for index, name in enumerate(
        re.findall(r"\bMSG_\w+", block(source, "_msgs")), 1)}
    return {"source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "system_strings": systems, "messages": messages,
            "actions": [128, 12], "history": [32, 14],
            "action_ir": [128, 24], "single_refs": [128, 4],
            "group_refs": [128, 5, 8], "selection": [12],
            "public_events": [32, 16], "public_event_refs": [32, 4]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    result = extract(args.source)
    target = Path(__file__).with_name("native_contract.json")
    target.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"source_sha256": result["source_sha256"],
                      "system_strings": len(result["system_strings"]),
                      "messages": len(result["messages"])}))
