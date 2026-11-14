import asyncio
import base64
import copy
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import struct
import tempfile
import time
import unittest
from unittest.mock import AsyncMock
from pathlib import Path
import zipfile

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer

from pvp import protocol as P
from pvp.deck import load_ydk
from pvp.replay import evrp_duel_to_yrp3d
from pvp.server import (
    NativeGateway, PvpApplication, PvpStore, RoomProvisioningRegistry,
    PLAYER_STALL_WARNING_SECONDS, SESSION_TTL, connection_state,
    continuous_series_open, partial_series_replay_stored, public_session,
    reconnect_scope, series_replay_available,
    series_replay_status, series_replay_stored, validate_series_replay,
)


SOURCE = Path(__file__).resolve().parents[1]
FIXED_DECK = SOURCE / "decks" / "SkyStriker.ydk"


def make_series_replay(duels: int = 2) -> bytes:
    start = base64.b64encode(
        P.packet(P.GAME_MSG, bytes((P.MSG_START, 0)))
    ).decode("ascii")
    envelope = {
        "version": 1,
        "meta": {
            "players": [{"name": "A"}, {"name": "B"}],
            "hostInfo": {"start_lp": 8000},
            "startTime": "2026-10-04T00:00:00.000Z",
            "endTime": "2026-10-04T00:10:00.000Z",
        },
        "duels": [
            {
                "seed": list(range(8)),
                "startTime": "2026-10-04T00:00:00.000Z",
                "endTime": "2026-10-04T00:01:00.000Z",
                "winPosition": index % 2,
                "winReason": 1,
                "frames": [
                    start,
                    base64.b64encode(P.packet(
                        P.GAME_MSG,
                        bytes((P.MSG_WIN, index % 2, 1)),
                    )).decode("ascii"),
                ],
                "spectatorFrames": [
                    start,
                    base64.b64encode(P.packet(
                        P.GAME_MSG,
                        bytes((P.MSG_WIN, index % 2, 1)),
                    )).decode("ascii"),
                ],
            }
            for index in range(duels)
        ],
    }
    return gzip.compress(
        json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    )


def prefix_reference(session: dict, path: Path) -> dict:
    return {
        "path": str(path),
        "session_id": session["id"],
        "room_id": session["room_id"],
        "match_id": session["match_id"],
    }


class FixedDeckTest(unittest.TestCase):
    def test_sky_striker_deck_builds_native_payload_and_stable_identity(self):
        deck = load_ydk(FIXED_DECK)

        self.assertEqual((len(deck.main), len(deck.extra), len(deck.side)), (40, 15, 0))
        self.assertEqual(
            deck.canonical_sha256,
            "43503f002556c020751d1dea290f87c82d86020f96aa62a752c3bcc10de7b7e6",
        )
        combined, side = P.parse_deck_payload(deck.payload)
        self.assertEqual(combined, list(deck.main + deck.extra))
        self.assertEqual(side, [])

    def test_fixed_deck_parser_rejects_missing_sections(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "bad.ydk"
            path.write_text("#main\n" + "1\n" * 40, encoding="ascii")
            with self.assertRaisesRegex(ValueError, "missing sections"):
                load_ydk(path)


class ReplayExportTest(unittest.TestCase):
    @staticmethod
    def records(payload: bytes) -> list[tuple[int, bytes]]:
        records = []
        offset = 0
        while offset < len(payload):
            opcode, size = struct.unpack_from("<BI", payload, offset)
            offset += 5
            records.append((opcode, payload[offset:offset + size]))
            offset += size
        if offset != len(payload):
            raise AssertionError("YRP3D record length is invalid")
        return records

    def test_evrp_duel_converts_to_mdpro3_records(self):
        envelope = validate_series_replay(make_series_replay(1))

        payload = evrp_duel_to_yrp3d(
            envelope["meta"],
            envelope["duels"][0],
        )

        records = self.records(payload)
        self.assertEqual([item[0] for item in records], [235, P.MSG_START, P.MSG_WIN])
        self.assertEqual(len(records[0][1]), 604)
        self.assertEqual(
            records[0][1][:100].decode("utf-16-le").split("\0", 1)[0],
            "A",
        )
        self.assertEqual(struct.unpack_from("<i", records[0][1], 600)[0], 5)

    def test_evrp_duel_requires_terminal_win(self):
        envelope = validate_series_replay(make_series_replay(1))
        envelope["duels"][0]["spectatorFrames"].pop()

        with self.assertRaisesRegex(ValueError, "MSG_WIN"):
            evrp_duel_to_yrp3d(envelope["meta"], envelope["duels"][0])

    def test_playable_archive_derives_yrp3d_and_keeps_raw_download(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-playable-replay",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "seed": list(range(8)),
                    "continuous_series": True,
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            raw_first = b"yrp2-first"
            raw_second = b"yrp2-second"
            store.finish(session, 0, 1, 1)
            store.record_replay(session, raw_first, "a", 1, 1)
            store.advance_series(session, {
                "game_number": 2,
                "seat_plan": {"a": 1, "b": 0},
                "seed": list(range(8)),
                "seed_mode": "mirrored",
                "required_first_player": "b",
            })
            store.finish(session, 1, 1, 2)
            store.record_replay(session, raw_second, "a", 2, 2)
            evrp = make_series_replay(2)
            store.record_series_replay(
                session,
                evrp,
                validate_series_replay(evrp),
            )

            with zipfile.ZipFile(io.BytesIO(
                store.replay_archive(session, "playable")
            )) as archive:
                self.assertEqual(
                    archive.namelist(),
                    [
                        "game-0001.yrp3d",
                        "game-0002.yrp3d",
                        "manifest.json",
                    ],
                )
                first = archive.read("game-0001.yrp3d")
                self.assertEqual(
                    [item[0] for item in self.records(first)],
                    [235, P.MSG_START, P.MSG_WIN],
                )
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(
                    manifest["schema"],
                    "ygo-pvp-replay-manifest/v2",
                )
                self.assertEqual(manifest["format"], "mdpro3-yrp3d")
                self.assertEqual(
                    manifest["replays"][0]["source_yrp_sha256"],
                    hashlib.sha256(raw_first).hexdigest(),
                )
                self.assertEqual(
                    manifest["replays"][0]["source_evrp_sha256"],
                    hashlib.sha256(evrp).hexdigest(),
                )

            with zipfile.ZipFile(io.BytesIO(
                store.replay_archive(session, "raw")
            )) as archive:
                self.assertIn("game-0001.yrp", archive.namelist())
                self.assertEqual(archive.read("game-0001.yrp"), raw_first)

    def test_playable_archive_recovers_historical_ordinals_and_seat_names(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            seed_5 = [100 + index for index in range(8)]
            seed_6 = [200 + index for index in range(8)]
            session = store.create(
                {
                    "room_id": "r-historical-replay",
                    "match_id": "m-02",
                    "game_number": 6,
                    "seat_plan": {"a": 1, "b": 0},
                    "seed": seed_6,
                    "continuous_series": True,
                    "players": {"a": {"name": "A"}, "b": {"name": "C"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            session["results"] = [
                {
                    "game_number": 5,
                    "seed": seed_5,
                    "seat_plan": {"a": 0, "b": 1},
                    "result": {"recorded_at": 1.0},
                },
                {
                    "game_number": 6,
                    "seed": seed_6,
                    "seat_plan": {"a": 1, "b": 0},
                    "result": {"recorded_at": 11.0},
                },
            ]
            session["result"] = {"recorded_at": 11.0}
            session["series_finished"] = True

            envelope = json.loads(gzip.decompress(make_series_replay(2)))
            for duel, seed, started, ended in zip(
                envelope["duels"],
                (seed_5, seed_6),
                (
                    "1970-01-01T00:00:00.000Z",
                    "1970-01-01T00:00:10.000Z",
                ),
                (
                    "1970-01-01T00:00:01.000Z",
                    "1970-01-01T00:00:11.000Z",
                ),
            ):
                duel["seed"] = seed
                duel["startTime"] = started
                duel["endTime"] = ended
            evrp = gzip.compress(
                json.dumps(envelope, separators=(",", ":")).encode("utf-8")
            )
            evrp_path = store.replay_root / f"{session['id']}.evrp"
            evrp_path.write_bytes(evrp)
            session["series_replay"] = {
                "path": str(evrp_path),
                "bytes": len(evrp),
                "sha256": hashlib.sha256(evrp).hexdigest(),
                "duels": 2,
            }

            session["series_replays"] = []
            for stored_ordinal, seed, started_at in (
                (1, seed_5, 0),
                (2, seed_6, 10),
            ):
                raw = struct.pack(
                    "<6I8s8I",
                    0x32707279,
                    0x1362,
                    0x11,
                    0,
                    0,
                    started_at,
                    bytes(8),
                    *seed,
                )
                path = store.replay_root / (
                    f"{session['id']}.g{stored_ordinal:04d}.yrp"
                )
                path.write_bytes(raw)
                session["series_replays"].append({
                    "ordinal": stored_ordinal,
                    "path": str(path),
                    "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                })

            with zipfile.ZipFile(io.BytesIO(
                store.replay_archive(session, "playable")
            )) as archive:
                self.assertEqual(
                    archive.namelist(),
                    [
                        "game-0005.yrp3d",
                        "game-0006.yrp3d",
                        "manifest.json",
                    ],
                )
                game_5 = self.records(archive.read("game-0005.yrp3d"))
                game_6 = self.records(archive.read("game-0006.yrp3d"))
                self.assertEqual(
                    game_5[0][1][:100].decode("utf-16-le").split("\0", 1)[0],
                    "A",
                )
                self.assertEqual(
                    game_6[0][1][:100].decode("utf-16-le").split("\0", 1)[0],
                    "C",
                )
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(
                    [item["ordinal"] for item in manifest["replays"]],
                    [5, 6],
                )
                self.assertEqual(
                    [item["source_yrp_ordinal"] for item in manifest["replays"]],
                    [1, 2],
                )
                self.assertEqual(manifest["missing_ordinals"], [1, 2, 3, 4])

    def test_playable_archive_maps_sixteen_surviving_duels_to_games_five_through_twenty(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-historical-prefix",
                    "match_id": "m-02",
                    "game_number": 20,
                    "seat_plan": {"a": 0, "b": 1},
                    "seed": list(range(8)),
                    "continuous_series": True,
                    "players": {"a": {"name": "A"}, "b": {"name": "C"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            for ordinal in range(1, 17):
                replay = b"yrp2" + ordinal.to_bytes(2, "little")
                self.assertIsNotNone(store.record_replay(
                    session,
                    replay,
                    "a",
                    ordinal,
                    ordinal,
                ))
            evrp = make_series_replay(16)
            evrp_path = store.replay_root / f"{session['id']}.evrp"
            evrp_path.write_bytes(evrp)
            session["status"] = "finished"
            session["series_finished"] = True
            session["series_replay"] = {
                "path": str(evrp_path),
                "bytes": len(evrp),
                "sha256": hashlib.sha256(evrp).hexdigest(),
                "duels": 16,
            }
            store._save(session)

            with zipfile.ZipFile(io.BytesIO(
                store.replay_archive(session, "playable")
            )) as archive:
                self.assertEqual(
                    archive.namelist(),
                    [
                        *(f"game-{ordinal:04d}.yrp3d"
                          for ordinal in range(5, 21)),
                        "manifest.json",
                    ],
                )
                manifest = json.loads(archive.read("manifest.json"))
                self.assertFalse(manifest["complete"])
                self.assertTrue(manifest["historical_prefix_missing"])
                self.assertEqual(
                    manifest["missing_ordinals"],
                    [1, 2, 3, 4],
                )
                self.assertEqual(
                    [item["ordinal"] for item in manifest["replays"]],
                    list(range(5, 21)),
                )
                self.assertEqual(
                    [
                        item["source_yrp_ordinal"]
                        for item in manifest["replays"]
                    ],
                    list(range(1, 17)),
                )

    def test_series_replay_validation_rejects_duplicate_game_numbers(self):
        envelope = json.loads(gzip.decompress(make_series_replay(2)))
        for duel in envelope["duels"]:
            duel["gameNumber"] = 5
        payload = gzip.compress(
            json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        )

        with self.assertRaisesRegex(ValueError, "game number"):
            validate_series_replay(payload)


class PvpStoreTest(unittest.TestCase):
    def test_continuous_series_archives_each_yrp_once_by_game_ordinal(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-replay-files",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "seed": list(range(8)),
                    "continuous_series": True,
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            first = b"yrp1" + bytes(64)
            second = b"yrp1" + bytes([1]) * 64

            store.finish(session, 0, 1, 1)
            store.record_replay(session, first, "a", 1, 1)
            store.record_replay(session, first, "b", 1, 1)
            store.record_replay(session, first, "a", 1, 1)
            store.advance_series(session, {
                "game_number": 2,
                "seat_plan": {"a": 1, "b": 0},
                "seed": list(range(8)),
                "seed_mode": "mirrored",
                "required_first_player": "b",
            })
            store.finish(session, 0, 1, 2)
            # A reconnect can resend an already archived game before the
            # current replay. Explicit game identity keeps both operations
            # idempotent.
            store.record_replay(session, first, "a", 1, 1)
            store.record_replay(session, second, "a", 2, 2)
            store.record_replay(session, second, "b", 2, 2)

            self.assertEqual(
                [item["ordinal"] for item in session["series_replays"]],
                [1, 2],
            )
            self.assertNotIn("replay_source_ordinals", session)
            self.assertEqual(session["series_replays"][0]["sources"], ["a", "b"])
            self.assertEqual(session["series_replays"][1]["sources"], ["a", "b"])
            public = public_session(session)
            self.assertNotIn("path", public["replay"])
            self.assertTrue(all(
                "path" not in replay for replay in public["series_replays"]
            ))
            self.assertNotIn("path", public["result"]["replay"])
            self.assertNotIn(
                "path", public["results"][0]["result"]["replay"]
            )

            with zipfile.ZipFile(io.BytesIO(store.replay_archive(session))) as archive:
                self.assertEqual(
                    archive.namelist(),
                    [
                        "game-0001.yrp",
                        "game-0002.yrp",
                        "manifest.json",
                    ],
                )
                self.assertEqual(archive.read("game-0001.yrp"), first)
                self.assertEqual(archive.read("game-0002.yrp"), second)
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(
                    manifest,
                    {
                        "schema": "ygo-pvp-replay-manifest/v1",
                        "session_id": session["id"],
                        "ordinal_scope": "absolute",
                        "replays": [
                            {
                                "ordinal": 1,
                                "filename": "game-0001.yrp",
                                "bytes": len(first),
                                "sha256": hashlib.sha256(first).hexdigest(),
                            },
                            {
                                "ordinal": 2,
                                "filename": "game-0002.yrp",
                                "bytes": len(second),
                                "sha256": hashlib.sha256(second).hexdigest(),
                            },
                        ],
                    },
                )

    def test_non_continuous_replay_archive_uses_absolute_game_number(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-absolute-replay",
                    "match_id": "m-final",
                    "game_number": 21,
                    "seat_plan": {"a": 0, "b": 1},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            replay = b"yrp1" + bytes(64)
            store.record_replay(session, replay)

            with zipfile.ZipFile(
                io.BytesIO(store.replay_archive(session))
            ) as archive:
                self.assertEqual(
                    archive.namelist(),
                    ["game-0021.yrp", "manifest.json"],
                )
                manifest = json.loads(archive.read("manifest.json"))
                self.assertEqual(manifest["ordinal_scope"], "absolute")
                self.assertEqual(manifest["session_id"], session["id"])
                self.assertEqual(manifest["replays"][0]["ordinal"], 21)

    def test_replay_archive_rejects_missing_or_corrupt_stored_replay(self):
        for damage in ("missing", "size", "sha256"):
            with self.subTest(damage=damage), tempfile.TemporaryDirectory() as root:
                store = PvpStore(Path(root))
                session = store.create(
                    {
                        "room_id": f"r-corrupt-{damage}",
                        "match_id": "m-01",
                        "game_number": 1,
                        "seat_plan": {"a": 0, "b": 1},
                        "players": {
                            "a": {"name": "A"},
                            "b": {"name": "B"},
                        },
                    },
                    "127.0.0.1",
                    7711,
                    "",
                    "",
                )
                replay = b"yrp1" + bytes(64)
                metadata = store.record_replay(session, replay)
                if damage == "missing":
                    Path(metadata["path"]).unlink()
                elif damage == "size":
                    metadata["bytes"] += 1
                    session["replay"]["bytes"] += 1
                else:
                    metadata["sha256"] = "0" * 64
                    session["replay"]["sha256"] = "0" * 64

                with self.assertRaises(web.HTTPConflict):
                    store.replay_archive(session)

    def test_non_continuous_finish_accepts_single_duel_series_replay(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            store = PvpStore(root_path / "sessions")
            session = store.create(
                {
                    "room_id": "r-single-replay",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "seed": list(range(8)),
                    "continuous_series": False,
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            store.observe_start(session, "a", bytes((P.MSG_START, 0)))
            store.observe_start(session, "b", bytes((P.MSG_START, 1)))
            store.finish(session, 0, 1)

            self.assertEqual(session["status"], "finished")
            self.assertTrue(session["series_finished"])

            archive = make_series_replay(1)
            store.record_series_replay(
                session, archive, validate_series_replay(archive)
            )
            self.assertTrue(series_replay_available(session))

            registry = RoomProvisioningRegistry(root_path / "registry")
            published = json.loads(
                registry.publish(session, None).read_text(encoding="utf-8")
            )
            self.assertTrue(published["series_finished"])
            self.assertEqual(published["series_replay"]["duels"], 1)

    def test_reconnected_replay_stream_deduplicates_then_fills_missing_game(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-replay-reconnect",
                    "match_id": "m-replay-reconnect",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "seed": list(range(8)),
                    "continuous_series": True,
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            first = b"yrp1" + bytes(64)
            second = b"yrp1" + bytes([2]) * 64
            store.finish(session, 0, 1, 1)
            store.record_replay(session, first, "b", 1, 1)
            store.advance_series(session, {
                "game_number": 2,
                "seat_plan": {"a": 1, "b": 0},
                "seed": list(range(8)),
                "seed_mode": "mirrored",
                "required_first_player": "b",
            })
            store.finish(session, 0, 1, 2)

            replayed = store.record_replay(session, first, "a")
            filled = store.record_replay(session, second, "a")

            self.assertEqual(replayed["ordinal"], 1)
            self.assertEqual(filled["ordinal"], 2)
            self.assertEqual(
                [item["ordinal"] for item in session["series_replays"]],
                [1, 2],
            )
            self.assertEqual(session["series_replays"][0]["sources"], ["b", "a"])
            self.assertEqual(session["result"]["replay"]["ordinal"], 2)

    def test_explicit_game_identity_keeps_identical_yrp_payloads_separate(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-identical-replays",
                    "match_id": "m-identical-replays",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "seed": list(range(8)),
                    "continuous_series": True,
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            replay = b"yrp1" + bytes(64)
            store.finish(session, 0, 1, 1)
            store.record_replay(session, replay, "a", 1, 1)
            store.advance_series(session, {
                "game_number": 2,
                "seat_plan": {"a": 1, "b": 0},
                "seed": list(range(8)),
                "seed_mode": "mirrored",
                "required_first_player": "b",
            })
            store.finish(session, 0, 1, 2)
            store.record_replay(session, replay, "a", 2, 2)

            self.assertEqual(
                [item["ordinal"] for item in session["series_replays"]],
                [1, 2],
            )
            self.assertEqual(
                session["series_replays"][0]["sha256"],
                session["series_replays"][1]["sha256"],
            )
            self.assertEqual(session["result"]["replay"]["ordinal"], 2)

    def test_continuous_series_reuses_room_and_advances_game_with_private_seed(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create({
                "room_id": "r-series", "match_id": "m-01",
                "game_number": 1, "seat_plan": {"a": 0, "b": 1},
                "seed": list(range(8)), "continuous_series": True,
                "players": {"a": {"name": "A"}, "b": {"name": "B"}},
            }, "127.0.0.1", 7711, "", "")
            self.assertIn(",m,", session["room_command"])
            command = session["room_command"]
            session["game_started"] = True
            store.observe_start(session, "a", bytes((P.MSG_START, 0)))
            store.observe_start(session, "b", bytes((P.MSG_START, 1)))
            first = store.finish(session, 0, 1)
            store.advance_series(session, {
                "game_number": 2, "seat_plan": {"a": 1, "b": 0},
                "seed": list(range(8)), "seed_mode": "mirrored",
                "required_first_player": "b",
            })
            store._save(session)
            self.assertEqual(session["room_command"], command)
            self.assertEqual(session["status"], "dueling")
            self.assertIsNone(session["result"])
            self.assertEqual(session["engine_seats"], {})
            self.assertNotIn("seed", public_session(session)["results"][0])
            store.observe_start(session, "a", bytes((P.MSG_START, 1)))
            store.observe_start(session, "b", bytes((P.MSG_START, 0)))
            session["game_started"] = True
            second = store.finish(session, 0, 1)
            self.assertEqual(second["winner"], "b")
            self.assertNotEqual(first["event_id"], second["event_id"])
            store.advance_series(session, None)
            store._save(session)
            self.assertEqual(session["status"], "finished")
            self.assertEqual([r["game_number"] for r in session["results"]], [1, 2])

    def test_stale_live_sessions_are_closed_on_load(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-1",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            session["updated_at"] = time.time() - SESSION_TTL - 1
            store._save(session)
            # _save refreshes updated_at, so emulate a restored old file.
            session["updated_at"] = time.time() - SESSION_TTL - 1
            store.sessions[session["id"]] = session

            self.assertEqual(store.expire_stale(), 1)
            self.assertEqual(session["status"], "error")
            self.assertEqual(session["error"], "session_expired")

    def test_restart_turns_persisted_duel_connections_into_reconnectable_seats(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            store = PvpStore(path)
            session = store.create(
                {
                    "room_id": "r-restart",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            session["status"] = "dueling"
            session["connections"]["a"].update({
                "connected": True,
                "name": "A",
                "last_name": "A",
                "connection_id": "c-old",
            })
            store._save(session)

            restored = PvpStore(path)
            loaded = restored.sessions[session["id"]]
            self.assertEqual(restored.restore_connections_after_restart(), 0)
            self.assertFalse(loaded["connections"]["a"]["connected"])
            self.assertEqual(loaded["connections"]["a"]["last_name"], "A")
            self.assertGreater(
                loaded["connections"]["a"]["reconnect_until"], time.time()
            )
            reservation = restored.reserve(loaded, "player", "a", "New name")
            self.assertEqual(reservation["engine_name"], "A")

    def test_restart_repairs_legacy_and_drifted_continuous_generations(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            store = PvpStore(path)
            legacy = store.create(
                {
                    "room_id": "r-legacy-generation",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "seed": list(range(8)),
                    "continuous_series": True,
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            legacy.pop("game_generation")
            legacy.pop("started_generation")
            store._save(legacy)

            drifted = store.create(
                {
                    "room_id": "r-drifted-generation",
                    "match_id": "m-01",
                    "game_number": 2,
                    "seat_plan": {"a": 1, "b": 0},
                    "seed": list(range(8)),
                    "continuous_series": True,
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            drifted["game_generation"] = 3
            drifted["started_generation"] = 3
            drifted["results"] = [{
                "game_number": 1,
                "generation": 1,
                "seed": list(range(8)),
                "seat_plan": {"a": 0, "b": 1},
                "result": {"winner": "a"},
            }]
            store._save(drifted)

            restored = PvpStore(path)
            loaded_legacy = restored.sessions[legacy["id"]]
            loaded_drifted = restored.sessions[drifted["id"]]
            self.assertEqual(loaded_legacy["game_generation"], 1)
            self.assertIsNone(loaded_legacy["started_generation"])
            self.assertEqual(loaded_drifted["game_generation"], 2)
            self.assertIsNone(loaded_drifted["started_generation"])

            loaded_legacy["game_started"] = True
            store = restored
            store.observe_start(loaded_legacy, "a", bytes((P.MSG_START, 0)))
            store.observe_start(loaded_legacy, "b", bytes((P.MSG_START, 1)))
            store.finish(loaded_legacy, 0, 1, 1)
            store.advance_series(loaded_legacy, {
                "game_number": 2,
                "seat_plan": {"a": 1, "b": 0},
                "seed": list(range(8)),
                "seed_mode": "mirrored",
                "required_first_player": "b",
            })
            self.assertEqual(loaded_legacy["game_number"], 2)
            self.assertEqual(loaded_legacy["game_generation"], 2)
            self.assertEqual(loaded_legacy["results"][0]["generation"], 1)

    def test_continuous_series_reconnect_survives_deadline_restart_and_ttl(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            store = PvpStore(path)
            session = store.create(
                {
                    "room_id": "r-series-reconnect",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "seed": list(range(8)),
                    "continuous_series": True,
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            first = store.reserve(session, "player", "a", "A")
            store.mark_dueling(session)
            store.release(session, "player", "a", first["connection_id"])

            connection = session["connections"]["a"]
            self.assertEqual(reconnect_scope(session, connection), "series")
            self.assertIsNone(connection["reconnect_until"])
            self.assertTrue(public_session(session)["connections"]["a"]["reconnecting"])
            self.assertEqual(
                public_session(session)["connections"]["a"]["reconnect_scope"],
                "series",
            )

            # Simulate metadata written by the previous three-minute policy.
            connection["reconnect_until"] = time.time() - 1
            session["updated_at"] = time.time() - SESSION_TTL - 1
            store.sessions[session["id"]] = session
            self.assertEqual(store.expire_stale(), 0)
            self.assertEqual(session["status"], "dueling")
            store._save(session)

            restored = PvpStore(path)
            loaded = restored.sessions[session["id"]]
            self.assertEqual(reconnect_scope(
                loaded, loaded["connections"]["a"]
            ), "series")
            self.assertIsNone(loaded["connections"]["a"]["reconnect_until"])
            reservation = restored.reserve(loaded, "player", "a", "Changed")
            self.assertEqual(reservation["engine_name"], "A")

            restored.release(
                loaded, "player", "a", reservation["connection_id"]
            )
            restored.advance_series(loaded, None)
            restored._save(loaded)
            self.assertIsNone(reconnect_scope(
                loaded, loaded["connections"]["a"]
            ))
            with self.assertRaisesRegex(
                ValueError, "no longer accepting connections"
            ):
                restored.reserve(loaded, "player", "a", "A")

    def test_continuous_series_lobby_leave_keeps_identity_and_seat(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-series-lobby-reconnect",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "seed": list(range(8)),
                    "continuous_series": True,
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            first = store.reserve(session, "player", "a", "A")
            store.reserve(session, "player", "b", "B")
            self.assertEqual(session["status"], "ready")

            store.release(session, "player", "a", first["connection_id"])
            self.assertEqual(session["status"], "waiting")
            self.assertTrue(continuous_series_open(session))
            self.assertEqual(
                reconnect_scope(session, session["connections"]["a"]),
                "series",
            )
            self.assertIsNone(session["connections"]["a"]["reconnect_until"])

            replacement = store.reserve(
                session, "player", "a", "Changed-name"
            )
            self.assertEqual(replacement["engine_name"], "A")
            self.assertEqual(session["status"], "ready")

    def test_restart_clears_stale_lobby_names_for_retry(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            store = PvpStore(path)
            session = store.create(
                {
                    "room_id": "r-restart-lobby",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            session["connections"]["a"].update({
                "last_name": "Human",
                "connection_id": None,
            })
            store._save(session)

            restored = PvpStore(path)
            loaded = restored.sessions[session["id"]]
            self.assertIsNone(loaded["connections"]["a"]["last_name"])
            reservation = restored.reserve(loaded, "player", "a", "Player-A")
            self.assertEqual(reservation["engine_name"], "A")

    def test_either_seat_can_reserve_first_and_result_maps_to_tournament_side(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-1",
                    "match_id": "m-01",
                    "game_number": 11,
                    "seat_plan": {"a": 1, "b": 0},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            store.reserve(session, "player", "a", "A")
            self.assertEqual(session["status"], "connecting")
            store.reserve(session, "player", "b", "B")
            self.assertEqual(session["status"], "ready")
            result = store.finish(session, 0, 1)
            self.assertEqual(result["winner"], "b")

    def test_active_duel_reconnect_window_preserves_seat_and_ignores_stale_release(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-reconnect",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )

            first = store.reserve(session, "player", "a", "A")
            store.mark_dueling(session)
            store.release(session, "player", "a", first["connection_id"])
            self.assertFalse(session["connections"]["a"]["connected"])
            self.assertGreater(
                session["connections"]["a"]["reconnect_until"], time.time()
            )

            second = store.reserve(session, "player", "a", "Changed")
            self.assertEqual(second["engine_name"], "A")
            self.assertTrue(session["connections"]["a"]["connected"])

            # The old relay may finish after the replacement connection is live.
            store.release(session, "player", "a", first["connection_id"])
            self.assertTrue(session["connections"]["a"]["connected"])
            store.release(session, "player", "a", second["connection_id"])
            self.assertFalse(session["connections"]["a"]["connected"])

    def test_player_connection_health_reports_stall_disconnect_and_reconnect(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-health",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            connection = session["connections"]["a"]
            self.assertEqual(connection_state(session, connection), "waiting")

            reservation = store.reserve(session, "player", "a", "A")
            self.assertEqual(connection_state(session, connection), "online")
            store.mark_dueling(session)
            connection["last_activity_at"] = (
                time.time() - PLAYER_STALL_WARNING_SECONDS - 1
            )
            self.assertEqual(connection_state(session, connection), "stalled")
            public = public_session(session)["connections"]["a"]
            self.assertEqual(public["state"], "stalled")
            self.assertIn("last_client_activity_at", public)
            self.assertIn("last_server_activity_at", public)

            store.touch_connection(
                session, "a", "client", reservation["connection_id"]
            )
            self.assertEqual(connection_state(session, connection), "online")
            self.assertGreater(
                connection["last_client_activity_at"],
                time.time() - 1,
            )

            store.release(
                session, "player", "a", reservation["connection_id"]
            )
            self.assertEqual(connection_state(session, connection), "reconnecting")
            self.assertIsNotNone(connection["disconnected_at"])
            connection["reconnect_until"] = time.time() - 1
            self.assertEqual(connection_state(session, connection), "offline")

    def test_client_nicknames_do_not_change_canonical_engine_identities(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-lobby-name-retry",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )

            first = store.reserve(session, "player", "a", "Human")
            duplicate = store.reserve(session, "player", "b", "Human")
            self.assertEqual(first["engine_name"], "A")
            self.assertEqual(duplicate["engine_name"], "B")
            store.release(
                session, "player", "b", duplicate["connection_id"]
            )

            self.assertIsNone(session["connections"]["b"]["last_name"])
            renamed = store.reserve(session, "player", "b", "Player-B")
            self.assertEqual(renamed["engine_name"], "B")
            self.assertEqual(session["connections"]["b"]["last_name"], "B")

    def test_expired_active_duel_reconnect_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-reconnect-expired",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            first = store.reserve(session, "player", "a", "A")
            store.mark_dueling(session)
            store.release(session, "player", "a", first["connection_id"])
            session["connections"]["a"]["reconnect_until"] = time.time() - 1
            store._save(session)

            with self.assertRaisesRegex(ValueError, "reconnect window"):
                store.reserve(session, "player", "a", "A")

    def test_observed_engine_seats_override_static_plan_after_rps(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-1",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            store.observe_start(session, "a", bytes((P.MSG_START, 1)))
            store.observe_start(session, "b", bytes((P.MSG_START, 0)))

            result = store.finish(session, 1, 1)

            self.assertEqual(result["winner"], "a")
            self.assertEqual(result["engine_winner"], 1)
            self.assertEqual(result["engine_seats"], {"a": 1, "b": 0})

    def test_finished_same_lineage_credentials_route_to_unique_active_session(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            credentials = {
                "a": "stable-player-a",
                "b": "stable-player-b",
                "spectator": "stable-watch-01",
            }
            body = {
                "room_id": "r-series",
                "match_id": "m-01",
                "game_number": 1,
                "seat_plan": {"a": 0, "b": 1},
                "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                "credentials": credentials,
                "seed": list(range(8)),
                "seed_mode": "mirrored",
                "required_first_player": "a",
            }
            finished = []
            for game_number in range(1, 21):
                body["game_number"] = game_number
                body["seat_plan"] = (
                    {"a": 0, "b": 1}
                    if game_number % 2 else {"a": 1, "b": 0}
                )
                body["required_first_player"] = (
                    "a" if game_number % 2 else "b"
                )
                historical = store.create(
                    body, "127.0.0.1", 7711, "", ""
                )
                historical["status"] = "finished"
                store._save(historical)
                finished.append(historical)
            body["game_number"] = 21
            body["seat_plan"] = {"a": 0, "b": 1}
            body["required_first_player"] = "a"
            waiting = store.create(body, "127.0.0.1", 7711, "", "")

            for role, code in credentials.items():
                found = store.find_code(code)
                self.assertIsNotNone(found)
                self.assertEqual(found[0]["id"], waiting["id"])
                self.assertEqual(
                    found[1],
                    "spectator" if role == "spectator" else "player",
                )
            self.assertEqual(len(finished), 20)

    def test_finished_same_lineage_code_chooses_most_complete_newest_replay(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            credentials = {
                "a": "replay-player-a",
                "b": "replay-player-b",
                "spectator": "replay-watch",
            }
            body = {
                "room_id": "r-replay-lineage",
                "match_id": "m-final",
                "game_number": 2,
                "seat_plan": {"a": 1, "b": 0},
                "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                "credentials": credentials,
                "seed": list(range(8)),
                "continuous_series": True,
            }
            shorter = store.create(body, "127.0.0.1", 7711, "", "")
            shorter["status"] = "finished"
            shorter["series_finished"] = True
            archive = make_series_replay(2)
            store.record_series_replay(
                shorter,
                archive,
                validate_series_replay(archive),
            )

            body["game_number"] = 3
            older = store.create(body, "127.0.0.1", 7711, "", "")
            older["status"] = "finished"
            older["series_finished"] = True
            archive = make_series_replay(3)
            store.record_series_replay(
                older, archive, validate_series_replay(archive)
            )
            older["created_at"] = 10
            store._save(older)

            newest = store.create(body, "127.0.0.1", 7711, "", "")
            newest["status"] = "finished"
            newest["series_finished"] = True
            archive = make_series_replay(3)
            store.record_series_replay(
                newest, archive, validate_series_replay(archive)
            )
            newest["created_at"] = 20
            store._save(newest)

            found = store.find_code(credentials["spectator"])

            self.assertIsNotNone(found)
            self.assertEqual(found[0]["id"], newest["id"])

    def test_find_code_rejects_multiple_active_or_cross_lineage_matches(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            credentials = {
                "a": "ambiguous-player-a",
                "b": "ambiguous-player-b",
                "spectator": "ambiguous-watch",
            }
            body = {
                "room_id": "r-ambiguous",
                "match_id": "m-01",
                "game_number": 1,
                "seat_plan": {"a": 0, "b": 1},
                "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                "credentials": credentials,
            }
            first = store.create(body, "127.0.0.1", 7711, "", "")
            body["credentials"] = {
                "a": "other-player-a",
                "b": "other-player-b",
                "spectator": "other-watch",
            }
            second = store.create(body, "127.0.0.1", 7711, "", "")
            second["credentials"] = copy.deepcopy(credentials)
            store._save(second)
            self.assertIsNone(store.find_code(credentials["a"]))

            first["status"] = "finished"
            second["status"] = "finished"
            first["room_id"] = "r-first-lineage"
            second["room_id"] = "r-second-lineage"
            store._save(first)
            store._save(second)
            self.assertIsNone(store.find_code(credentials["spectator"]))

    def test_two_active_rooms_have_distinct_codes_and_provisioning(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            store = PvpStore(root_path / "sessions")
            registry = RoomProvisioningRegistry(root_path / "registry")
            body = {
                "room_id": "r-one", "match_id": "m-01",
                "game_number": 1, "seat_plan": {"a": 0, "b": 1},
                "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                "seed": list(range(8)), "continuous_series": True,
            }
            first = store.create(body, "127.0.0.1", 7711, "", "")
            body["room_id"] = "r-two"
            second = store.create(body, "127.0.0.1", 7711, "", "")
            first_path = registry.publish(first, load_ydk(FIXED_DECK))
            second_path = registry.publish(second, load_ydk(FIXED_DECK))

            self.assertNotEqual(first["room_command"], second["room_command"])
            self.assertNotEqual(first_path, second_path)
            self.assertTrue(first_path.exists() and second_path.exists())
            self.assertEqual(
                json.loads(first_path.read_text(encoding="utf-8"))["session_id"],
                first["id"],
            )
            self.assertEqual(
                json.loads(second_path.read_text(encoding="utf-8"))["session_id"],
                second["id"],
            )
            for session in (first, second):
                for role, code in session["credentials"].items():
                    found = store.find_code(code)
                    self.assertEqual(found[0]["id"], session["id"])
                    self.assertEqual(found[1], "spectator" if role == "spectator" else "player")
            self.assertTrue(set(first["credentials"].values()).isdisjoint(
                second["credentials"].values()
            ))

    def test_rejects_code_collision_until_old_session_is_closed(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            credentials = {
                "a": "shared-code-a", "b": "shared-code-b",
                "spectator": "shared-watch",
            }
            body = {
                "room_id": "r-one", "match_id": "m-01",
                "game_number": 1, "seat_plan": {"a": 0, "b": 1},
                "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                "credentials": credentials,
            }
            first = store.create(body, "127.0.0.1", 7711, "", "")
            body["room_id"] = "r-two"
            with self.assertRaisesRegex(ValueError, "another active room"):
                store.create(body, "127.0.0.1", 7711, "", "")
            self.assertEqual(len(store.sessions), 1)
            first["status"] = "finished"
            store._save(first)
            with self.assertRaisesRegex(ValueError, "already in use"):
                store.create(body, "127.0.0.1", 7711, "", "")
            store.close(first["id"])
            second = store.create(body, "127.0.0.1", 7711, "", "")
            self.assertEqual(store.find_code(credentials["spectator"])[0]["id"], second["id"])

    def test_room_registry_contains_seed_and_enforced_first_player(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            store = PvpStore(root_path / "sessions")
            session = store.create(
                {
                    "room_id": "r-series",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "players": {
                        "a": {"name": "A"},
                        "b": {"name": "B"},
                    },
                    "seed": list(range(8)),
                    "seed_mode": "mirrored",
                    "required_first_player": "a",
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            from pvp.server import RoomProvisioningRegistry

            target = RoomProvisioningRegistry(
                root_path / "registry"
            ).publish(session, load_ydk(FIXED_DECK))
            value = json.loads(target.read_text(encoding="utf-8"))
            self.assertNotIn(
                "registry_path", public_session(session)["provisioning"]
            )

            self.assertEqual(value["seed"], list(range(8)))
            self.assertEqual(value["seat_plan"], {"a": 0, "b": 1})
            self.assertEqual(value["players"], {"a": "A", "b": "B"})
            self.assertEqual(value["required_first_player"], "a")
            self.assertTrue(value["full_spectator"])
            self.assertEqual(value["time_limit"], 600)
            self.assertEqual(
                value["fixed_deck"]["canonical_sha256"],
                "43503f002556c020751d1dea290f87c82d86020f96aa62a752c3bcc10de7b7e6",
            )

    def test_non_seeded_provisioning_keeps_full_spectator_flag_without_forcing_first_player(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            store = PvpStore(root_path / "sessions")
            session = store.create(
                {
                    "room_id": "r-ordinary",
                    "match_id": "m-ordinary",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "players": {
                        "a": {"name": "A"},
                        "b": {"name": "B"},
                    },
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            target = RoomProvisioningRegistry(
                root_path / "registry"
            ).publish(session, load_ydk(FIXED_DECK))
            value = json.loads(target.read_text(encoding="utf-8"))
            self.assertIsNone(value["seed"])
            self.assertIsNone(value["required_first_player"])
            self.assertTrue(value["full_spectator"])
            self.assertEqual(value["time_limit"], 600)

    def test_finished_series_replay_allows_only_spectators_and_hides_path(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-replay",
                    "match_id": "m-replay",
                    "game_number": 2,
                    "seat_plan": {"a": 1, "b": 0},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                    "seed": list(range(8)),
                    "continuous_series": True,
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            session["status"] = "finished"
            session["series_finished"] = True
            archive = make_series_replay()
            envelope = validate_series_replay(archive)
            store.record_series_replay(session, archive, envelope)

            self.assertTrue(series_replay_available(session))
            self.assertEqual(session["series_replay"]["duels"], 2)
            self.assertNotIn("available_until", session["series_replay"])
            self.assertNotIn("path", public_session(session)["series_replay"])
            spectator = store.reserve(session, "spectator", None, "Watcher")
            self.assertIsNone(spectator["connection_id"])
            with self.assertRaisesRegex(
                ValueError, "no longer accepting connections"
            ):
                store.reserve(session, "player", "a", "A")

            session["series_replay"]["available_until"] = time.time() - 1
            store._save(session)
            self.assertTrue(series_replay_available(session))
            store.close(session["id"])
            self.assertFalse(series_replay_available(session))
            with self.assertRaisesRegex(
                ValueError, "no longer accepting connections"
            ):
                store.reserve(session, "spectator", None, "Late watcher")

    def test_audited_partial_series_replay_is_playable_and_reports_missing_games(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            credentials = {
                "a": "partial-player-a",
                "b": "partial-player-b",
                "spectator": "partial-watch",
            }
            session = store.create(
                {
                    "room_id": "r-partial-replay",
                    "match_id": "m-partial-replay",
                    "game_number": 2,
                    "seat_plan": {"a": 1, "b": 0},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                    "credentials": credentials,
                    "seed": list(range(8)),
                    "continuous_series": True,
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            archive = make_series_replay(1)
            target = store.replay_root / f"{session['id']}.evrp"
            target.write_bytes(archive)
            session["status"] = "finished"
            session["series_finished"] = True
            session["series_replay"] = {
                "path": str(target),
                "bytes": len(archive),
                "sha256": hashlib.sha256(archive).hexdigest(),
                "duels": 1,
                "partial": True,
                "ordinals": [2],
                "missing_ordinals": [1],
            }
            store._save(session)

            self.assertFalse(series_replay_stored(session))
            self.assertTrue(partial_series_replay_stored(session))
            self.assertTrue(series_replay_available(session))
            self.assertEqual(
                series_replay_status(session),
                {
                    "state": "partial",
                    "available": True,
                    "complete": False,
                    "partial": True,
                    "replay_count": 1,
                    "expected_replay_count": 2,
                    "missing_game_numbers": [1],
                },
            )
            self.assertEqual(
                store.find_code(credentials["spectator"])[0]["id"],
                session["id"],
            )
            self.assertEqual(
                public_session(session)["replay_status"]["state"],
                "partial",
            )

    def test_administrative_seal_keeps_spectator_code_for_completed_games(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            credentials = {
                "a": "sealed-player-a",
                "b": "sealed-player-b",
                "spectator": "sealed-watch",
            }
            session = store.create(
                {
                    "room_id": "r-sealed-replay",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                    "credentials": credentials,
                    "seed": list(range(8)),
                    "continuous_series": True,
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            store.finish(session, 0, 1, 1)
            store.advance_series(session, {
                "game_number": 2,
                "seat_plan": {"a": 1, "b": 0},
                "seed": list(range(8)),
                "seed_mode": "mirrored",
                "required_first_player": "b",
            })
            store._save(session)

            sealed = store.seal(session["id"], "player withdrew")

            self.assertEqual(sealed["status"], "finished")
            self.assertTrue(sealed["series_finished"])
            self.assertEqual(sealed["replay_status"]["expected_replay_count"], 1)
            self.assertEqual(session["sealed_expected_duels"], 1)
            with self.assertRaisesRegex(
                ValueError, "no longer accepting connections"
            ):
                store.reserve(session, "player", "a", "A")

            archive_envelope = json.loads(gzip.decompress(
                make_series_replay(1)
            ))
            archive_envelope["duels"][0]["gameNumber"] = 1
            archive = gzip.compress(json.dumps(
                archive_envelope, separators=(",", ":")
            ).encode("utf-8"))
            identity = {
                "session_id": session["id"],
                "room_id": session["room_id"],
                "match_id": session["match_id"],
                "duels": 1,
                "expected_duels": 1,
                "ordinals": [1],
                "missing_ordinals": [],
                "sha256": hashlib.sha256(archive).hexdigest(),
            }
            self.assertTrue(store.install_recovered_series_replay(
                session, archive, identity
            ))
            self.assertTrue(series_replay_available(session))
            found = store.find_code(credentials["spectator"])
            self.assertIsNotNone(found)
            self.assertEqual(found[0]["id"], session["id"])
            self.assertEqual(found[1], "spectator")

    def test_administrative_seal_does_not_count_an_unfinished_first_game(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-empty-seal",
                    "match_id": "m-01",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                    "seed": list(range(8)),
                    "continuous_series": True,
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )

            sealed = store.seal(session["id"], "no completed duel")

            self.assertEqual(sealed["replay_status"]["replay_count"], 0)
            self.assertEqual(sealed["replay_status"]["expected_replay_count"], 0)
            self.assertFalse(sealed["replay_status"]["available"])

    def test_full_evrp_replaces_short_archive_and_checksum_is_verified(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            session = store.create(
                {
                    "room_id": "r-replay-replace",
                    "match_id": "m-replay-replace",
                    "game_number": 1,
                    "seat_plan": {"a": 0, "b": 1},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                    "seed": list(range(8)),
                    "continuous_series": True,
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            short = make_series_replay(1)
            full = make_series_replay(2)
            self.assertTrue(store.record_series_replay(
                session, short, validate_series_replay(short)
            ))
            self.assertTrue(series_replay_stored(session))

            session["game_number"] = 2
            session["game_generation"] = 2
            session["status"] = "finished"
            session["series_finished"] = True
            store._save(session)
            self.assertFalse(series_replay_stored(session))
            self.assertTrue(store.record_series_replay(
                session, full, validate_series_replay(full)
            ))
            self.assertTrue(series_replay_stored(session))
            self.assertEqual(session["series_replay"]["duels"], 2)

            replay_path = Path(session["series_replay"]["path"])
            damaged = bytearray(replay_path.read_bytes())
            damaged[-1] ^= 0x01
            replay_path.write_bytes(damaged)
            self.assertFalse(series_replay_stored(session))
            self.assertTrue(store.record_series_replay(
                session, full, validate_series_replay(full)
            ))
            self.assertTrue(series_replay_stored(session))

    def test_series_replay_validation_rejects_raw_omniscient_only_archive(self):
        frame = base64.b64encode(
            P.packet(P.GAME_MSG, bytes((P.MSG_START, 0)))
        ).decode("ascii")
        archive = gzip.compress(json.dumps({
            "version": 1,
            "meta": {},
            "duels": [{
                "seed": list(range(8)),
                "frames": [frame],
            }],
        }).encode("utf-8"))

        with self.assertRaisesRegex(ValueError, "spectator frame"):
            validate_series_replay(archive)


class ReplayIdentityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = PvpStore(Path(self.temp.name))
        self.seed = list(range(8))
        self.started_at = 1791200000
        self.session = self.store.create({
            "room_id": "r-identity", "match_id": "m-identity",
            "game_number": 1, "seat_plan": {"a": 0, "b": 1},
            "seed": self.seed, "continuous_series": True,
            "players": {"a": {"name": "A"}, "b": {"name": "B"}},
        }, "127.0.0.1", 7711, "", "")
        self.session["created_at"] = self.started_at - 10
        for number in range(1, 4):
            result = self.store.finish(self.session, 0, 1, number)
            result["recorded_at"] = self.started_at + number * 60 - 10
            first = "b" if number % 2 else "a"
            self.store.advance_series(self.session, {
                "game_number": number + 1,
                "seat_plan": {first: 0, "a" if first == "b" else "b": 1},
                "seed": self.seed, "seed_mode": "mirrored",
                "required_first_player": first,
            } if number < 3 else None)

    def replay(self, number, suffix=b"", seed=None, started_at=None):
        payload = bytearray(64)
        payload[:4] = b"yrp2"
        struct.pack_into("<I", payload, 20, started_at if started_at is not None
                         else self.started_at + (number - 1) * 60 + 1)
        struct.pack_into("<8I", payload, 32, *(self.seed if seed is None else seed))
        return bytes(payload) + suffix

    def test_retransmission_with_explicit_new_hint_does_not_fill_new_game(self):
        first = self.replay(1)
        self.store.record_replay(self.session, first, "a", 1, 1)
        replayed = self.store.record_replay(self.session, first, "b", 3, 3)
        self.assertEqual(replayed["ordinal"], 1)
        self.assertEqual([entry["ordinal"] for entry in self.session["series_replays"]], [1])
        third = self.store.record_replay(self.session, self.replay(3), "b", 3, 3)
        self.assertEqual(third["ordinal"], 3)

    def test_first_delivery_and_out_of_order_delivery_use_timestamp_not_queue(self):
        second = self.store.record_replay(self.session, self.replay(2), "b", 3, 3)
        first = self.store.record_replay(self.session, self.replay(1), "b")
        self.assertEqual((second["ordinal"], first["ordinal"]), (2, 1))
        self.assertEqual([entry["ordinal"] for entry in self.session["series_replays"]], [1, 2])

    def test_foreign_seed_is_preserved_without_polluting_canonical_replays(self):
        payload = self.replay(3, seed=list(range(8, 16)))
        self.assertIsNone(self.store.record_replay(self.session, payload, "b", 3, 3))
        self.assertEqual(self.session["series_replays"], [])
        conflict = self.session["replay_conflicts"][0]
        self.assertEqual(Path(conflict["path"]).read_bytes(), payload)

    def test_impossible_start_time_is_not_bound_to_current_game(self):
        payload = self.replay(3, started_at=self.started_at + 500)
        self.assertIsNone(self.store.record_replay(self.session, payload, "b", 3, 3))
        self.assertEqual(self.session["series_replays"], [])

    def test_different_bytes_for_old_game_keep_conflict_on_actual_game(self):
        first = self.replay(1, b"first-source")
        self.store.record_replay(self.session, first, "a", 1, 1)
        self.assertIsNone(self.store.record_replay(
            self.session, self.replay(1, b"second-source"), "b", 3, 3,
        ))
        self.assertEqual(self.session["replay_conflicts"][0]["ordinal"], 1)
        self.assertEqual(len(self.session["series_replays"]), 1)

    def test_same_second_mirror_games_preserve_valid_explicit_generation(self):
        self.session["results"][0]["result"]["recorded_at"] = self.started_at + 1.2
        self.session["results"][1]["result"]["recorded_at"] = self.started_at + 1.9
        payload = self.replay(1)
        self.store.record_replay(self.session, payload, "a", 1, 1)
        second = self.store.record_replay(self.session, payload, "a", 2, 2)
        self.assertEqual(second["ordinal"], 2)
        self.assertEqual(len(self.session["series_replays"]), 2)

    def test_ambiguous_header_without_hint_is_quarantined(self):
        self.session["results"][0]["result"]["recorded_at"] = self.started_at + 1.2
        self.session["results"][1]["result"]["recorded_at"] = self.started_at + 1.9
        self.assertIsNone(self.store.record_replay(self.session, self.replay(1), "b"))
        self.assertEqual(self.session["series_replays"], [])

    def test_historical_repair_restores_verified_conflict_without_changing_scores(self):
        from tools.repair_replay_bindings import repair_candidate
        for number in range(1, 4):
            self.store.record_replay(self.session, self.replay(number), "a", number, number)
        original = self.session["series_replays"][0]
        broken = self.session["series_replays"][2]
        for key in ("path", "bytes", "sha256"):
            broken[key] = original[key]
        self.store._record_replay_conflict(self.session, self.replay(3), "b", 3, 3)
        envelope = json.loads(gzip.decompress(make_series_replay(3)))
        for number, duel in enumerate(envelope["duels"], 1):
            duel["gameNumber"] = number
            duel["startTime"] = datetime.fromtimestamp(
                self.started_at + (number - 1) * 60 + 1, timezone.utc
            ).isoformat()
            duel["endTime"] = datetime.fromtimestamp(
                self.started_at + number * 60 - 10, timezone.utc
            ).isoformat()
        archive = gzip.compress(json.dumps(envelope).encode())
        self.store.record_series_replay(self.session, archive, envelope)
        before = copy.deepcopy(self.session)
        candidate, changes = repair_candidate(self.store, self.session)
        self.assertEqual(self.session, before)
        self.assertEqual([item["ordinal"] for item in changes], [3])
        self.assertEqual(
            [item["result"]["winner"] for item in candidate["results"]],
            [item["result"]["winner"] for item in before["results"]],
        )
        with zipfile.ZipFile(io.BytesIO(self.store.playable_replay_archive(candidate))) as exported:
            self.assertTrue(json.loads(exported.read("manifest.json"))["complete"])
            self.assertEqual(len(exported.namelist()), 4)


class ReplayIdentityGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_old_retransmission_does_not_consume_pending_new_replay(self):
        fixture = ReplayIdentityTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        application = PvpApplication(Path(fixture.temp.name), "test", "127.0.0.1", 7711)
        application.publish_result = AsyncMock()
        application.publish_replay_supplement = AsyncMock()
        gateway = NativeGateway(application, "127.0.0.1", 0)
        state = {"completed_games": [(3, 3)]}

        class MemoryWriter:
            def write(self, _raw):
                pass

            async def drain(self):
                pass

        for number in (1, 3):
            reader = asyncio.StreamReader()
            reader.feed_data(P.packet(P.REPLAY, fixture.replay(number)))
            reader.feed_eof()
            with self.assertRaises(asyncio.IncompleteReadError):
                await gateway._backend_to_client(
                    reader, MemoryWriter(), fixture.session,
                    "player", "b", False, state,
                )
            self.assertEqual(state["completed_games"], [(3, 3)] if number == 1 else [])


class SeriesReplayArchiveTest(unittest.IsolatedAsyncioTestCase):
    async def test_game_21_session_merges_persisted_twenty_duel_prefix(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            application = PvpApplication(
                root_path / "sessions",
                "internal",
                "127.0.0.1",
                7711,
                room_registry_dir=root_path / "registry",
            )
            try:
                prefix_archive = make_series_replay(20)
                prefix_path = (
                    application.store.replay_root / "m-final-prefix.evrp"
                )
                prefix_path.write_bytes(prefix_archive)
                session = application.store.create(
                    {
                        "room_id": "r-c54c8de4",
                        "match_id": "m-final",
                        "game_number": 21,
                        "seat_plan": {"a": 1, "b": 0},
                        "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                        "seed": list(range(8)),
                        "continuous_series": True,
                    },
                    "127.0.0.1",
                    7711,
                    "",
                    "",
                )
                prefix = application.store.set_series_replay_prefix(
                    session, prefix_reference(session, prefix_path)
                )
                self.assertEqual(prefix["duels"], 20)
                self.assertEqual(prefix["session_id"], session["id"])
                self.assertEqual(prefix["room_id"], session["room_id"])
                self.assertEqual(prefix["match_id"], session["match_id"])
                self.assertNotIn(
                    "path", public_session(session)["series_replay_prefix"]
                )

                # A prefix is only an input to the final archive. It must not
                # become a finished replay by itself.
                session["status"] = "finished"
                session["series_finished"] = True
                application.store._save(session)
                self.assertFalse(series_replay_available(session))
                restored = PvpStore(root_path / "sessions")
                self.assertEqual(
                    restored.sessions[session["id"]][
                        "series_replay_prefix"
                    ]["sha256"],
                    hashlib.sha256(prefix_archive).hexdigest(),
                )

                current_archive = make_series_replay(1)
                wire = (
                    bytes((1,))
                    + struct.pack("<HH", 0, 1)
                    + current_archive
                )
                self.assertTrue(
                    await application.record_series_replay_chunk(session, wire)
                )
                self.assertTrue(series_replay_available(session))
                self.assertEqual(session["series_replay"]["duels"], 21)

                merged_payload = Path(
                    session["series_replay"]["path"]
                ).read_bytes()
                merged = validate_series_replay(merged_payload)
                prefix_envelope = validate_series_replay(prefix_archive)
                current_envelope = validate_series_replay(current_archive)
                self.assertEqual(
                    merged["duels"][:20], prefix_envelope["duels"]
                )
                self.assertEqual(
                    merged["duels"][20:], current_envelope["duels"]
                )
            finally:
                await application.close()

    async def test_prefix_duel_count_must_end_before_current_game(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            store = PvpStore(root_path / "sessions")
            prefix_path = store.replay_root / "short-prefix.evrp"
            prefix_path.write_bytes(make_series_replay(19))
            session = store.create(
                {
                    "room_id": "r-prefix-invalid",
                    "match_id": "m-final",
                    "game_number": 21,
                    "seat_plan": {"a": 1, "b": 0},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                    "seed": list(range(8)),
                    "continuous_series": True,
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )

            with self.assertRaisesRegex(ValueError, "precede game_number"):
                store.set_series_replay_prefix(
                    session, prefix_reference(session, prefix_path)
                )
            self.assertIsNone(session["series_replay_prefix"])

    async def test_replay_prefix_requires_exact_target_identity(self):
        with tempfile.TemporaryDirectory() as root:
            store = PvpStore(Path(root))
            prefix_path = store.replay_root / "identity-prefix.evrp"
            prefix_path.write_bytes(make_series_replay(20))
            session = store.create(
                {
                    "room_id": "r-prefix-identity",
                    "match_id": "m-final",
                    "game_number": 21,
                    "seat_plan": {"a": 1, "b": 0},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                    "seed": list(range(8)),
                    "continuous_series": True,
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            reference = prefix_reference(session, prefix_path)

            for field in ("session_id", "room_id", "match_id"):
                with self.subTest(field=field, case="mismatch"):
                    invalid = copy.deepcopy(reference)
                    invalid[field] = "wrong-value"
                    with self.assertRaisesRegex(ValueError, field):
                        store.set_series_replay_prefix(session, invalid)
                with self.subTest(field=field, case="missing"):
                    invalid = copy.deepcopy(reference)
                    invalid.pop(field)
                    with self.assertRaisesRegex(ValueError, field):
                        store.set_series_replay_prefix(session, invalid)
            self.assertIsNone(session["series_replay_prefix"])

    async def test_complete_evrp_is_kept_when_it_arrives_before_final_callback(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            application = PvpApplication(
                root_path / "sessions",
                "internal",
                "127.0.0.1",
                7711,
                room_registry_dir=root_path / "registry",
            )
            try:
                session = application.store.create(
                    {
                        "room_id": "r-early-replay",
                        "match_id": "m-early-replay",
                        "game_number": 2,
                        "seat_plan": {"a": 1, "b": 0},
                        "players": {
                            "a": {"name": "A"},
                            "b": {"name": "B"},
                        },
                        "seed": list(range(8)),
                        "continuous_series": True,
                    },
                    "127.0.0.1",
                    7711,
                    "",
                    "",
                )
                session["status"] = "dueling"
                archive = make_series_replay(2)
                wire = bytes((1,)) + struct.pack("<HH", 0, 1) + archive

                self.assertTrue(
                    await application.record_series_replay_chunk(
                        session,
                        wire,
                    )
                )
                self.assertIsNotNone(session.get("series_replay"))
                self.assertFalse(series_replay_available(session))

                application.store.advance_series(session, None)
                application.store._save(session)
                self.assertTrue(series_replay_available(session))
            finally:
                await application.close()

    async def test_chunks_reassemble_out_of_order_and_publish_replay_room(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            application = PvpApplication(
                root_path / "sessions",
                "internal",
                "127.0.0.1",
                7711,
                room_registry_dir=root_path / "registry",
            )
            try:
                session = application.store.create(
                    {
                        "room_id": "r-replay-chunks",
                        "match_id": "m-replay-chunks",
                        "game_number": 2,
                        "seat_plan": {"a": 1, "b": 0},
                        "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                        "seed": list(range(8)),
                        "continuous_series": True,
                    },
                    "127.0.0.1",
                    7711,
                    "",
                    "",
                )
                session["status"] = "finished"
                session["series_finished"] = True
                application.store._save(session)
                archive = make_series_replay(2)
                chunk_size = max(1, len(archive) // 3)
                chunks = [
                    archive[offset:offset + chunk_size]
                    for offset in range(0, len(archive), chunk_size)
                ]
                count = len(chunks)

                def wire(index):
                    return (
                        bytes((1,))
                        + struct.pack("<HH", index, count)
                        + chunks[index]
                    )

                order = list(reversed(range(count)))
                self.assertFalse(await application.record_series_replay_chunk(
                    session, wire(order[0])
                ))
                self.assertFalse(await application.record_series_replay_chunk(
                    session, wire(order[0])
                ))
                completed = False
                for index in order[1:]:
                    completed = await application.record_series_replay_chunk(
                        session, wire(index)
                    )
                self.assertTrue(completed)
                self.assertTrue(series_replay_available(session))
                replay_path = Path(session["series_replay"]["path"])
                self.assertEqual(replay_path.read_bytes(), archive)
                provisioning = json.loads(
                    (
                        root_path / "registry"
                        / f"{session['room_command'].split(',', 1)[0]}.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertTrue(provisioning["series_finished"])
                self.assertEqual(
                    provisioning["series_replay"]["sha256"],
                    hashlib.sha256(archive).hexdigest(),
                )
            finally:
                await application.close()

    async def test_startup_republishes_finished_replay_even_with_expired_legacy_ttl(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            store = PvpStore(root_path / "sessions")
            session = store.create(
                {
                    "room_id": "r-replay-restart",
                    "match_id": "m-replay-restart",
                    "game_number": 2,
                    "seat_plan": {"a": 1, "b": 0},
                    "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                    "seed": list(range(8)),
                    "continuous_series": True,
                },
                "127.0.0.1",
                7711,
                "",
                "",
            )
            session["status"] = "finished"
            session["series_finished"] = True
            archive = make_series_replay()
            store.record_series_replay(
                session, archive, validate_series_replay(archive)
            )
            session["series_replay"]["available_until"] = time.time() - 1
            store._save(session)

            application = PvpApplication(
                root_path / "sessions",
                "internal",
                "127.0.0.1",
                7711,
                room_registry_dir=root_path / "registry",
            )
            try:
                room_key = session["room_command"].split(",", 1)[0]
                published = json.loads(
                    (root_path / "registry" / f"{room_key}.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    published["series_replay"]["bytes"], len(archive)
                )
            finally:
                await application.close()


class NativeGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend_join = None
        self.backend_join_version = None
        self.backend_join_order = []
        self.backend_prefix = []
        self.backend_messages = []
        self.backend_frames = None
        self.backend_hold_open = False
        self.backend_release = asyncio.Event()

        async def backend(reader, writer):
            player_name = ""
            try:
                while True:
                    opcode, payload, _raw = await P.read_frame(reader)
                    if opcode == P.PLAYER_INFO:
                        player_name = P.decode_name(payload)
                    if opcode == P.JOIN_GAME:
                        self.backend_join = P.join_password(payload)
                        self.backend_join_version = P.client_version(payload)
                        self.backend_join_order.append(player_name)
                        if self.backend_hold_open:
                            writer.write(P.packet(P.JOIN_GAME))
                            await writer.drain()
                            await self.backend_release.wait()
                            return
                        if self.backend_frames is not None:
                            for response_opcode, response_payload in self.backend_frames:
                                writer.write(P.packet(
                                    response_opcode, response_payload
                                ))
                            await writer.drain()
                            await asyncio.sleep(0.05)
                            return
                        for message in self.backend_messages:
                            writer.write(P.packet(P.GAME_MSG, message))
                        writer.write(P.packet(P.DUEL_START))
                        writer.write(P.packet(P.GAME_MSG, bytes((P.MSG_WIN, 0, 1))))
                        writer.write(P.packet(P.REPLAY, b"yrp1" + bytes(128)))
                        writer.write(P.packet(P.DUEL_END))
                        await writer.drain()
                        await asyncio.sleep(0.05)
                        return
                    self.backend_prefix.append(opcode)
            finally:
                await P.close_writer(writer)

        self.backend = await asyncio.start_server(backend, "127.0.0.1", 0)
        backend_port = self.backend.sockets[0].getsockname()[1]
        self.temp = tempfile.TemporaryDirectory()
        self.application = PvpApplication(
            Path(self.temp.name),
            "internal",
            "127.0.0.1",
            backend_port,
        )
        self.gateway = NativeGateway(self.application, "127.0.0.1", 0)
        await self.gateway.start()
        self.gateway_port = self.gateway.server.sockets[0].getsockname()[1]
        self.session = self.application.store.create(
            {
                "room_id": "r-1",
                "match_id": "m-01",
                "game_number": 1,
                "seat_plan": {"a": 0, "b": 1},
                "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                "seed": list(range(8)),
                "seed_mode": "mirrored",
                "required_first_player": "a",
            },
            "127.0.0.1",
            backend_port,
            "",
            "",
        )

    async def asyncTearDown(self):
        self.backend_release.set()
        self.gateway.server.close()
        await self.gateway.server.wait_closed()
        await self.application.close()
        self.backend.close()
        await self.backend.wait_closed()
        self.temp.cleanup()

    async def test_native_frames_are_relayed_and_result_is_persisted(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway_port)
        writer.write(P.packet(P.PLAYER_INFO, P.encode_name("A")))
        writer.write(
            P.packet(
                P.JOIN_GAME,
                struct.pack("<H2xI", 0x1362, 0) +
                P.encode_name(self.session["credentials"]["a"]),
            )
        )
        await writer.drain()

        received = []
        try:
            while True:
                opcode, _payload, _raw = await asyncio.wait_for(
                    P.read_frame(reader), 2
                )
                received.append(opcode)
                if opcode == P.DUEL_END:
                    break
        finally:
            await P.close_writer(writer)

        await asyncio.sleep(0.05)
        self.assertIn(P.DUEL_START, received)
        self.assertIn(P.GAME_MSG, received)
        self.assertIn(P.REPLAY, received)
        self.assertIn(P.DUEL_END, received)
        self.assertEqual(self.backend_join, self.session["room_command"])
        self.assertEqual(self.backend_join_version, P.ENGINE_VERSION)
        self.assertEqual(self.session["result"]["winner"], "a")
        self.assertEqual(self.session["status"], "finished")
        self.assertIsNotNone(self.session["replay"])

    async def test_between_game_leave_is_dropped_while_following_frames_continue(self):
        class MemoryWriter:
            def __init__(self):
                self.data = bytearray()

            def write(self, value):
                self.data.extend(value)

            async def drain(self):
                return None

        self.session["continuous_series"] = True
        self.session["game_started"] = True
        self.session["status"] = "dueling"
        self.application.store._save(self.session)
        relay_state = {"series_leave_guard_until": 0.0}

        engine_reader = asyncio.StreamReader()
        engine_reader.feed_data(
            P.packet(P.GAME_MSG, bytes((P.MSG_WIN, 0, 1)))
        )
        engine_reader.feed_data(P.packet(P.DUEL_START))
        engine_reader.feed_eof()
        client_writer = MemoryWriter()
        with self.assertRaises(asyncio.IncompleteReadError):
            await self.gateway._backend_to_client(
                engine_reader,
                client_writer,
                self.session,
                "player",
                "a",
                False,
                relay_state,
            )

        await asyncio.sleep(0.9)
        client_reader = asyncio.StreamReader()
        client_reader.feed_data(P.packet(P.LEAVE_GAME))
        client_reader.feed_data(P.packet(0x01, b"\x01"))
        client_reader.feed_eof()
        backend_writer = MemoryWriter()
        with self.assertRaises(asyncio.IncompleteReadError):
            await self.gateway._client_to_backend(
                client_reader,
                backend_writer,
                self.session,
                "player",
                "a",
                relay_state,
            )

        forwarded = asyncio.StreamReader()
        forwarded.feed_data(bytes(backend_writer.data))
        forwarded.feed_eof()
        opcode, payload, _raw = await P.read_frame(forwarded)
        self.assertEqual((opcode, payload), (0x01, b"\x01"))
        with self.assertRaises(asyncio.IncompleteReadError):
            await P.read_frame(forwarded)

    async def test_relay_updates_each_player_direction_activity(self):
        class MemoryWriter:
            def __init__(self):
                self.data = bytearray()

            def write(self, value):
                self.data.extend(value)

            async def drain(self):
                return None

        reservation = self.application.store.reserve(
            self.session, "player", "a", "A"
        )
        connection = self.session["connections"]["a"]
        old_client = connection["last_client_activity_at"]
        old_server = connection["last_server_activity_at"]
        await asyncio.sleep(0.01)

        client_reader = asyncio.StreamReader()
        client_reader.feed_data(P.packet(0x01, b"\x01"))
        client_reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await self.gateway._client_to_backend(
                client_reader,
                MemoryWriter(),
                self.session,
                "player",
                "a",
                connection_id=reservation["connection_id"],
            )
        self.assertGreater(connection["last_client_activity_at"], old_client)

        backend_reader = asyncio.StreamReader()
        backend_reader.feed_data(P.packet(P.JOIN_GAME))
        backend_reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await self.gateway._backend_to_client(
                backend_reader,
                MemoryWriter(),
                self.session,
                "player",
                "a",
                connection_id=reservation["connection_id"],
            )
        self.assertGreater(connection["last_server_activity_at"], old_server)

    async def test_delayed_win_from_previous_relay_generation_is_ignored(self):
        class MemoryWriter:
            def write(self, _value):
                return None

            async def drain(self):
                return None

        session = self.application.store.create(
            {
                "room_id": "r-delayed-win",
                "match_id": "m-delayed-win",
                "game_number": 1,
                "seat_plan": {"a": 0, "b": 1},
                "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                "seed": list(range(8)),
                "continuous_series": True,
            },
            "127.0.0.1",
            7711,
            "",
            "",
        )
        generation_a, _ = self.application.store.bind_relay_start(session, "a")
        generation_b, game_b = self.application.store.bind_relay_start(session, "b")
        self.assertEqual((generation_a, generation_b), (1, 1))
        self.assertTrue(await self.application.finish_result(
            session, 0, 1, generation_a
        ))
        self.application.store.advance_series(session, {
            "game_number": 2,
            "seat_plan": {"a": 1, "b": 0},
            "seed": list(range(8)),
            "seed_mode": "mirrored",
            "required_first_player": "b",
        })
        self.application.store._save(session)
        generation_a, _ = self.application.store.bind_relay_start(session, "a")
        self.assertEqual(generation_a, 2)

        reader = asyncio.StreamReader()
        reader.feed_data(P.packet(
            P.GAME_MSG, bytes((P.MSG_WIN, 1, 1))
        ))
        reader.feed_eof()
        relay_state = {
            "series_leave_guard_until": 0.0,
            "generation": generation_b,
            "game_number": game_b,
            "game_complete": False,
            "completed_games": [],
        }
        with self.assertRaises(asyncio.IncompleteReadError):
            await self.gateway._backend_to_client(
                reader,
                MemoryWriter(),
                session,
                "player",
                "b",
                False,
                relay_state,
            )

        self.assertIsNone(session["result"])
        self.assertEqual(session["game_generation"], 2)
        self.assertEqual(relay_state["completed_games"], [(1, 1)])
        self.assertTrue(relay_state["game_complete"])

        reader = asyncio.StreamReader()
        reader.feed_data(P.packet(P.DUEL_START))
        reader.feed_data(P.packet(
            P.GAME_MSG, bytes((P.MSG_WIN, 0, 1))
        ))
        reader.feed_eof()
        with self.assertRaises(asyncio.IncompleteReadError):
            await self.gateway._backend_to_client(
                reader,
                MemoryWriter(),
                session,
                "player",
                "b",
                False,
                relay_state,
            )

        self.assertEqual(relay_state["generation"], 2)
        self.assertEqual(session["result"]["generation"], 2)
        self.assertEqual(session["result"]["game_number"], 2)

    async def test_seat_one_enters_backend_before_seat_zero_connects(self):
        async def connect(side, name):
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", self.gateway_port
            )
            writer.write(P.packet(P.PLAYER_INFO, P.encode_name(name)))
            writer.write(P.packet(
                P.JOIN_GAME,
                struct.pack("<H2xI", 0x1362, 0)
                + P.encode_name(self.session["credentials"][side]),
            ))
            await writer.drain()
            return reader, writer

        self.backend_hold_open = True
        seat_one_reader, seat_one_writer = await connect("b", "Seat-1")
        seat_zero_reader = seat_zero_writer = None
        try:
            opcode, _payload, _raw = await asyncio.wait_for(
                P.read_frame(seat_one_reader), 2
            )
            self.assertEqual(opcode, P.JOIN_GAME)
            self.assertTrue(self.session["connections"]["b"]["connected"])
            self.assertEqual(self.backend_join_order, ["B"])

            seat_zero_reader, seat_zero_writer = await connect("a", "Seat-0")
            opcode, _payload, _raw = await asyncio.wait_for(
                P.read_frame(seat_zero_reader), 2
            )
            self.assertEqual(opcode, P.JOIN_GAME)
            self.assertEqual(self.backend_join_order, ["B", "A"])
        finally:
            self.backend_release.set()
            await P.close_writer(seat_one_writer)
            if seat_zero_writer is not None:
                await P.close_writer(seat_zero_writer)

    async def test_legacy_join_and_game_messages_are_adapted_without_changing_prefix(self):
        confirm = bytes((31, 1, 0, 1)) + bytes(range(7))
        chain = bytes((16, 0, 1, 3)) + bytes(range(8)) + bytes((0x42, 1)) + bytes(range(12))
        self.backend_messages = [confirm, chain]
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway_port)
        try:
            writer.write(P.packet(0x17, bytes(range(24))))
            writer.write(P.packet(P.PLAYER_INFO, P.encode_name("Legacy-A")))
            writer.write(P.packet(
                P.JOIN_GAME,
                struct.pack("<H2xI", P.LEGACY_VERSION, 0)
                + P.encode_name(self.session["credentials"]["a"]),
            ))
            await writer.drain()
            messages = []
            while True:
                opcode, payload, _ = await asyncio.wait_for(P.read_frame(reader), 2)
                if opcode == P.GAME_MSG:
                    messages.append(payload)
                if opcode == P.DUEL_END:
                    break
            self.assertEqual(messages[:2], [
                bytes((31, 1, 1)) + bytes(range(7)),
                bytes((16, 0, 1, 3, 1)) + bytes(range(8))
                + bytes((0x42,)) + bytes(range(12)),
            ])
            self.assertEqual(self.backend_join_version, P.ENGINE_VERSION)
            self.assertEqual(self.backend_prefix, [0x17, P.PLAYER_INFO])
        finally:
            await P.close_writer(writer)

    async def test_modern_game_message_is_forwarded_without_legacy_conversion(self):
        modern = bytes((31, 1, 0, 1)) + bytes(range(7))
        self.backend_messages = [modern]
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway_port)
        try:
            writer.write(P.packet(P.PLAYER_INFO, P.encode_name("Modern-A")))
            writer.write(P.packet(
                P.JOIN_GAME,
                struct.pack("<H2xI", P.ENGINE_VERSION, 0)
                + P.encode_name(self.session["credentials"]["a"]),
            ))
            await writer.drain()
            while True:
                opcode, payload, _ = await asyncio.wait_for(P.read_frame(reader), 2)
                if opcode == P.GAME_MSG:
                    self.assertEqual(payload, modern)
                    break
        finally:
            await P.close_writer(writer)

    async def test_finished_spectator_receives_full_series_without_export_frames(self):
        self.session["continuous_series"] = True
        self.session["game_number"] = 2
        self.session["game_generation"] = 2
        self.session["status"] = "finished"
        self.session["series_finished"] = True
        archive = make_series_replay(2)
        self.application.store.record_series_replay(
            self.session, archive, validate_series_replay(archive)
        )
        first_start = bytes((P.MSG_START, 0))
        first_win = bytes((P.MSG_WIN, 0, 1))
        second_start = bytes((P.MSG_START, 0x10))
        second_win = bytes((P.MSG_WIN, 1, 1))
        self.backend_frames = [
            (P.JOIN_GAME, b""),
            (P.DUEL_START, b""),
            (P.GAME_MSG, first_start),
            (P.GAME_MSG, first_win),
            (P.EVRP_EXPORT, b"\x01\x00\x00\x01\x00x"),
            (P.DUEL_START, b""),
            (P.GAME_MSG, second_start),
            (P.GAME_MSG, second_win),
            (P.DUEL_END, b""),
        ]
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.gateway_port
        )
        try:
            writer.write(P.packet(P.PLAYER_INFO, P.encode_name("Replay-Watcher")))
            writer.write(P.packet(
                P.JOIN_GAME,
                struct.pack("<H2xI", P.MDPRO3_VERSION, 0)
                + P.encode_name(self.session["credentials"]["spectator"]),
            ))
            await writer.drain()
            received = []
            while True:
                opcode, payload, _raw = await asyncio.wait_for(
                    P.read_frame(reader), 2
                )
                received.append((opcode, payload))
                if opcode == P.DUEL_END:
                    break
        finally:
            await P.close_writer(writer)

        self.assertNotIn(P.EVRP_EXPORT, [opcode for opcode, _ in received])
        self.assertEqual(
            received,
            [
                (P.JOIN_GAME, b""),
                (P.DUEL_START, b""),
                (P.GAME_MSG, first_start),
                (P.GAME_MSG, first_win),
                (P.DUEL_START, b""),
                (P.GAME_MSG, second_start),
                (P.GAME_MSG, second_win),
                (P.DUEL_END, b""),
            ],
        )

    async def test_mdpro3_join_is_upgraded_with_legacy_game_conversion(self):
        confirm = bytes((31, 1, 0, 1)) + bytes(range(7))
        chain = bytes((16, 0, 1, 3)) + bytes(range(8)) + bytes((0x42, 1)) + bytes(range(12))
        self.backend_messages = [confirm, chain]
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway_port)
        try:
            writer.write(P.packet(0x17, bytes(range(30))))
            writer.write(P.packet(P.PLAYER_INFO, P.encode_name("MDPro3-A")))
            writer.write(P.packet(
                P.JOIN_GAME,
                struct.pack("<H2xI", P.MDPRO3_VERSION, 0)
                + P.encode_name(self.session["credentials"]["a"]),
            ))
            await writer.drain()
            messages = []
            while True:
                opcode, payload, _ = await asyncio.wait_for(P.read_frame(reader), 2)
                if opcode == P.GAME_MSG:
                    messages.append(payload)
                if opcode == P.DUEL_END:
                    break
            self.assertEqual(self.backend_join_version, P.ENGINE_VERSION)
            self.assertEqual(self.backend_prefix, [0x17, P.PLAYER_INFO])
            self.assertEqual(messages[:2], [
                bytes((31, 1, 1)) + bytes(range(7)),
                bytes((16, 0, 1, 3, 1)) + bytes(range(8))
                + bytes((0x42,)) + bytes(range(12)),
            ])
        finally:
            await P.close_writer(writer)


class NativeReconnectGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.backend_names = []
        self.first_backend_closed = asyncio.Event()
        self.backend_count = 0

        async def backend(reader, writer):
            name = ""
            try:
                while True:
                    opcode, payload, _raw = await P.read_frame(reader)
                    if opcode == P.PLAYER_INFO:
                        name = P.decode_name(payload)
                    if opcode != P.JOIN_GAME:
                        continue
                    self.backend_count += 1
                    self.backend_names.append(name)
                    if self.backend_count == 1:
                        writer.write(P.packet(P.DUEL_START))
                        await writer.drain()
                        try:
                            await P.read_frame(reader)
                        except (asyncio.IncompleteReadError, ConnectionError, OSError):
                            pass
                        self.first_backend_closed.set()
                        return
                    writer.write(P.packet(P.DUEL_START))
                    writer.write(P.packet(P.GAME_MSG, bytes((P.MSG_WIN, 0, 1))))
                    writer.write(P.packet(P.DUEL_END))
                    await writer.drain()
                    return
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                if self.backend_count == 1:
                    self.first_backend_closed.set()
            finally:
                await P.close_writer(writer)

        self.backend = await asyncio.start_server(backend, "127.0.0.1", 0)
        backend_port = self.backend.sockets[0].getsockname()[1]
        self.temp = tempfile.TemporaryDirectory()
        self.application = PvpApplication(
            Path(self.temp.name),
            "internal",
            "127.0.0.1",
            backend_port,
        )
        self.gateway = NativeGateway(self.application, "127.0.0.1", 0)
        await self.gateway.start()
        self.gateway_port = self.gateway.server.sockets[0].getsockname()[1]
        self.session = self.application.store.create(
            {
                "room_id": "r-reconnect-native",
                "match_id": "m-01",
                "game_number": 1,
                "seat_plan": {"a": 0, "b": 1},
                "players": {"a": {"name": "A"}, "b": {"name": "B"}},
            },
            "127.0.0.1",
            backend_port,
            "",
            "",
        )

    async def asyncTearDown(self):
        self.gateway.server.close()
        await self.gateway.server.wait_closed()
        await self.application.close()
        self.backend.close()
        await self.backend.wait_closed()
        self.temp.cleanup()

    async def connect(self, name):
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", self.gateway_port
        )
        writer.write(P.packet(P.PLAYER_INFO, P.encode_name(name)))
        writer.write(P.packet(
            P.JOIN_GAME,
            struct.pack("<H2xI", P.ENGINE_VERSION, 0)
            + P.encode_name(self.session["credentials"]["a"]),
        ))
        await writer.drain()
        return reader, writer

    async def test_disconnect_reconnects_same_seat_and_continues_result(self):
        first_reader, first_writer = await self.connect("A")
        try:
            opcode, _payload, _raw = await asyncio.wait_for(
                P.read_frame(first_reader), 2
            )
            self.assertEqual(opcode, P.DUEL_START)
        finally:
            await P.close_writer(first_writer)

        await asyncio.wait_for(self.first_backend_closed.wait(), 2)
        for _ in range(100):
            if self.session["connections"]["a"]["reconnect_until"]:
                break
            await asyncio.sleep(0.01)
        self.assertFalse(self.session["connections"]["a"]["connected"])
        self.assertIsNotNone(self.session["connections"]["a"]["reconnect_until"])

        second_reader, second_writer = await self.connect("Changed-name")
        try:
            received = []
            while True:
                opcode, _payload, _raw = await asyncio.wait_for(
                    P.read_frame(second_reader), 2
                )
                received.append(opcode)
                if opcode == P.DUEL_END:
                    break
        finally:
            await P.close_writer(second_writer)

        self.assertEqual(self.backend_names, ["A", "A"])
        self.assertIn(P.DUEL_START, received)
        self.assertIn(P.GAME_MSG, received)
        self.assertIn(P.DUEL_END, received)
        self.assertEqual(self.session["result"]["winner"], "a")

    async def test_leave_game_can_rejoin_continuous_series_without_deadline(self):
        self.session["continuous_series"] = True
        self.session["seed"] = list(range(8))
        self.application.store._save(self.session)

        first_reader, first_writer = await self.connect("A")
        try:
            opcode, _payload, _raw = await asyncio.wait_for(
                P.read_frame(first_reader), 2
            )
            self.assertEqual(opcode, P.DUEL_START)
            first_writer.write(P.packet(P.LEAVE_GAME))
            await first_writer.drain()
        finally:
            await P.close_writer(first_writer)

        await asyncio.wait_for(self.first_backend_closed.wait(), 2)
        for _ in range(100):
            if reconnect_scope(
                self.session, self.session["connections"]["a"]
            ) == "series":
                break
            await asyncio.sleep(0.01)
        connection = self.session["connections"]["a"]
        self.assertFalse(connection["connected"])
        self.assertIsNone(connection["reconnect_until"])
        self.assertEqual(reconnect_scope(self.session, connection), "series")

        second_reader, second_writer = await self.connect("Changed-name")
        try:
            received = []
            while True:
                opcode, _payload, _raw = await asyncio.wait_for(
                    P.read_frame(second_reader), 2
                )
                received.append(opcode)
                if opcode == P.DUEL_END:
                    break
        finally:
            await P.close_writer(second_writer)

        self.assertEqual(self.backend_names, ["A", "A"])
        self.assertIn(P.DUEL_START, received)
        self.assertIn(P.GAME_MSG, received)
        self.assertEqual(self.session["result"]["winner"], "a")


class LegacyProtocolTest(unittest.TestCase):
    def test_only_engine_compatible_versions_are_rewritten(self):
        cases = (
            (P.LEGACY_VERSION, P.ENGINE_VERSION),
            (P.MDPRO3_VERSION, P.ENGINE_VERSION),
            (P.ENGINE_VERSION, P.ENGINE_VERSION),
            (0x1234, 0x1234),
        )
        for version, expected in cases:
            original = struct.pack("<H2xI", version, 0x12345678) + P.encode_name("code")
            converted = P.engine_join(original, "backend")
            self.assertEqual(P.client_version(converted), expected)
            self.assertEqual(converted[2:8], original[2:8])
            self.assertEqual(P.join_password(converted), "backend")
        with self.assertRaises(P.ProtocolError):
            P.engine_join(b"", "backend")

    def test_legacy_chain_cannot_encode_mixed_flags(self):
        header = bytes((16, 0, 2, 3)) + bytes(8)
        items = (bytes((1, 0)) + bytes(12)
                 + bytes((2, 1)) + bytes(12))
        with self.assertRaisesRegex(P.ProtocolError, "cannot be represented"):
            P.legacy_game_message(header + items)

    def test_unrelated_game_messages_are_unchanged(self):
        self.assertEqual(P.legacy_game_message(b"\x05\x01\x00"), b"\x05\x01\x00")
        with self.assertRaisesRegex(P.ProtocolError, "invalid modern"):
            P.legacy_game_message(bytes((31, 0, 0, 1)))


class MultiRoomGatewayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend_joins = []
        self.release_duels = asyncio.Event()

        async def backend(reader, writer):
            try:
                while True:
                    opcode, payload, _ = await P.read_frame(reader)
                    if opcode == P.JOIN_GAME:
                        command = P.join_password(payload)
                        self.backend_joins.append(command)
                        await self.release_duels.wait()
                        winner = 0 if command == self.first["room_command"] else 1
                        writer.write(P.packet(P.DUEL_START))
                        writer.write(P.packet(P.GAME_MSG, bytes((P.MSG_WIN, winner, 1))))
                        writer.write(P.packet(P.DUEL_END))
                        await writer.drain()
                        return
            finally:
                await P.close_writer(writer)

        self.backend = await asyncio.start_server(backend, "127.0.0.1", 0)
        port = self.backend.sockets[0].getsockname()[1]
        self.application = PvpApplication(Path(self.temp.name), "internal", "127.0.0.1", port)
        self.gateway = NativeGateway(self.application, "127.0.0.1", 0)
        await self.gateway.start()
        self.gateway_port = self.gateway.server.sockets[0].getsockname()[1]
        body = {
            "room_id": "r-first", "match_id": "m-01", "game_number": 1,
            "seat_plan": {"a": 0, "b": 1},
            "players": {"a": {"name": "A"}, "b": {"name": "B"}},
        }
        self.first = self.application.store.create(body, "127.0.0.1", port, "", "")
        body["room_id"] = "r-second"
        self.second = self.application.store.create(body, "127.0.0.1", port, "", "")

    async def asyncTearDown(self):
        self.release_duels.set()
        self.gateway.server.close()
        await self.gateway.server.wait_closed()
        await self.application.close()
        self.backend.close()
        await self.backend.wait_closed()
        self.temp.cleanup()

    async def connect(self, session, role):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gateway_port)
        writer.write(P.packet(P.PLAYER_INFO, P.encode_name(role)))
        writer.write(P.packet(
            P.JOIN_GAME,
            struct.pack("<H2xI", 0x1362, 0) + P.encode_name(session["credentials"][role]),
        ))
        await writer.drain()
        return reader, writer

    async def test_two_rooms_relay_players_and_spectators_without_crossing_results(self):
        connections = []
        try:
            for role in ("a", "b"):
                for session in (self.first, self.second):
                    connections.append(await self.connect(session, role))
                    for _ in range(100):
                        if session["connections"][role]["connected"]:
                            break
                        await asyncio.sleep(0.01)
                    self.assertTrue(session["connections"][role]["connected"])
            for session in (self.first, self.second):
                connections.append(await self.connect(session, "spectator"))
            for _ in range(100):
                if len(self.backend_joins) == 6:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(self.backend_joins.count(self.first["room_command"]), 3)
            self.assertEqual(self.backend_joins.count(self.second["room_command"]), 3)
            self.assertEqual(self.first["status"], "ready")
            self.assertEqual(self.second["status"], "ready")
            self.assertEqual(self.first["spectators"], 1)
            self.assertEqual(self.second["spectators"], 1)
            self.release_duels.set()
            for reader, _ in connections:
                while True:
                    opcode, _, _ = await asyncio.wait_for(P.read_frame(reader), 2)
                    if opcode == P.DUEL_END:
                        break
            self.assertEqual(self.first["result"]["winner"], "a")
            self.assertEqual(self.second["result"]["winner"], "b")
            self.assertEqual(self.first["room_id"], "r-first")
            self.assertEqual(self.second["room_id"], "r-second")
        finally:
            for _, writer in connections:
                await P.close_writer(writer)


class FixedDeckPolicyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.application = PvpApplication(
            Path(self.temp.name),
            "internal",
            "127.0.0.1",
            7711,
            fixed_deck=FIXED_DECK,
        )

    async def asyncTearDown(self):
        await self.application.close()
        self.temp.cleanup()

    def create_session(self):
        return self.application.store.create(
            {
                "room_id": "r-fixed",
                "match_id": "m-fixed",
                "game_number": 1,
                "seat_plan": {"a": 0, "b": 1},
                "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                "seed": list(range(8)),
                "seed_mode": "mirrored",
                "required_first_player": "a",
            },
            "127.0.0.1",
            7711,
            "",
            "",
        )

    async def relay_deck(self, session, side, payload):
        class MemoryWriter:
            def __init__(self):
                self.data = bytearray()

            def write(self, value):
                self.data.extend(value)

            async def drain(self):
                return None

        reader = asyncio.StreamReader()
        reader.feed_data(P.packet(P.UPDATE_DECK, payload))
        reader.feed_eof()
        writer = MemoryWriter()
        with self.assertRaises(asyncio.IncompleteReadError):
            await self.application.gateway._client_to_backend(
                reader, writer, session, "player", side
            )
        forwarded = asyncio.StreamReader()
        forwarded.feed_data(bytes(writer.data))
        forwarded.feed_eof()
        opcode, forwarded_payload, _raw = await P.read_frame(forwarded)
        self.assertEqual(opcode, P.UPDATE_DECK)
        return forwarded_payload

    async def test_both_client_decks_are_replaced_before_result_is_accepted(self):
        self.application.gateway = NativeGateway(
            self.application, "127.0.0.1", 0
        )
        session = self.create_session()

        forwarded_a = await self.relay_deck(session, "a", b"client-deck-a")
        forwarded_b = await self.relay_deck(session, "b", b"client-deck-b")

        self.assertEqual(forwarded_a, self.application.fixed_deck.payload)
        self.assertEqual(forwarded_b, self.application.fixed_deck.payload)
        self.assertEqual(
            session["deck_replacements"]["a"]["submitted_payload_sha256"],
            hashlib.sha256(b"client-deck-a").hexdigest(),
        )
        self.assertEqual(
            session["deck_replacements"]["b"]["replacement_count"], 1
        )
        self.assertTrue(await self.application.finish_result(session, 0, 1))
        self.assertEqual(session["result"]["winner"], "a")

    async def test_result_is_rejected_if_only_one_player_deck_was_replaced(self):
        self.application.gateway = NativeGateway(
            self.application, "127.0.0.1", 0
        )
        session = self.create_session()
        await self.relay_deck(session, "a", b"only-a")

        self.assertFalse(await self.application.finish_result(session, 0, 1))
        self.assertIsNone(session["result"])
        self.assertEqual(
            session["error"], "fixed_deck_not_applied_to_both_players"
        )


class CallbackDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = 0
        self.bodies = []
        self.callback_statuses = [503, 200]

        async def callback(request):
            self.calls += 1
            self.bodies.append(await request.json())
            status = self.callback_statuses[
                min(self.calls - 1, len(self.callback_statuses) - 1)
            ]
            return web.Response(status=status)

        application = web.Application()
        application.router.add_post("/events", callback)
        self.runner = web.AppRunner(application)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]

        self.temp = tempfile.TemporaryDirectory()
        self.application = PvpApplication(
            Path(self.temp.name),
            "internal",
            "127.0.0.1",
            7711,
            fixed_deck=FIXED_DECK,
        )
        self.application.http = ClientSession()
        self.session = self.application.store.create(
            {
                "room_id": "r-1",
                "match_id": "m-01",
                "game_number": 1,
                "seat_plan": {"a": 0, "b": 1},
                "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                "seed": list(range(8)),
                "seed_mode": "mirrored",
                "required_first_player": "a",
            },
            "127.0.0.1",
            7711,
            f"http://127.0.0.1:{port}/events",
            "callback-key",
        )
        await self.application.record_deck_replacement(
            self.session, "a", b"submitted-a"
        )
        await self.application.record_deck_replacement(
            self.session, "b", b"submitted-b"
        )
        self.application.store.observe_start(
            self.session, "a", bytes((P.MSG_START, 0))
        )
        self.application.store.observe_start(
            self.session, "b", bytes((P.MSG_START, 1))
        )
        await self.application.finish_result(self.session, 0, 1)

    async def asyncTearDown(self):
        await self.application.close()
        await self.runner.cleanup()
        self.temp.cleanup()

    async def test_failed_callback_is_retryable_and_eventually_marked_delivered(self):
        await self.application.publish_result(self.session)
        self.assertEqual(self.calls, 1)
        self.assertIsNone(self.session["callback_delivered_at"])

        await self.application.publish_result(self.session)
        self.assertEqual(self.calls, 2)
        self.assertIsNotNone(self.session["callback_delivered_at"])
        self.assertEqual(
            self.bodies[-1]["fixed_deck"]["canonical_sha256"],
            self.application.fixed_deck.canonical_sha256,
        )
        self.assertEqual(
            self.bodies[-1]["deck_replacements"]["a"]["replacement_count"], 1
        )
        self.assertEqual(self.bodies[-1]["seed"], list(range(8)))
        self.assertEqual(
            self.bodies[-1]["required_first_player"], "a"
        )
        self.assertEqual(
            self.bodies[-1]["seat_plan"], {"a": 0, "b": 1}
        )

    async def test_closed_session_rejects_results_and_stops_callback_retries(self):
        self.application.store.close(self.session["id"])
        self.assertFalse(await self.application.finish_result(self.session, 0, 1))
        await self.application.publish_result(self.session)
        self.assertEqual(self.calls, 0)
        self.assertEqual(self.session["status"], "closed")

    async def test_missing_tournament_target_is_abandoned_after_one_404(self):
        self.callback_statuses = [404, 200]

        await self.application.publish_result(self.session)
        abandoned_at = self.session.get("callback_abandoned_at")

        self.assertEqual(self.calls, 1)
        self.assertIsNone(self.session["callback_delivered_at"])
        self.assertIsNotNone(abandoned_at)
        self.assertEqual(self.session["callback_abandoned_status"], 404)
        self.assertEqual(
            self.session["callback_abandoned_reason"],
            "tournament_target_not_found",
        )
        await self.application.publish_result(self.session)
        self.assertEqual(self.calls, 1)
        self.assertEqual(
            self.session.get("callback_abandoned_at"),
            abandoned_at,
        )

    async def test_seat_mismatch_is_abandoned_once_without_callback_retries(self):
        self.session["engine_seats"] = {"a": 1, "b": 0}
        self.application.store._save(self.session)

        await self.application.publish_result(self.session)
        first_abandoned_at = self.session.get("callback_abandoned_at")

        self.assertEqual(self.calls, 0)
        self.assertEqual(self.session["status"], "error")
        self.assertEqual(
            self.session["error"], "required_first_player_not_enforced"
        )
        self.assertIsNotNone(first_abandoned_at)
        await self.application.publish_result(self.session)
        self.assertEqual(self.calls, 0)
        self.assertEqual(
            self.session.get("callback_abandoned_at"), first_abandoned_at
        )


class ContinuousCallbackTransactionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.response_status = 200
        self.response = {
            "ok": True,
            "series_finished": False,
            "next_game": {
                "game_number": 2,
                "seat_plan": {"a": 1, "b": 0},
                "seed": list(range(8)),
                "seed_mode": "mirrored",
                "required_first_player": "b",
            },
        }

        async def callback(request):
            self.calls.append(await request.json())
            return web.json_response(
                self.response, status=self.response_status
            )

        web_app = web.Application()
        web_app.router.add_post("/events", callback)
        self.runner = web.AppRunner(web_app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]

        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.application = PvpApplication(
            self.root / "sessions",
            "internal",
            "127.0.0.1",
            7711,
            room_registry_dir=self.root / "registry",
        )
        self.application.http = ClientSession()
        self.session = self.application.store.create(
            {
                "room_id": "r-series-callback",
                "match_id": "m-series-callback",
                "game_number": 1,
                "seat_plan": {"a": 0, "b": 1},
                "players": {"a": {"name": "A"}, "b": {"name": "B"}},
                "seed": list(range(8)),
                "seed_mode": "mirrored",
                "required_first_player": "a",
                "continuous_series": True,
            },
            "127.0.0.1",
            7711,
            f"http://127.0.0.1:{port}/events",
            "callback-key",
        )
        self.application.room_registry.publish(self.session, None)
        self.application.store.observe_start(
            self.session, "a", bytes((P.MSG_START, 0))
        )
        self.application.store.observe_start(
            self.session, "b", bytes((P.MSG_START, 1))
        )
        generation, _ = self.application.store.bind_relay_start(
            self.session, "a"
        )
        await self.application.finish_result(self.session, 0, 1, generation)

    async def asyncTearDown(self):
        await self.application.close()
        await self.runner.cleanup()
        self.temp.cleanup()

    async def test_callback_waits_for_replay_and_registry_failure_is_retryable(self):
        await self.application.publish_result(self.session)
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.session["callback_delivered_at"])

        replay = b"yrp1" + bytes(64)
        self.application.store.record_replay(
            self.session, replay, "a", 1, 1
        )
        original_publish = self.application.room_registry.publish

        def fail_publish(_session, _deck):
            raise OSError("registry unavailable")

        self.application.room_registry.publish = fail_publish
        await self.application.publish_result(self.session)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.session["game_number"], 1)
        self.assertIsNotNone(self.session["result"])
        self.assertIsNone(self.session["callback_delivered_at"])

        self.application.room_registry.publish = original_publish
        registry_path = (
            self.application.room_registry.root
            / f"{self.session['room_command'].split(',', 1)[0]}.json"
        )
        registry_before = registry_path.read_bytes()
        original_save = self.application.store._save

        def fail_save(_session):
            raise OSError("session store unavailable")

        self.application.store._save = fail_save
        await self.application.publish_result(self.session)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.session["game_number"], 1)
        self.assertIsNotNone(self.session["result"])
        self.assertIsNone(self.session["callback_delivered_at"])
        self.assertEqual(registry_path.read_bytes(), registry_before)

        self.application.store._save = original_save
        await self.application.publish_result(self.session)
        self.assertEqual(len(self.calls), 3)
        self.assertIsNotNone(self.calls[-1]["replay"])
        self.assertFalse(self.calls[-1]["replay_missing_after_timeout"])
        self.assertEqual(self.session["game_number"], 2)
        self.assertIsNone(self.session["result"])
        self.assertIsNotNone(self.session["callback_delivered_at"])

    async def test_missing_replay_timeout_allows_audited_fallback(self):
        self.response = {"ok": True, "series_finished": True}
        self.session["result"]["replay_wait_until"] = 0
        self.application.store._save(self.session)

        await self.application.publish_result(self.session)

        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.calls[0]["replay"])
        self.assertTrue(self.calls[0]["replay_missing_after_timeout"])
        self.assertEqual(self.session["status"], "finished")
        self.assertTrue(self.session["series_finished"])
        self.assertIsNotNone(self.session["callback_delivered_at"])

    async def test_late_replay_supplement_retries_after_restart_without_advance(self):
        event_id = self.session["result"]["event_id"]
        self.session["result"]["replay_wait_until"] = 0
        self.application.store._save(self.session)

        await self.application.publish_result(self.session)

        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.session["game_number"], 2)
        self.assertIsNone(self.session["result"])
        self.assertEqual(len(self.session["results"]), 1)
        supplement = self.session["replay_supplements"][0]
        self.assertEqual(supplement["event_id"], event_id)
        self.assertIsNone(supplement["body"]["replay"])
        self.assertIsNone(supplement["delivered_at"])

        replay = b"yrp1" + bytes(64)
        self.application.store.record_replay(
            self.session, replay, "a", 1, 1
        )
        self.assertIsNotNone(supplement["body"]["replay"])
        self.assertNotIn("replay_supplements", public_session(self.session))

        self.response_status = 503
        await self.application.publish_replay_supplement(self.session)

        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[-1]["event_id"], event_id)
        self.assertIsNotNone(self.calls[-1]["replay"])
        self.assertFalse(
            self.calls[-1]["replay_missing_after_timeout"]
        )
        supplement = self.session["replay_supplements"][0]
        self.assertEqual(supplement["attempts"], 1)
        self.assertIsNone(supplement["delivered_at"])
        self.assertEqual(self.session["game_number"], 2)
        self.assertEqual(len(self.session["results"]), 1)

        await self.application.close()
        self.application = PvpApplication(
            self.root / "sessions",
            "internal",
            "127.0.0.1",
            7711,
            room_registry_dir=self.root / "registry",
        )
        self.application.http = ClientSession()
        self.session = self.application.store.sessions[self.session["id"]]
        self.response_status = 200

        await self.application.publish_replay_supplement(self.session)

        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[-1]["event_id"], event_id)
        self.assertEqual(
            self.calls[-1]["replay"]["sha256"],
            hashlib.sha256(replay).hexdigest(),
        )
        supplement = self.session["replay_supplements"][0]
        self.assertEqual(supplement["attempts"], 2)
        self.assertIsNotNone(supplement["delivered_at"])
        self.assertEqual(self.session["game_number"], 2)
        self.assertIsNone(self.session["result"])
        self.assertEqual(len(self.session["results"]), 1)


if __name__ == "__main__":
    unittest.main()
