"""Post-duel YDK export, never inferred from partial public observations.

Container layouts follow ygo_agent.human_replay / windbot_archive_audit:
YRP1 has a 32-byte header; the supported YRP2 response format has 80 bytes.
No engine or model dependencies are needed to extract the recorded decks.
"""
from collections import Counter, OrderedDict
import hashlib
import lzma
from pathlib import Path
import struct
import threading

from .continuous import authoritative_outcome
from .vendor.netduel.actions import Reader, UnsupportedMessage


LIMIT = 16 * 1024 * 1024


def replay_decks(payload):
    if not 32 <= len(payload) <= LIMIT or payload[:4] not in (b"yrp1", b"yrp2"):
        raise ValueError("Unsupported replay container")
    _, flags, _, size = struct.unpack_from("<4I", payload, 4)
    header = 32 if payload[:4] == b"yrp1" else 80
    if flags & ~0x11 or len(payload) < header or size > LIMIT:
        raise ValueError("Unsupported replay layout or size")
    raw = payload[header:]
    if flags & 1:
        prop = payload[24]
        lc, lp, pb = prop % 9, (prop // 9) % 5, prop // 45
        dictionary = struct.unpack_from("<I", payload, 25)[0]
        if pb > 4 or lc + lp > 4 or not 4096 <= dictionary <= 64 * 1024 * 1024:
            raise ValueError("Invalid or oversized LZMA dictionary")
        encoded = raw
        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_RAW, filters=[{
            "id": lzma.FILTER_LZMA1, "dict_size": dictionary, "lc": lc, "lp": lp, "pb": pb}])
        raw = decoder.decompress(encoded, max_length=min(size, LIMIT) + 1)
        if not decoder.eof and decoder.needs_input and len(raw) == size:
            # Modern YGOPro uses LZMA1EXT without an end marker. Validate the
            # known-size stream separately; legacy end-marked streams use RAW above.
            decoder = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE, memlimit=96 * 1024 * 1024)
            checked = decoder.decompress(payload[24:29] + struct.pack("<Q", size) + encoded,
                                         max_length=size + 1)
            if checked != raw:
                raise ValueError("Inconsistent decompressed replay")
        if not decoder.eof or decoder.unused_data:
            raise ValueError("Incomplete or concatenated compressed replay")
    if len(raw) > LIMIT or (size or flags & 1) and len(raw) != size:
        raise ValueError("Replay payload size mismatch")
    reader = Reader(raw)
    try:
        names = [reader._take(40).decode("utf-16le").split("\0", 1)[0] for _ in range(2)]
        lp, hand, draw, _ = [reader.u32() for _ in range(4)]
        if not 0 < lp <= 1000000 or hand > 60 or draw > 60:
            raise ValueError("Invalid replay duel setup")
        decks = []
        for _ in range(2):
            deck = {}
            for name, maximum in (("main", 60), ("extra", 15)):
                count = reader.u32()
                if count > maximum or name == "main" and count < 1:
                    raise ValueError("Invalid replay deck count")
                values = [reader.u32() for _ in range(count)]
                if any(not 0 < code <= 0x7fffffff for code in values):
                    raise ValueError("Hidden or invalid replay card code")
                deck[name] = values
            decks.append(deck)
        while reader.left:
            length = reader.u8()
            if not 1 <= length <= 64:
                raise ValueError("Invalid recorded response")
            reader.skip(length)
    except UnsupportedMessage as error:
        raise ValueError("Truncated replay data") from error
    return names, decks


class OpponentDecks:
    def __init__(self, manager):
        self.manager = manager
        self.cache = OrderedDict()
        self.lock = threading.Lock()

    def inspect(self, session, refresh=False):
        from common import safe_path
        folder = safe_path(self.manager.sessions, session)
        paths = [folder / name for name in ("result.json", "replay-status.json", "model.ydk", "session.json")]
        signature = tuple((path.stat().st_mtime_ns, path.stat().st_size) if path.is_file() else None
                          for path in paths)
        key = (session, signature)
        with self.lock:
            cached = self.cache.get(key)
        if cached is not None and not refresh:
            return cached
        unavailable = {"available": False, "reason": "no_complete_replay"}
        try:
            result = self.manager._read(paths[0])
            status = self.manager._read(paths[1])
            if (authoritative_outcome(result) == "incomplete" or not status.get("finalized_at")
                    or not status.get("recording_available")):
                return unavailable
            entries = status.get("replays", [])
            if not 1 <= len(entries) <= 5:
                raise ValueError("ambiguous_replay")
            metadata = self.manager._read(folder / "session.json")
            items = []
            seen_games = set()
            for entry in entries:
                replay = safe_path(self.manager.root, entry["archive"])
                if (not replay.is_relative_to((self.manager.root / "recordings").resolve())
                        or replay.is_symlink() or replay.suffix != ".yrp" or replay.stat().st_size > LIMIT):
                    raise ValueError("invalid_replay")
                raw = replay.read_bytes()
                if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                    raise ValueError("replay_integrity")
                names, decks = replay_decks(raw)
                game = entry.get("game", 1)
                if type(game) is not int or game in seen_games:
                    raise ValueError("ambiguous_replay_game")
                seen_games.add(game)
                if result.get("result_scope") == "match":
                    if not 1 <= game <= len(result["games"]):
                        raise ValueError("unverified_match_game")
                    seat = result["games"][game - 1]["our_player"]
                else:
                    seat = result["model_seat"]
                if metadata.get("deck_source") == "server_random":
                    if names[seat] != metadata.get("nickname") or names[1 - seat] == names[seat]:
                        raise ValueError("random_deck_replay_identity_mismatch")
                else:
                    if paths[2].is_symlink() or paths[2].stat().st_size > 65536:
                        raise ValueError("invalid_model_deck")
                    own = self.manager.agent.parse(paths[2].read_text(encoding="utf-8-sig"))
                    if any(Counter(decks[seat][part]) != Counter(own[part]) for part in ("main", "extra")):
                        raise ValueError("model_deck_mismatch")
                deck = decks[1 - seat]
                text = "# Opponent deck from server replay; Side Deck was not recorded.\n#main\n"
                text += "".join(f"{code}\n" for code in deck["main"]) + "#extra\n"
                text += "".join(f"{code}\n" for code in deck["extra"]) + "!side\n"
                items.append({"main": len(deck["main"]), "extra": len(deck["extra"]),
                              "side_known": False, "name": names[1 - seat], "ydk": text, "game": game})
            value = {**items[0], "available": True, "count": len(items), "items": items}
        except (OSError, ValueError, KeyError, TypeError, lzma.LZMAError):
            value = {"available": False, "reason": "unverified_replay_deck"}
        with self.lock:
            self.cache[key] = value
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
        return value
