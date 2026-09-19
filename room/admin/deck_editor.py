"""Read-only card metadata and stable revisions for the owner deck editor."""
import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3


EXTRA_TYPES = 0x40 | 0x2000 | 0x800000 | 0x4000000
SECTIONS = ("main", "extra", "side")


def canonical_ydk(deck):
    return "".join(marker + "\n" + "".join(str(card) + "\n" for card in deck[key])
                   for key, marker in (("main", "#main"), ("extra", "#extra"), ("side", "!side")))


def deck_revision(deck):
    raw = json.dumps([deck["name"], deck["ydk"]], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


class DeckCards:
    def __init__(self, database, names):
        self.database, self.names_path = Path(database), Path(names)
        self.rows = None

    def _load(self):
        if self.rows is not None:
            return
        rows = {}
        if self.database.is_file():
            with closing(sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                for code, name in connection.execute("SELECT id, name FROM texts"):
                    rows[int(code)] = {"name": str(name), "original": str(name)}
        if self.names_path.is_file():
            with self.names_path.open("rb") as stream:
                raw = stream.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError("Card names file is too large")
            names = json.loads(raw)
            if not isinstance(names, dict) or len(names) > 100000:
                raise ValueError("Invalid card names file")
            for code, name in names.items():
                if not code.isascii() or not code.isdecimal() or not isinstance(name, str):
                    raise ValueError("Invalid card name entry")
                if 0 < int(code) <= 0xFFFFFFFF and 0 < len(name) <= 500:
                    rows.setdefault(int(code), {"original": ""})["name"] = name
        self.rows = rows

    def search(self, card_types, query="", section="side", ids=None):
        self._load()
        if section not in SECTIONS:
            raise ValueError("Invalid deck section")
        query = query.strip().casefold()
        if len(query) > 80:
            raise ValueError("Card search is too long")
        codes = ids if ids is not None else sorted(card_types)
        result = []
        for code in codes:
            metadata = self.rows.get(code, {})
            card_type = card_types.get(code, 0)
            extra = bool(card_type & EXTRA_TYPES)
            if ids is None:
                if (section == "main" and extra) or (section == "extra" and not extra):
                    continue
                if not query or not any(query in str(value).casefold() for value in
                                         (code, metadata.get("name", ""), metadata.get("original", ""))):
                    continue
            result.append({"id": code, "name": metadata.get("name") or str(code),
                           "extra": extra, "supported": code in card_types})
        result.sort(key=lambda row: (str(row["id"]) != query, row["name"].casefold(), row["id"]))
        return {"items": result if ids is not None else result[:40], "total": len(result)}
