"""Deck parsing, validation and one-use local room configurations."""
import argparse
import copy
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

SECTIONS = ("main", "extra", "side")
EXTRA_TYPES = 0x40 | 0x2000 | 0x800000 | 0x4000000
SERVICE_URL = "http://127.0.0.1:7916"


def parse_ydk(text):
    deck = {section: [] for section in SECTIONS}
    section = "main"
    for number, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = line.strip()
        if line in ("#main", "#extra", "!side"):
            section = line.lstrip("#!")
        elif not line or line.startswith("#"):
            continue
        elif line.isascii() and line.isdecimal() and 0 < int(line) <= 0xFFFFFFFF:
            deck[section].append(int(line))
        else:
            raise ValueError(f"Invalid card ID at line {number}: {line[:60]}")
    return deck


def read_ydk(path):
    path = Path(path)
    if path.stat().st_size > 128 * 1024:
        raise ValueError("Deck file exceeds 128 KiB")
    return parse_ydk(path.read_text(encoding="utf-8-sig"))


def validate_selection(deck, card_types):
    if not isinstance(deck, dict) or set(deck) != set(SECTIONS):
        raise ValueError("Deck must contain main, extra, and side lists")
    for section in SECTIONS:
        cards = deck[section]
        if not isinstance(cards, list) or any(type(x) is not int or not 0 < x <= 0xFFFFFFFF for x in cards):
            raise ValueError(f"Invalid card IDs in {section}")
    if not 40 <= len(deck["main"]) <= 60 or len(deck["extra"]) > 15 or len(deck["side"]) > 15:
        raise ValueError("Main deck: 40-60 cards; extra and side: at most 15 each")
    unknown = sorted(set(sum((deck[x] for x in SECTIONS), [])) - card_types.keys())
    if unknown:
        raise ValueError("Cards outside this model snapshot: " + ", ".join(map(str, unknown)))
    misplaced = [code for code in deck["main"] if card_types[code] & EXTRA_TYPES]
    misplaced += [code for code in deck["extra"] if not card_types[code] & EXTRA_TYPES]
    if misplaced:
        raise ValueError("Cards in the wrong main/extra section: " + ", ".join(map(str, sorted(set(misplaced)))))
    return copy.deepcopy(deck)


def selection_name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 120 or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid deck name")
    return value.strip()


class PendingDecks:
    def __init__(self, ttl=600, limit=32, clock=time.monotonic):
        self.ttl, self.limit, self.clock = ttl, limit, clock
        self.items = {}
        self.lock = threading.Lock()

    def _expire(self):
        now = self.clock()
        self.items = {key: value for key, value in self.items.items() if value[0] > now}

    def prepare(self, request, card_types):
        if not isinstance(request, dict):
            raise ValueError("Expected a JSON object")
        profile = request.get("profile", "YGOPro2")
        if profile not in ("YGOPro2", "legacy"):
            raise ValueError("Unsupported client profile")
        selection = {"profile": profile}
        for actor in ("human", "model"):
            try:
                selection[actor + "_name"] = selection_name(request.get(actor + "_name"))
                selection[actor + "_deck"] = validate_selection(request.get(actor + "_deck"), card_types)
            except ValueError as error:
                raise ValueError(f"{actor}: {error}") from error
        with self.lock:
            self._expire()
            if len(self.items) >= self.limit:
                raise ValueError("Too many pending games; wait for old selections to expire")
            token = secrets.token_hex(8)
            self.items[token] = (self.clock() + self.ttl, selection)
        return {"password": "D:" + token, "expires_in": self.ttl,
                "human_name": selection["human_name"], "model_name": selection["model_name"]}

    def consume(self, token):
        with self.lock:
            self._expire()
            item = self.items.pop(token, None)
        if item is None:
            raise ValueError("Deck selection expired or was already used; start from the deck picker again")
        return item[1]


def local_request(path, payload=None, base=SERVICE_URL):
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(base + path, data=data, headers={"Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            message = json.load(error).get("error", str(error))
        except (ValueError, AttributeError):
            message = str(error)
        raise ValueError(message) from error


def prepare_files(human, model, base=SERVICE_URL):
    return local_request("/prepare", {
        "profile": "YGOPro2", "human_name": Path(human).stem, "model_name": Path(model).stem,
        "human_deck": read_ydk(human), "model_deck": read_ydk(model),
    }, base)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--human", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare_files(args.human, args.model)))
    except (OSError, ValueError) as error:
        parser.exit(1, str(error) + "\n")
