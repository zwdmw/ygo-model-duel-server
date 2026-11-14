import asyncio
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import zipfile

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from tournament.server import TournamentError, TournamentStore


def make_playable_replay_archive(
    session_id: str,
    replays: dict[int, bytes],
    missing_ordinals: list[int] | None = None,
) -> bytes:
    output = io.BytesIO()
    manifest_entries = []
    missing = list(missing_ordinals or [])
    source_evrp_sha = hashlib.sha256(
        f"evrp:{session_id}".encode("ascii")
    ).hexdigest()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for duel_index, (ordinal, payload) in enumerate(
            sorted(replays.items()),
            1,
        ):
            filename = f"game-{ordinal:04d}.yrp3d"
            digest = hashlib.sha256(payload).hexdigest()
            archive.writestr(filename, payload)
            manifest_entries.append({
                "ordinal": ordinal,
                "filename": filename,
                "bytes": len(payload),
                "sha256": digest,
                "source_yrp_sha256": hashlib.sha256(
                    b"yrp2:" + payload
                ).hexdigest(),
                "source_yrp_ordinal": duel_index,
                "source_evrp_sha256": source_evrp_sha,
                "source_evrp_duel": duel_index,
            })
        archive.writestr(
            "manifest.json",
            json.dumps({
                "schema": "ygo-pvp-replay-manifest/v2",
                "session_id": session_id,
                "ordinal_scope": "absolute",
                "format": "mdpro3-yrp3d",
                "complete": not missing,
                "historical_prefix_missing": bool(missing),
                "missing_ordinals": missing,
                "replays": manifest_entries,
            }, separators=(",", ":")).encode("utf-8"),
        )
    return output.getvalue()


class TournamentStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TournamentStore(Path(self.temp.name))
        self.room = self.store.create(
            "four-player-test",
            ["Alice", "Bob", "Carol", "Dave"],
        )

    def tearDown(self):
        self.temp.cleanup()

    def native_match(self):
        room = self.store.create(
            "native-test",
            ["Alice", "Bob", "Carol", "Dave"],
            "native-pvp",
        )
        room_id = room["id"]
        self.store.start(room_id)
        match_id = self.store.public(room_id)["active_match_id"]
        match = self.store._find_match(self.store.rooms[room_id], match_id)
        match["session"].update({
            "pvp_session_id": "s-native-test",
            "native": {
                "host": "127.0.0.1",
                "port": 6009,
                "seat_plan": {"a": 0, "b": 1},
            },
            "native_player_codes": {"a": "code-a", "b": "code-b"},
            "spectator_code": "code-spectator",
        })
        self.store._save(self.store.rooms[room_id])
        return room_id, match_id

    def record(self, room_id, match_id, winners):
        for winner in winners:
            self.store.record_game(room_id, match_id, winner)

    def finish_active_round_robin(self, room_id, winners=None):
        room = self.store.public(room_id)
        match_id = room["active_match_id"]
        sequence = winners or (["a", "b"] * 10)
        self.record(room_id, match_id, sequence)
        return match_id

    def finish_round_robin(self, room_id, winners=None):
        while self.store.public(room_id)["stage"] == "round_robin":
            self.finish_active_round_robin(room_id, winners)

    def test_round_robin_schedule_supports_configurable_player_count(self):
        self.assertEqual(self.room["player_count"], 4)
        self.assertEqual(self.room["rules"]["player_count"], 4)
        self.assertEqual(len(self.room["matches"]), 6)
        self.assertEqual(len(self.room["player_access"]), 4)

        three = self.store.create(
            "three-player-test",
            ["A", "B", "C"],
            player_count=3,
        )
        self.assertEqual(three["player_count"], 3)
        self.assertEqual(three["rules"]["player_count"], 3)
        self.assertEqual(len(three["matches"]), 3)
        self.assertEqual(
            {
                frozenset((match["player_a"], match["player_b"]))
                for match in three["matches"]
            },
            {
                frozenset(("p-1", "p-2")),
                frozenset(("p-1", "p-3")),
                frozenset(("p-2", "p-3")),
            },
        )

        five = self.store.create(
            "five-player-test",
            ["A", "B", "C", "D", "E"],
            player_count=5,
        )
        self.assertEqual(five["player_count"], 5)
        self.assertEqual(five["rules"]["player_count"], 5)
        self.assertEqual(len(five["matches"]), 10)

        six = self.store.create(
            "six-player-test",
            ["A", "B", "C", "D", "E", "F"],
            player_count=6,
        )
        self.assertEqual(six["player_count"], 6)
        self.assertEqual(len(six["matches"]), 15)

    def test_league_start_opens_every_pairing_room_in_parallel(self):
        room = self.store.create(
            "three-player-parallel",
            ["A", "B", "C"],
            player_count=3,
            base_bo2_count=1,
        )
        started = self.store.start(room["id"])

        self.assertTrue(started["rules"]["parallel_matches"])
        self.assertEqual(len(started["active_matches"]), 3)
        self.assertEqual(
            {match["status"] for match in started["active_matches"]},
            {"running"},
        )
        self.assertEqual(
            len({match["duel_room_id"] for match in started["active_matches"]}),
            3,
        )

    def test_parallel_league_can_use_fixed_count_game_rooms(self):
        room = self.store.create(
            "parallel-games",
            ["A", "B", "C", "D"],
            series_format="games",
            game_count=3,
        )
        room_id = room["id"]
        started = self.store.start(room_id)
        match_id = started["matches"][0]["id"]

        for winner in ("a", "draw", "b"):
            self.store.record_game(room_id, match_id, winner)

        result = self.store.public(room_id)
        finished = next(
            match for match in result["matches"] if match["id"] == match_id
        )
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(finished["points"], {"a": 4, "b": 4})
        self.assertEqual(len(result["active_matches"]), 5)
        standings = {
            row["player_id"]: row for row in result["standings"]
        }
        self.assertEqual(standings["p-1"]["match_points"], 4)
        self.assertEqual(standings["p-2"]["match_points"], 4)
        self.assertEqual(standings["p-1"]["game_draws"], 1)

    def test_explicit_player_count_must_match_names(self):
        with self.assertRaisesRegex(TournamentError, "must match"):
            self.store.create(
                "wrong-count",
                ["Alice", "Bob", "Carol", "Dave"],
                player_count=5,
            )

    def test_explicit_player_count_rejects_unsupported_schedule_size(self):
        with self.assertRaisesRegex(TournamentError, "require 3-32"):
            self.store.create(
                "unsupported-count",
                ["Alice", "Bob"],
                player_count=2,
            )

    def test_league_can_create_a真人_final_challenge(self):
        room = self.store.create(
            "真人挑战赛",
            ["A", "B", "C", "D"],
            final_opponent_name="真人选手",
        )
        self.assertEqual(room["final_opponent"]["id"], "p-final")
        self.assertEqual(room["final_opponent"]["name"], "真人选手")
        self.assertIn("final_opponent_access", room)
        self.assertEqual(room["final_opponent_access"]["player_id"], "p-final")
        self.assertNotIn(
            "p-final",
            {row["player_id"] for row in room["standings"]},
        )

    def test_first_to_four_final_uses_real_player_and_alternates_seats(self):
        room = self.store.create(
            "真人决赛",
            ["A", "B", "C", "D"],
            final_opponent_name="真人选手",
        )
        room_id = room["id"]
        self.store.start(room_id)
        self.finish_round_robin(room_id, ["a", "a"] * 10)

        final = self.store.public(room_id)["active_match"]
        final_id = final["id"]
        self.assertEqual(final["match_format"], "first_to_four")
        self.assertEqual(final["player_b"], "p-final")
        self.assertEqual(final["target_wins"], 4)
        self.assertEqual(final["max_games"], 7)
        self.assertEqual(
            self.store.join_player(
                room_id,
                room["final_opponent_access"]["token"],
            )["player"]["id"],
            "p-final",
        )

        for winner in ["a", "b", "a", "b", "a", "b", "a"]:
            self.store.record_game(room_id, final_id, winner)

        result = self.store.public(room_id)
        finished = next(
            match for match in result["matches"] if match["id"] == final_id
        )
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(finished["game_wins"], {"a": 4, "b": 3})
        self.assertEqual(finished["winner"], "p-1")
        self.assertEqual(result["champion"], "p-1")
        self.assertEqual(
            [game["required_first_player"] for game in finished["games"]],
            ["a", "b", "a", "b", "a", "b", "a"],
        )
        self.assertEqual(
            len({tuple(game["seed"]) for game in finished["games"]}),
            7,
        )

    def test_first_to_four_ends_immediately_at_four_wins(self):
        room = self.store.create(
            "快速真人决赛",
            ["A", "B", "C", "D"],
            final_opponent_name="真人选手",
        )
        room_id = room["id"]
        self.store.start(room_id)
        self.finish_round_robin(room_id, ["a", "a"] * 10)
        final_id = self.store.public(room_id)["active_match_id"]

        self.record(room_id, final_id, ["b", "b", "a", "a", "b", "b"])
        result = self.store.public(room_id)
        final = next(
            match for match in result["matches"] if match["id"] == final_id
        )
        self.assertEqual(final["total_game_count"], 6)
        self.assertEqual(final["game_wins"], {"a": 2, "b": 4})
        self.assertEqual(final["winner"], "p-final")
        self.assertEqual(result["champion"], "p-final")

    def test_play_all_seven_final_does_not_stop_at_four_wins(self):
        room = self.store.create(
            "打满七局真人决赛",
            ["A", "B", "C", "D"],
            final_opponent_name="真人选手",
            final_completion_policy="play_all_seven",
        )
        room_id = room["id"]
        self.assertEqual(
            room["rules"]["final_completion_policy"], "play_all_seven"
        )
        self.store.start(room_id)
        self.finish_round_robin(room_id, ["a", "a"] * 10)
        final_id = self.store.public(room_id)["active_match_id"]

        self.record(room_id, final_id, ["a", "a", "a", "a"])
        running = self.store.public(room_id)["active_match"]
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["total_game_count"], 4)
        self.assertEqual(running["completion_policy"], "play_all_seven")

        self.record(room_id, final_id, ["b", "b", "b"])
        result = self.store.public(room_id)
        final = next(
            match for match in result["matches"] if match["id"] == final_id
        )
        self.assertEqual(final["status"], "finished")
        self.assertEqual(final["total_game_count"], 7)
        self.assertEqual(final["game_wins"], {"a": 4, "b": 3})
        self.assertEqual(final["winner"], "p-1")
        self.assertEqual(result["champion"], "p-1")

    def test_play_all_seven_tie_adds_single_game_overtime(self):
        room = self.store.create(
            "打满七局平分加赛",
            ["A", "B", "C", "D"],
            final_opponent_name="真人选手",
            final_completion_policy="play_all_seven",
        )
        room_id = room["id"]
        self.store.start(room_id)
        self.finish_round_robin(room_id, ["a", "a"] * 10)
        final_id = self.store.public(room_id)["active_match_id"]

        self.record(
            room_id,
            final_id,
            ["a", "b", "a", "b", "a", "b", "draw"],
        )
        tied = self.store.public(room_id)["active_match"]
        self.assertEqual(tied["status"], "running")
        self.assertEqual(tied["game_wins"], {"a": 3, "b": 3})
        self.assertEqual(tied["draw_replays"], 1)
        self.assertEqual(tied["game_limit"], 8)

        self.store.record_game(room_id, final_id, "b")
        result = self.store.public(room_id)
        final = next(
            match for match in result["matches"] if match["id"] == final_id
        )
        self.assertEqual(final["status"], "finished")
        self.assertEqual(final["total_game_count"], 8)
        self.assertEqual(final["winner"], "p-final")
        self.assertEqual(result["champion"], "p-final")

    def test_rejects_unknown_final_completion_policy(self):
        with self.assertRaisesRegex(
            TournamentError, "final_completion_policy"
        ):
            self.store.create(
                "错误真人决赛配置",
                ["A", "B", "C", "D"],
                final_opponent_name="真人选手",
                final_completion_policy="unknown",
            )

    def test_first_to_four_draw_adds_a_replay_until_someone_reaches_four(self):
        room = self.store.create(
            "平局加赛真人决赛",
            ["A", "B", "C", "D"],
            final_opponent_name="真人选手",
        )
        room_id = room["id"]
        self.store.start(room_id)
        self.finish_round_robin(room_id, ["a", "a"] * 10)
        final_id = self.store.public(room_id)["active_match_id"]

        self.record(
            room_id,
            final_id,
            ["a", "b", "a", "b", "a", "b", "draw", "a"],
        )
        result = self.store.public(room_id)
        final = next(
            match for match in result["matches"] if match["id"] == final_id
        )
        self.assertEqual(final["status"], "finished")
        self.assertEqual(final["total_game_count"], 8)
        self.assertEqual(final["game_wins"], {"a": 4, "b": 3})
        self.assertEqual(final["draw_replays"], 1)
        self.assertEqual(final["winner"], "p-1")
        self.assertEqual(
            [game["required_first_player"] for game in final["games"]],
            ["a", "b", "a", "b", "a", "b", "a", "b"],
        )

    def test_legacy_room_load_adds_player_count_rule(self):
        room_path = Path(self.temp.name) / f"{self.room['id']}.json"
        legacy = json.loads(room_path.read_text(encoding="utf-8"))
        legacy["rules"].pop("player_count")
        room_path.write_text(
            json.dumps(legacy, ensure_ascii=False),
            encoding="utf-8",
        )
        reloaded = TournamentStore(Path(self.temp.name))
        public_room = reloaded.public(self.room["id"])
        self.assertEqual(public_room["player_count"], 4)
        self.assertEqual(public_room["rules"]["player_count"], 4)

    def test_two_rooms_keep_independent_active_matches(self):
        second = self.store.create("second-room", ["A", "B", "C", "D"])
        self.store.start(self.room["id"])
        self.store.start(second["id"])
        first_match = self.store.public(self.room["id"])["active_match_id"]
        second_match = self.store.public(second["id"])["active_match_id"]

        self.store.record_game(self.room["id"], first_match, "a")
        first = self.store.public(self.room["id"])
        other = self.store.public(second["id"])
        self.assertEqual(first["active_match_id"], first_match)
        self.assertEqual(other["active_match_id"], second_match)
        self.assertEqual(first["active_match"]["bo2_count"], 0)
        self.assertEqual(other["active_match"]["bo2_count"], 0)

    def test_bo2_scores_three_one_and_swaps_first_player(self):
        room_id = self.room["id"]
        self.store.start(room_id)
        match_id = self.store.public(room_id)["active_match_id"]

        self.store.record_game(room_id, match_id, "a")
        first = self.store.public(room_id)["active_match"]
        self.assertEqual(first["games"][-1]["seat_map"], {"a": 0, "b": 1})
        self.assertEqual(first["bo2_game"], 2)
        self.assertEqual(first["next_seat_plan"], "b-first")

        self.store.record_game(room_id, match_id, "a")
        match = self.store.public(room_id)["matches"][0]
        self.assertEqual(match["bo2_count"], 1)
        self.assertEqual(match["bo2s"][0]["winner"], "a")
        self.assertEqual(match["bo2s"][0]["points"], {"a": 3, "b": 0})
        self.assertEqual(match["points"], {"a": 3, "b": 0})
        self.assertEqual(match["games"][1]["seat_map"], {"a": 1, "b": 0})
        self.assertEqual(match["games"][1]["seat_plan"], "b-first")

    def test_draw_bo2_awards_one_point_each(self):
        room_id = self.room["id"]
        self.store.start(room_id)
        match_id = self.store.public(room_id)["active_match_id"]
        self.record(room_id, match_id, ["a", "b"])

        match = self.store.public(room_id)["matches"][0]
        self.assertEqual(match["bo2s"][0]["winner"], "draw")
        self.assertEqual(match["bo2s"][0]["points"], {"a": 1, "b": 1})
        self.assertEqual(match["bo2_draws"], 1)
        self.assertEqual(match["points"], {"a": 1, "b": 1})

    def test_ten_bo2_complete_twenty_games_and_finish_match(self):
        room_id = self.room["id"]
        self.store.start(room_id)
        match_id = self.store.public(room_id)["active_match_id"]
        self.record(room_id, match_id, ["a", "b"] * 10)

        match = self.store.public(room_id)["matches"][0]
        self.assertEqual(match["status"], "finished")
        self.assertEqual(match["phase"], "finished")
        self.assertEqual(match["total_game_count"], 20)
        self.assertEqual(match["bo2_count"], 10)
        self.assertEqual(match["points"], {"a": 10, "b": 10})
        self.assertEqual(match["match_result"], "draw")
        self.assertIsNone(match["winner"])
        self.assertIsNone(match["loser"])
        self.assertEqual(match["games"][18]["seat_map"], {"a": 0, "b": 1})
        self.assertEqual(match["games"][19]["seat_map"], {"a": 1, "b": 0})
        standings = self.store.public(room_id)["standings"]
        self.assertEqual(standings[0]["match_points"], 10)
        self.assertEqual(standings[0]["bo2_draws"], 10)

    def test_remaining_games_forfeit_keeps_played_result_and_recalculates_bo2(self):
        room = self.store.create(
            "remaining-forfeit",
            ["A", "B", "C"],
            base_bo2_count=2,
        )
        room_id = room["id"]
        self.store.start(room_id)
        match_id = "m-01"
        self.store.record_game(room_id, match_id, "a")

        result = self.store.adjudicate_forfeit(
            room_id,
            match_id,
            "a",
            "remaining",
            "模型卡死且重连失败",
        )

        match = next(
            item for item in result["matches"] if item["id"] == match_id
        )
        self.assertEqual(match["status"], "finished")
        self.assertEqual(
            [game["winner"] for game in match["games"]],
            ["a", "b", "b", "b"],
        )
        self.assertEqual(match["points"], {"a": 1, "b": 4})
        self.assertEqual(match["bo2_wins"], {"a": 0, "b": 1})
        self.assertEqual(match["bo2_draws"], 1)
        self.assertEqual(match["forfeit_game_count"], 3)
        self.assertTrue(all(
            game.get("administrative_forfeit", {}).get("synthetic")
            for game in match["games"][1:]
        ))
        ruling = match["administrative_rulings"][-1]
        self.assertEqual(ruling["affected_game_numbers"], [2, 3, 4])
        self.assertEqual(ruling["synthetic_game_count"], 3)
        standings = {
            row["player_id"]: row for row in result["standings"]
        }
        self.assertEqual(standings[match["player_a"]]["match_points"], 1)
        self.assertEqual(standings[match["player_b"]]["match_points"], 4)

    def test_entire_match_forfeit_overrides_result_but_preserves_replay(self):
        room = self.store.create(
            "entire-forfeit",
            ["A", "B"],
            room_type="series",
            base_bo2_count=2,
            overtime_enabled=False,
        )
        room_id = room["id"]
        self.store.start(room_id)
        match_id = "m-01"
        self.store.record_game(room_id, match_id, "a")
        stored = self.store._find_match(self.store._room(room_id), match_id)
        stored["games"][0]["replay"] = {"bytes": 128, "sha256": "a" * 64}

        result = self.store.adjudicate_forfeit(
            room_id,
            match_id,
            "a",
            "all",
            "整场取消成绩",
        )

        match = result["matches"][0]
        self.assertEqual(result["status"], "finished")
        self.assertEqual([game["winner"] for game in match["games"]], ["b"] * 4)
        self.assertEqual(match["points"], {"a": 0, "b": 6})
        self.assertEqual(match["games"][0]["original_winner"], "a")
        self.assertEqual(match["games"][0]["replay"]["bytes"], 128)
        self.assertFalse(
            match["games"][0]["administrative_forfeit"]["synthetic"]
        )
        self.assertEqual(
            match["administrative_rulings"][-1]["overridden_game_numbers"],
            [1],
        )

    def test_force_finish_assigns_final_points_and_preserves_real_games(self):
        room = self.store.create(
            "force-score",
            ["A", "B", "C"],
            base_bo2_count=1,
        )
        room_id = room["id"]
        self.store.start(room_id)
        self.store.record_game(room_id, "m-01", "a")
        stored = self.store._find_match(self.store._room(room_id), "m-01")
        stored["games"][0]["replay"] = {
            "bytes": 128,
            "sha256": "a" * 64,
        }

        result = self.store.force_finish_round_robin(
            room_id, "m-01", 2, 1, "player withdrew"
        )

        match = next(item for item in result["matches"] if item["id"] == "m-01")
        self.assertEqual(match["status"], "finished")
        self.assertEqual(match["points"], {"a": 2, "b": 1})
        self.assertEqual(match["games"][0]["winner"], "a")
        self.assertEqual(match["games"][0]["replay"]["bytes"], 128)
        self.assertEqual(
            match["administrative_rulings"][-1]["type"],
            "forced_score",
        )
        standings = {
            row["player_id"]: row for row in result["standings"]
        }
        self.assertEqual(standings[match["player_a"]]["match_points"], 2)
        self.assertEqual(standings[match["player_b"]]["match_points"], 1)

    def test_force_finish_rejects_points_above_scheduled_maximum(self):
        room = self.store.create(
            "force-score-limit",
            ["A", "B", "C"],
            base_bo2_count=1,
        )
        room_id = room["id"]
        self.store.start(room_id)

        with self.assertRaisesRegex(TournamentError, "between 0 and 3"):
            self.store.force_finish_round_robin(
                room_id, "m-01", 4, 0, "invalid score"
            )

    def test_last_force_finished_pairing_creates_champion_final(self):
        room = self.store.create(
            "force-score-final",
            ["A", "B", "C"],
            base_bo2_count=1,
        )
        room_id = room["id"]
        self.store.start(room_id)

        for match_id, score in (
            ("m-01", (3, 0)),
            ("m-02", (3, 0)),
            ("m-03", (0, 3)),
        ):
            self.store.force_finish_round_robin(
                room_id, match_id, *score, "administrative completion"
            )

        result = self.store.public(room_id)
        self.assertEqual(result["stage"], "final")
        self.assertEqual(result["status"], "running")
        self.assertEqual(result["active_match_id"], "m-final")
        self.assertEqual(result["final_match_id"], "m-final")

    def test_adjudication_can_be_undone_before_later_results(self):
        room = self.store.create(
            "undo-forfeit",
            ["A", "B"],
            room_type="series",
            base_bo2_count=2,
            overtime_enabled=False,
        )
        room_id = room["id"]
        self.store.start(room_id)
        self.store.record_game(room_id, "m-01", "a")
        adjudicated = self.store.adjudicate_forfeit(
            room_id,
            "m-01",
            "a",
            "remaining",
            "temporary failure",
        )
        self.assertTrue(adjudicated["matches"][0]["can_undo_adjudication"])

        restored = self.store.undo_adjudication(room_id, "m-01")

        match = restored["matches"][0]
        self.assertEqual(restored["status"], "running")
        self.assertEqual(match["status"], "running")
        self.assertEqual(len(match["games"]), 1)
        self.assertEqual(match["games"][0]["winner"], "a")
        self.assertEqual(match["administrative_rulings"], [])
        self.assertFalse(match["can_undo_adjudication"])
        self.assertEqual(
            restored["administrative_ruling_log"][-1]["type"],
            "revert",
        )

    def test_adjudication_undo_rejects_later_parallel_results(self):
        room = self.store.create(
            "late-result-forfeit",
            ["A", "B", "C"],
            base_bo2_count=1,
        )
        room_id = room["id"]
        self.store.start(room_id)
        self.store.adjudicate_forfeit(
            room_id, "m-01", "a", "remaining", "stuck"
        )
        self.store.record_game(room_id, "m-02", "a")

        with self.assertRaisesRegex(TournamentError, "later results"):
            self.store.undo_adjudication(room_id, "m-01")

    def test_native_forfeit_excludes_synthetic_games_from_replay_expectation(self):
        room_id, match_id = self.native_match()
        self.store.record_game(
            room_id, match_id, "a", source="native"
        )
        stored = self.store._find_match(self.store._room(room_id), match_id)
        stored["games"][0]["replay"] = {"bytes": 64, "sha256": "b" * 64}

        result = self.store.adjudicate_forfeit(
            room_id,
            match_id,
            "a",
            "remaining",
            "native client stopped responding",
        )

        match = next(
            item for item in result["matches"] if item["id"] == match_id
        )
        self.assertEqual(match["total_game_count"], 20)
        self.assertEqual(match["replay_status"]["replay_count"], 1)
        self.assertEqual(match["replay_status"]["expected_replay_count"], 1)
        self.assertEqual(match["replay_status"]["missing_game_numbers"], [])
        game_count = len(match["games"])
        late = self.store.record_native_game(room_id, match_id, {
            "event_id": "late-after-adjudication",
            "session_id": "s-native-test",
        })
        late_match = next(
            item for item in late["matches"] if item["id"] == match_id
        )
        self.assertEqual(len(late_match["games"]), game_count)

    def test_five_player_round_robin_automatically_creates_final(self):
        room = self.store.create(
            "five-player-final",
            ["A", "B", "C", "D", "E"],
            player_count=5,
        )
        room_id = room["id"]
        self.store.start(room_id)
        self.finish_round_robin(room_id)

        public_room = self.store.public(room_id)
        self.assertEqual(public_room["stage"], "final")
        self.assertEqual(public_room["status"], "running")
        self.assertEqual(public_room["final_match_id"], "m-final")
        self.assertEqual(public_room["active_match_id"], "m-final")
        self.assertEqual(len(public_room["matches"]), 11)
        final = public_room["active_match"]
        self.assertEqual(final["stage"], "final")
        self.assertEqual(final["bo2_limit"], 10)
        self.assertEqual(
            {final["player_a"], final["player_b"]},
            {"p-1", "p-2"},
        )

    def test_final_tie_enters_two_game_swap_tiebreak_and_crowns_champion(self):
        room_id = self.room["id"]
        self.store.start(room_id)
        self.finish_round_robin(room_id, ["a", "a"] * 10)
        final_id = self.store.public(room_id)["active_match_id"]

        self.record(room_id, final_id, ["a", "b"] * 10)
        tied = self.store.public(room_id)["active_match"]
        self.assertEqual(tied["stage"], "final")
        self.assertEqual(tied["phase"], "tiebreak")
        self.assertEqual(tied["status"], "tiebreak")
        self.assertEqual(tied["bo2_count"], 10)
        self.assertEqual(tied["points"], {"a": 10, "b": 10})
        self.assertEqual(tied["next_seat_plan"], "swap-block")

        self.store.record_game(room_id, final_id, "a")
        self.store.record_game(room_id, final_id, "a")
        public_room = self.store.public(room_id)
        final = next(match for match in public_room["matches"] if match["id"] == final_id)
        self.assertEqual(final["status"], "finished")
        self.assertEqual(final["winner"], final["player_a"])
        self.assertEqual(public_room["status"], "finished")
        self.assertEqual(public_room["stage"], "finished")
        self.assertEqual(public_room["champion"], final["player_a"])
        self.assertEqual(final["games"][20]["seat_map"], {"a": 1, "b": 0})
        self.assertEqual(final["games"][21]["seat_map"], {"a": 0, "b": 1})

    def test_split_tiebreak_block_requires_another_two_game_block(self):
        room_id = self.store.create(
            "tiebreak-blocks",
            ["A", "B", "C", "D"],
        )["id"]
        self.store.start(room_id)
        self.finish_round_robin(room_id, ["a", "a"] * 10)
        match_id = self.store.public(room_id)["active_match_id"]

        self.record(room_id, match_id, ["a", "b"] * 10)
        tied = self.store.public(room_id)["active_match"]
        self.assertEqual(tied["phase"], "tiebreak")
        self.assertEqual(tied["playoff_block"], 1)
        self.assertEqual(tied["total_game_count"], 20)

        self.record(room_id, match_id, ["a", "b"])
        split = self.store.public(room_id)["active_match"]
        self.assertEqual(split["playoff_block"], 2)
        self.assertEqual(split["total_game_count"], 22)
        self.assertEqual(split["games"][20]["block"], 1)
        self.assertEqual(split["games"][21]["block"], 1)

        self.record(room_id, match_id, ["b", "b"])
        final = next(
            match for match in self.store.public(room_id)["matches"]
            if match["id"] == match_id
        )
        self.assertEqual(final["status"], "finished")
        self.assertEqual(final["winner"], final["player_b"])
        self.assertEqual(final["games"][22]["seat_map"], {"a": 1, "b": 0})
        self.assertEqual(final["games"][23]["seat_map"], {"a": 0, "b": 1})

    def test_tiebreak_recompute_counts_only_the_current_block(self):
        room = self.store.create(
            "tiebreak-recompute",
            ["A", "B"],
            room_type="series",
            base_bo2_count=1,
            overtime_enabled=True,
        )
        room_id = room["id"]
        self.store.start(room_id)
        match_id = self.store.public(room_id)["active_match_id"]
        self.record(room_id, match_id, ["a", "b", "a", "b", "b"])
        match = self.store._find_match(self.store._room(room_id), match_id)

        self.store._recompute_match(self.store._room(room_id), match)

        self.assertEqual(match["playoff_block"], 2)
        self.assertEqual(match["playoff_block_wins"], {"a": 0, "b": 1})
        self.store.record_game(room_id, match_id, "b")
        self.assertEqual(match["status"], "finished")
        self.assertEqual(match["winner"], match["player_b"])

    def test_undo_recomputes_incomplete_bo2(self):
        room_id = self.room["id"]
        self.store.start(room_id)
        match_id = self.store.public(room_id)["active_match_id"]
        self.store.record_game(room_id, match_id, "a")
        self.store.undo_last_game(room_id, match_id)
        match = self.store.public(room_id)["active_match"]
        self.assertEqual(match["total_game_count"], 0)
        self.assertEqual(match["bo2_count"], 0)
        self.assertEqual(match["points"], {"a": 0, "b": 0})

    def test_player_token_join_and_ready_are_bound_to_named_seat(self):
        room_id = self.room["id"]
        access = self.room["player_access"][0]
        self.store.start(room_id)

        joined = self.store.join_player(room_id, access["token"])
        self.assertEqual(joined["player"]["id"], access["player_id"])
        self.assertNotIn("join_token_hash", joined["room"]["players"][0])

        with self.assertRaises(Exception):
            self.store.set_player_ready(room_id, "p-2", access["token"], True)

        room = self.store.set_player_ready(
            room_id,
            access["player_id"],
            access["token"],
            True,
        )
        self.assertEqual(room["active_match"]["session"]["status"], "waiting")
        self.assertTrue(room["active_match"]["session"]["player_ready"]["a"])

    def test_rotating_player_access_invalidates_previous_token(self):
        room_id = self.room["id"]
        old_token = self.room["player_access"][0]["token"]
        rotated = self.store.rotate_player_access(room_id)
        new_token = rotated["player_access"][0]["token"]
        self.assertNotEqual(old_token, new_token)
        with self.assertRaises(Exception):
            self.store.join_player(room_id, old_token)
        self.assertEqual(
            self.store.join_player(room_id, new_token)["player"]["id"],
            "p-1",
        )

    def test_native_room_rejects_manual_score_and_hides_connection_codes(self):
        room_id, match_id = self.native_match()
        with self.assertRaisesRegex(TournamentError, "native matches"):
            self.store.record_game(room_id, match_id, "a")

        public_room = self.store.public(room_id)
        public_session = public_room["active_match"]["session"]
        self.assertNotIn("native_player_codes", public_session)
        self.assertNotIn("connection", public_session)

        player_room = self.store.public(room_id, "p-1")
        player_session = player_room["active_match"]["session"]
        self.assertEqual(player_session["connection"]["code"], "code-a")
        self.assertNotIn("code-b", json.dumps(player_session))

    def test_native_spectator_connection_is_protected_store_data(self):
        room_id, _match_id = self.native_match()
        spectator = self.store.native_spectator(room_id)
        self.assertEqual(spectator["room_id"], room_id)
        self.assertEqual(spectator["match_id"], "m-01")
        self.assertEqual(spectator["mode"], "live")
        self.assertEqual(spectator["connection"]["host"], "127.0.0.1")
        self.assertEqual(spectator["connection"]["port"], 6009)
        self.assertEqual(spectator["connection"]["code"], "code-spectator")

    def test_finished_native_spectator_falls_back_to_replay_connection(self):
        room_id, match_id = self.native_match()
        room = self.store._room(room_id)
        match = self.store._find_match(room, match_id)
        match["games"] = [
            {"number": 1, "winner": "a", "replay": {"bytes": 10}},
            {"number": 2, "winner": "b", "replay": {"bytes": 10}},
        ]
        match["status"] = "finished"
        match["session"]["status"] = "finished"
        match["session"]["replay_status"] = {
            "session_id": match["session"]["pvp_session_id"],
            "state": "complete",
            "available": True,
            "complete": True,
            "partial": False,
            "replay_count": 2,
            "expected_replay_count": 2,
            "missing_game_numbers": [],
        }
        room["active_match_id"] = None
        room["status"] = "finished"
        self.store._save(room)

        public = self.store.public(room_id)
        self.assertEqual(
            public["matches"][0]["observer_connection"]["code"],
            "code-spectator",
        )
        spectator = self.store.native_spectator(room_id)

        self.assertEqual(spectator["match_id"], match_id)
        self.assertEqual(spectator["mode"], "replay")
        self.assertEqual(spectator["status"], "finished")
        self.assertEqual(spectator["game_number"], 2)
        self.assertEqual(
            spectator["connection"]["code"], "code-spectator"
        )

        closed = self.store.close(room_id)
        self.assertNotIn("observer_connection", closed["matches"][0])
        with self.assertRaisesRegex(TournamentError, "closed"):
            self.store.native_spectator(room_id, match_id)
        with self.assertRaisesRegex(TournamentError, "closed"):
            self.store.native_access(room_id, match_id)

    def test_finished_incomplete_native_replay_hides_rejected_connection(self):
        room_id, match_id = self.native_match()
        room = self.store._room(room_id)
        match = self.store._find_match(room, match_id)
        match["games"] = [
            {"number": 1, "winner": "a"},
            {"number": 2, "winner": "b", "replay": {"bytes": 10}},
        ]
        match["status"] = "finished"
        match["session"]["status"] = "finished"
        match["session"]["replay_status"] = {
            "session_id": match["session"]["pvp_session_id"],
            "state": "incomplete",
            "available": False,
            "complete": False,
            "partial": False,
            "replay_count": 1,
            "expected_replay_count": 2,
            "missing_game_numbers": [1],
        }
        room["active_match_id"] = None
        room["status"] = "finished"
        self.store._save(room)

        public = self.store.public(room_id)["matches"][0]

        self.assertNotIn("observer_connection", public)
        self.assertEqual(public["replay_status"]["replay_count"], 1)
        self.assertEqual(public["replay_status"]["missing_game_numbers"], [1])
        with self.assertRaisesRegex(
            TournamentError,
            r"incomplete \(1/2; missing games 1\)",
        ):
            self.store.native_spectator(room_id, match_id)

    def test_native_result_is_recorded_once_by_event_id(self):
        room_id, match_id = self.native_match()
        deck_hash = "d" * 64
        stored_match = self.store._find_match(
            self.store.rooms[room_id], match_id
        )
        stored_match["session"]["fixed_deck"] = {
            "name": "SkyStriker",
            "canonical_sha256": deck_hash,
            "counts": {"main": 40, "extra": 15, "side": 0},
        }
        plan = self.store._ensure_game_plan(
            self.store.rooms[room_id], stored_match
        )
        self.store._save(self.store.rooms[room_id])
        event = {
            "event_id": "event-native-1",
            "session_id": "s-native-test",
            "game_number": 1,
            "winner": "a",
            "engine_winner": 0,
            "engine_seats": {"a": 0, "b": 1},
            "seed": plan["seed"],
            "seat_plan": plan["seat_map"],
            "required_first_player": plan["required_first_player"],
            "reason": 1,
            "replay": {"bytes": 128, "sha256": "a" * 64},
            "fixed_deck": {
                "name": "SkyStriker",
                "canonical_sha256": deck_hash,
                "counts": {"main": 40, "extra": 15, "side": 0},
            },
            "deck_replacements": {
                side: {
                    "replacement_count": 1,
                    "target_deck_sha256": deck_hash,
                }
                for side in ("a", "b")
            },
        }
        self.store.record_native_game(room_id, match_id, event)
        self.store.record_native_game(room_id, match_id, event)

        match = self.store.public(room_id)["active_match"]
        self.assertEqual(len(match["games"]), 1)
        self.assertEqual(match["games"][0]["event_id"], "event-native-1")
        self.assertEqual(match["games"][0]["engine_winner"], 0)
        self.assertEqual(match["games"][0]["engine_seats"], {"a": 0, "b": 1})
        self.assertEqual(match["games"][0]["replay"]["bytes"], 128)
        self.assertEqual(
            match["games"][0]["fixed_deck"]["canonical_sha256"], deck_hash
        )
        self.assertEqual(
            match["games"][0]["deck_replacements"]["b"]["replacement_count"], 1
        )
        self.assertEqual(match["games"][0]["session_id"], "s-native-test")
        stored = self.store._find_match(self.store._room(room_id), match_id)
        self.assertEqual(
            stored["pvp_sessions"],
            [{"id": "s-native-test", "start_game": 1}],
        )

        conflicting = copy.deepcopy(event)
        conflicting.update({
            "game_number": 2,
            "winner": "b",
            "engine_winner": 1,
        })
        with self.assertRaisesRegex(TournamentError, "conflicts"):
            self.store.record_native_game(room_id, match_id, conflicting)

    def test_duplicate_native_event_can_enrich_late_replay_metadata(self):
        room_id, match_id = self.native_match()
        match = self.store._find_match(self.store._room(room_id), match_id)
        plan = self.store._ensure_game_plan(self.store._room(room_id), match)
        event = {
            "event_id": "event-late-replay",
            "session_id": "s-native-test",
            "game_number": 1,
            "winner": "a",
            "engine_winner": plan["seat_map"]["a"],
            "engine_seats": plan["seat_map"],
            "seed": plan["seed"],
            "seat_plan": plan["seat_map"],
            "required_first_player": plan["required_first_player"],
            "reason": 1,
            "replay_missing_after_timeout": True,
        }

        self.store.record_native_game(room_id, match_id, event)
        enriched = copy.deepcopy(event)
        enriched["replay"] = {"bytes": 256, "sha256": "f" * 64}
        enriched["replay_missing_after_timeout"] = False
        self.store.record_native_game(room_id, match_id, enriched)

        game = self.store._find_match(
            self.store._room(room_id), match_id
        )["games"][0]
        self.assertTrue(game["replay_missing_after_timeout"])
        self.assertTrue(game["replay_recovered_after_timeout"])
        self.assertEqual(game["replay"]["bytes"], 256)
        self.assertEqual(len(
            self.store._find_match(
                self.store._room(room_id), match_id
            )["games"]
        ), 1)

    def test_native_bo2_final_keeps_room_and_plans_game_21_after_full_tie(self):
        room = self.store.create(
            "native-final-overtime",
            ["A", "B", "C"],
            engine="native-pvp",
            base_bo2_count=1,
        )
        room_id = room["id"]
        self.store.start(room_id)
        stored_room = self.store._room(room_id)

        for match in list(stored_room["matches"]):
            match["session"]["pvp_session_id"] = f"s-{match['id']}"
            for number in (1, 2):
                plan = self.store._ensure_game_plan(stored_room, match)
                self.store.record_native_game(room_id, match["id"], {
                    "event_id": f"{match['id']}-{number}",
                    "session_id": f"s-{match['id']}",
                    "game_number": number,
                    "winner": "a",
                    "engine_winner": plan["seat_map"]["a"],
                    "seed": plan["seed"],
                    "seat_plan": plan["seat_map"],
                    "engine_seats": plan["seat_map"],
                    "required_first_player": plan["required_first_player"],
                })

        final = self.store._find_match(stored_room, "m-final")
        self.assertTrue(self.store._auto_next(stored_room, final))
        self.assertEqual(final["session"]["player_ready"], {"a": True, "b": True})
        final["session"]["pvp_session_id"] = "s-final-continuous"

        for number in range(1, 21):
            plan = self.store._ensure_game_plan(stored_room, final)
            winner = "a" if number % 2 else "b"
            self.store.record_native_game(room_id, final["id"], {
                "event_id": f"final-tie-{number}",
                "session_id": "s-final-continuous",
                "game_number": number,
                "winner": winner,
                "engine_winner": plan["seat_map"][winner],
                "seed": plan["seed"],
                "seat_plan": plan["seat_map"],
                "engine_seats": plan["seat_map"],
                "required_first_player": plan["required_first_player"],
            })
            self.assertEqual(
                final["session"]["pvp_session_id"],
                "s-final-continuous",
            )

        self.assertEqual(final["status"], "tiebreak")
        self.assertEqual(final["phase"], "tiebreak")
        self.assertEqual(final["points"], {"a": 10, "b": 10})
        self.assertEqual(final["playoff_block"], 1)
        next_plan = self.store._ensure_game_plan(stored_room, final)
        self.assertEqual(next_plan["number"], 21)
        self.assertEqual(next_plan["phase"], "tiebreak")
        self.assertEqual(next_plan["block"], 1)
        self.assertEqual(next_plan["seat_map"], {"a": 1, "b": 0})
        self.assertEqual(next_plan["required_first_player"], "b")

        self.store._recompute_match(stored_room, final)
        self.assertEqual(
            final["session"]["pvp_session_id"],
            "s-final-continuous",
        )
        self.store._save(stored_room)
        reloaded = TournamentStore(Path(self.temp.name))
        loaded_final = reloaded._find_match(
            reloaded._room(room_id),
            "m-final",
        )
        self.assertEqual(
            loaded_final["session"]["pvp_session_id"],
            "s-final-continuous",
        )

        loaded_final["session"].update({
            "status": "waiting",
            "player_ready": {"a": False, "b": False},
            "opened_at": None,
        })
        loaded_final["session"].pop("pvp_session_id")
        reloaded._save(reloaded._room(room_id))
        repaired = TournamentStore(Path(self.temp.name))
        repaired_final = repaired._find_match(
            repaired._room(room_id),
            "m-final",
        )
        self.assertEqual(repaired_final["session"]["status"], "ready")
        self.assertEqual(
            repaired_final["session"]["player_ready"],
            {"a": True, "b": True},
        )
        self.assertIsNotNone(repaired_final["session"]["opened_at"])

    def test_native_result_rejects_winner_engine_mismatch(self):
        room_id, match_id = self.native_match()
        stored_match = self.store._find_match(
            self.store.rooms[room_id], match_id
        )
        plan = self.store._ensure_game_plan(
            self.store.rooms[room_id], stored_match
        )
        self.store._save(self.store.rooms[room_id])

        with self.assertRaisesRegex(
            TournamentError, "does not match engine winner"
        ):
            self.store.record_native_game(room_id, match_id, {
                "event_id": "event-native-mismatch",
                "session_id": "s-native-test",
                "game_number": 1,
                "winner": "a",
                "engine_winner": 1,
                "engine_seats": {"a": 0, "b": 1},
                "seed": plan["seed"],
                "seat_plan": plan["seat_map"],
                "required_first_player": plan["required_first_player"],
            })

        match = self.store.public(room_id)["active_match"]
        self.assertEqual(match["games"], [])
        self.assertEqual(match["points"], {"a": 0, "b": 0})

    def test_native_result_saves_score_and_event_metadata_once(self):
        room_id, match_id = self.native_match()
        stored_match = self.store._find_match(
            self.store.rooms[room_id], match_id
        )
        plan = self.store._ensure_game_plan(
            self.store.rooms[room_id], stored_match
        )
        self.store._save(self.store.rooms[room_id])
        event = {
            "event_id": "event-native-atomic",
            "session_id": "s-native-test",
            "game_number": 1,
            "winner": "a",
            "engine_winner": plan["seat_map"]["a"],
            "engine_seats": plan["seat_map"],
            "seed": plan["seed"],
            "seat_plan": plan["seat_map"],
            "required_first_player": plan["required_first_player"],
        }
        original_save = self.store._save
        saved = []

        def counted_save(room):
            saved.append(room["id"])
            original_save(room)

        self.store._save = counted_save
        try:
            self.store.record_native_game(room_id, match_id, event)
        finally:
            self.store._save = original_save

        self.assertEqual(saved, [room_id])
        reloaded = TournamentStore(Path(self.temp.name))
        game = reloaded.public(room_id)["active_match"]["games"][0]
        self.assertEqual(game["winner"], "a")
        self.assertEqual(game["event_id"], "event-native-atomic")
        self.assertEqual(game["engine_winner"], plan["seat_map"]["a"])

    def test_parallel_native_rooms_have_isolated_player_and_observer_codes(self):
        room = self.store.create(
            "native-parallel",
            ["A", "B", "C", "D"],
            "native-pvp",
            base_bo2_count=1,
        )
        room_id = room["id"]
        self.store.start(room_id)
        private = self.store.native_access(room_id)

        self.assertTrue(private["parallel_matches"])
        self.assertEqual(len(private["matches"]), 6)
        all_codes = {
            code
            for entry in private["matches"]
            for code in (
                entry["player_passwords"]["a"],
                entry["player_passwords"]["b"],
                entry["spectator_password"],
            )
        }
        self.assertEqual(len(all_codes), 18)

        stored = self.store._room(room_id)
        for match in stored["matches"]:
            credentials = match["native_credentials"]
            match["session"].update({
                "pvp_session_id": f"s-{match['id']}",
                "native": {
                    "host": "127.0.0.1",
                    "port": 6009,
                    "seat_plan": {"a": 0, "b": 1},
                },
                "native_player_codes": {
                    "a": credentials["a"],
                    "b": credentials["b"],
                },
                "spectator_code": credentials["spectator"],
            })

        public = self.store.public(room_id)
        self.assertTrue(all(
            match.get("observer_connection", {}).get("code")
            for match in public["active_matches"]
        ))
        self.assertNotIn(
            next(iter(all_codes)),
            json.dumps(public["standings"]),
        )

        player = self.store.public(room_id, "p-1")
        for match in player["active_matches"]:
            connection = match["session"].get("connection")
            if "p-1" in (match["player_a"], match["player_b"]):
                self.assertIsNotNone(connection)
            else:
                self.assertIsNone(connection)

    def test_native_pause_and_undo_are_rejected(self):
        room_id, match_id = self.native_match()
        with self.assertRaisesRegex(TournamentError, "native duel must finish"):
            self.store.pause(room_id)
        self.store.record_game(room_id, match_id, "a", source="native")
        with self.assertRaisesRegex(TournamentError, "native results cannot be undone"):
            self.store.undo_last_game(room_id, match_id)

    def test_public_health_shape_is_available_from_application(self):
        from tournament.server import TournamentApplication

        application = TournamentApplication(Path(self.temp.name), "test-key")
        self.assertEqual(
            application.store.rooms,
            {self.room["id"]: self.store.rooms[self.room["id"]]},
        )
        self.assertIn(
            "/health",
            {route.resource.canonical for route in application.app().router.routes()},
        )


class SeriesRoomTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = TournamentStore(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **options):
        return self.store.create(
            "series-test",
            ["Alice", "Bob"],
            options.pop("engine", "manual-admin"),
            room_type="series",
            **options,
        )

    def record(self, room_id, winners):
        match_id = self.store.public(room_id)["active_match_id"]
        for winner in winners:
            self.store.record_game(room_id, match_id, winner)

    def test_series_room_has_one_match_and_configurable_rules(self):
        room = self.create(
            base_bo2_count=7,
            seed_mode="mirrored",
            overtime_enabled=False,
            auto_next_game=False,
        )

        self.assertEqual(room["room_type"], "series")
        self.assertEqual(room["player_count"], 2)
        self.assertEqual(len(room["matches"]), 1)
        self.assertEqual(room["rules"]["base_bo2_count"], 7)
        self.assertEqual(room["active_match"], None)
        self.assertEqual(room["matches"][0]["bo2_limit"], 7)

    def test_mirrored_mode_reuses_seed_within_each_swapped_pair(self):
        room = self.create(base_bo2_count=2, seed_mode="mirrored")
        room_id = room["id"]
        self.store.start(room_id)
        self.record(room_id, ["a", "b", "a"])

        games = self.store.public(room_id)["active_match"]["games"]
        self.assertEqual(games[0]["seed"], games[1]["seed"])
        self.assertNotEqual(games[1]["seed"], games[2]["seed"])
        self.assertEqual(games[0]["seat_map"], {"a": 0, "b": 1})
        self.assertEqual(games[1]["seat_map"], {"a": 1, "b": 0})
        self.assertEqual(games[0]["required_first_player"], "a")
        self.assertEqual(games[1]["required_first_player"], "b")

    def test_independent_mode_uses_a_new_seed_for_each_game(self):
        room = self.create(base_bo2_count=1, seed_mode="independent")
        room_id = room["id"]
        self.store.start(room_id)
        self.record(room_id, ["a", "b"])

        games = self.store.public(room_id)["matches"][0]["games"]
        self.assertNotEqual(games[0]["seed"], games[1]["seed"])
        self.assertEqual(games[0]["seat_map"], {"a": 0, "b": 1})
        self.assertEqual(games[1]["seat_map"], {"a": 1, "b": 0})

    def test_fixed_bo2_schedule_does_not_end_after_clinching_a_majority(self):
        room = self.create(
            base_bo2_count=5,
            overtime_enabled=False,
        )
        room_id = room["id"]
        self.store.start(room_id)

        self.record(room_id, ["a", "a"] * 3)
        leading = self.store.public(room_id)
        match = leading["active_match"]
        self.assertEqual(leading["status"], "running")
        self.assertEqual(match["status"], "running")
        self.assertEqual(match["phase"], "regular")
        self.assertEqual(match["bo2_count"], 3)
        self.assertEqual(match["total_game_count"], 6)
        self.assertEqual(match["points"], {"a": 9, "b": 0})
        self.assertEqual(match["next_bo2"], 4)

        self.record(room_id, ["a", "a"] * 2)
        finished = self.store.public(room_id)
        match = finished["matches"][0]
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(match["status"], "finished")
        self.assertEqual(match["bo2_count"], 5)
        self.assertEqual(match["total_game_count"], 10)
        self.assertEqual(match["points"], {"a": 15, "b": 0})

    def test_tied_series_can_finish_without_overtime(self):
        room = self.create(
            base_bo2_count=1,
            overtime_enabled=False,
        )
        room_id = room["id"]
        self.store.start(room_id)
        self.record(room_id, ["a", "b"])

        finished = self.store.public(room_id)
        match = finished["matches"][0]
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(match["match_result"], "draw")
        self.assertIsNone(match["winner"])

    def test_overtime_repeats_swap_blocks_until_one_side_wins_both(self):
        room = self.create(base_bo2_count=1, overtime_enabled=True)
        room_id = room["id"]
        self.store.start(room_id)
        self.record(room_id, ["a", "b"])

        tied = self.store.public(room_id)["active_match"]
        self.assertEqual(tied["phase"], "tiebreak")
        self.assertEqual(tied["playoff_block"], 1)

        self.record(room_id, ["a", "b"])
        repeated = self.store.public(room_id)["active_match"]
        self.assertEqual(repeated["playoff_block"], 2)

        self.record(room_id, ["b", "b"])
        finished = self.store.public(room_id)
        match = finished["matches"][0]
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(match["winner"], "p-2")
        self.assertEqual(match["games"][2]["seat_map"], {"a": 0, "b": 1})
        self.assertEqual(match["games"][3]["seat_map"], {"a": 1, "b": 0})
        self.assertEqual(match["games"][4]["seed"], match["games"][5]["seed"])

    def test_native_series_keeps_stable_credentials_and_auto_readiness(self):
        room = self.create(engine="native-pvp", base_bo2_count=1)
        room_id = room["id"]
        private = self.store.native_access(room_id)
        self.store.start(room_id)
        started = self.store.public(room_id)

        self.assertEqual(
            private["player_passwords"].keys(), {"a", "b"}
        )
        self.assertEqual(
            len(set(private["player_passwords"].values())), 2
        )
        self.assertTrue(
            all(started["active_match"]["session"]["player_ready"].values())
        )
        self.assertEqual(
            started["active_match"]["session"]["status"], "ready"
        )
        self.assertNotIn(
            private["player_passwords"]["a"], json.dumps(started)
        )

    def test_native_series_records_two_games_on_same_session_with_swapped_seats(self):
        room = self.create(engine="native-pvp", base_bo2_count=1)
        room_id = room["id"]
        self.store.start(room_id)
        match_id = self.store.public(room_id)["active_match_id"]
        match = self.store._find_match(self.store._room(room_id), match_id)
        match["session"]["pvp_session_id"] = "s-continuous"
        self.store._save(self.store._room(room_id))

        for number, winner in ((1, "a"), (2, "b")):
            plan = self.store._ensure_game_plan(self.store._room(room_id), match)
            event = {
                "event_id": f"event-{number}",
                "session_id": "s-continuous",
                "game_number": number,
                "winner": winner,
                "engine_winner": plan["seat_map"][winner],
                "seed": plan["seed"],
                "seat_plan": plan["seat_map"],
                "engine_seats": plan["seat_map"],
                "required_first_player": plan["required_first_player"],
                "reason": 1,
            }
            self.store.record_native_game(room_id, match_id, event)
            self.assertEqual(match["session"]["pvp_session_id"], "s-continuous")

        self.assertEqual(match["games"][0]["seed"], match["games"][1]["seed"])
        self.assertNotEqual(match["games"][0]["seat_map"], match["games"][1]["seat_map"])
        self.assertEqual(match["points"], {"a": 1, "b": 1})
        self.assertEqual(match["phase"], "tiebreak")

    def test_single_games_use_exact_count_fresh_seeds_and_alternating_seats(self):
        room = self.create(
            series_format="games", game_count=5, seed_mode="mirrored",
            overtime_enabled=True,
        )
        room_id = room["id"]
        self.assertEqual(room["rules"]["series_format"], "games")
        self.assertEqual(room["rules"]["seed_mode"], "independent")
        self.assertFalse(room["rules"]["overtime_enabled"])
        self.store.start(room_id)
        self.record(room_id, ["a", "b", "draw", "a"])
        active = self.store.public(room_id)["active_match"]
        self.assertEqual(active["next_game"], 5)
        self.assertEqual(active["game_limit"], 5)
        self.assertEqual(active["points"], {"a": 7, "b": 4})
        self.assertEqual(active["bo2_count"], 0)
        self.record(room_id, ["b"])
        finished = self.store.public(room_id)
        match = finished["matches"][0]
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(match["total_game_count"], 5)
        self.assertEqual(match["points"], {"a": 7, "b": 7})
        self.assertEqual(match["match_result"], "draw")
        self.assertEqual([game["required_first_player"] for game in match["games"]],
                         ["a", "b", "a", "b", "a"])
        self.assertEqual(len({tuple(game["seed"]) for game in match["games"]}), 5)
        self.assertTrue(all(game["bo2"] is None for game in match["games"]))
        self.assertEqual(
            [row["match_points"] for row in finished["standings"]], [7, 7]
        )
        with self.assertRaisesRegex(TournamentError, "not running"):
            self.store.record_game(room_id, match["id"], "a")

    def test_single_game_and_undo_recalculate_points(self):
        room = self.create(series_format="games", game_count=1)
        room_id = room["id"]
        self.store.start(room_id)
        match_id = self.store.public(room_id)["active_match_id"]
        self.record(room_id, ["a"])
        self.assertEqual(self.store.public(room_id)["status"], "finished")

        other = self.create(series_format="games", game_count=3)
        other_id = other["id"]
        self.store.start(other_id)
        other_match = self.store.public(other_id)["active_match_id"]
        self.record(other_id, ["a", "draw"])
        self.store.undo_last_game(other_id, other_match)
        active = self.store.public(other_id)["active_match"]
        self.assertEqual(active["points"], {"a": 3, "b": 0})
        self.assertEqual(active["next_game"], 2)
        self.assertEqual(active["next_seat_plan"], "b-first")

    def test_single_games_validate_count_and_reload(self):
        for count in (0, 201, "bad", True):
            with self.subTest(count=count), self.assertRaises(TournamentError):
                self.create(series_format="games", game_count=count)
        with self.assertRaisesRegex(TournamentError, "series_format"):
            self.create(series_format="invalid")
        room = self.create(series_format="games", game_count=3)
        reloaded = TournamentStore(Path(self.temp.name))
        self.assertEqual(reloaded.public(room["id"])["rules"]["game_count"], 3)
        legacy = self.create(base_bo2_count=2)
        path = Path(self.temp.name) / f"{legacy['id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["rules"].pop("series_format")
        data["rules"].pop("game_count")
        path.write_text(json.dumps(data), encoding="utf-8")
        loaded = TournamentStore(Path(self.temp.name)).public(legacy["id"])
        self.assertEqual(loaded["rules"]["series_format"], "bo2")
        self.assertEqual(loaded["rules"]["game_count"], 4)

    def test_native_single_games_advance_on_same_session_until_exact_limit(self):
        room = self.create(
            engine="native-pvp", series_format="games", game_count=3
        )
        room_id = room["id"]
        self.store.start(room_id)
        match_id = self.store.public(room_id)["active_match_id"]
        match = self.store._find_match(self.store._room(room_id), match_id)
        match["session"]["pvp_session_id"] = "s-single-games"
        for number, winner in ((1, "a"), (2, "b"), (3, "a")):
            plan = self.store._ensure_game_plan(self.store._room(room_id), match)
            self.store.record_native_game(room_id, match_id, {
                "event_id": f"e-single-{number}",
                "session_id": "s-single-games",
                "game_number": number,
                "winner": winner,
                "engine_winner": plan["seat_map"][winner],
                "seed": plan["seed"],
                "seat_plan": plan["seat_map"],
                "engine_seats": plan["seat_map"],
                "required_first_player": plan["required_first_player"],
            })
        result = self.store.public(room_id)
        self.assertEqual(result["status"], "finished")
        self.assertEqual(result["matches"][0]["points"], {"a": 6, "b": 3})
        self.assertEqual(result["matches"][0]["total_game_count"], 3)
        self.assertEqual(result["matches"][0]["bo2_count"], 0)
        self.assertEqual(result["matches"][0]["winner"], "p-1")
        self.assertEqual(
            [game["required_first_player"] for game in match["games"]],
            ["a", "b", "a"],
        )
        self.assertEqual(len({tuple(game["seed"]) for game in match["games"]}), 3)

    def test_native_first_to_four_final_reuses_session_and_alternates_games(self):
        room = self.store.create(
            "native真人决赛",
            ["A", "B", "C", "D"],
            engine="native-pvp",
            final_opponent_name="真人选手",
        )
        room_id = room["id"]
        self.store.start(room_id)

        while self.store.public(room_id)["stage"] == "round_robin":
            public = self.store.public(room_id)
            match_id = public["active_match_id"]
            match = self.store._find_match(self.store._room(room_id), match_id)
            for number in range(1, 21):
                match["session"]["pvp_session_id"] = f"s-{match_id}"
                plan = self.store._ensure_game_plan(
                    self.store._room(room_id), match
                )
                self.store.record_native_game(room_id, match_id, {
                    "event_id": f"{match_id}-{number}",
                    "session_id": f"s-{match_id}",
                    "game_number": number,
                    "winner": "a",
                    "engine_winner": plan["seat_map"]["a"],
                    "seed": plan["seed"],
                    "seat_plan": plan["seat_map"],
                    "engine_seats": plan["seat_map"],
                    "required_first_player": plan["required_first_player"],
                })

        final = self.store.public(room_id)["active_match"]
        final_id = final["id"]
        final_match = self.store._find_match(self.store._room(room_id), final_id)
        final_match["session"]["pvp_session_id"] = "s-final-continuous"
        for number, winner in enumerate(
            ["a", "b", "a", "b", "a", "b", "a"], start=1
        ):
            plan = self.store._ensure_game_plan(
                self.store._room(room_id), final_match
            )
            self.store.record_native_game(room_id, final_id, {
                "event_id": f"final-{number}",
                "session_id": "s-final-continuous",
                "game_number": number,
                "winner": winner,
                "engine_winner": plan["seat_map"][winner],
                "seed": plan["seed"],
                "seat_plan": plan["seat_map"],
                "engine_seats": plan["seat_map"],
                "required_first_player": plan["required_first_player"],
            })

        result = self.store.public(room_id)
        finished = next(
            match for match in result["matches"] if match["id"] == final_id
        )
        self.assertEqual(finished["status"], "finished")
        self.assertEqual(finished["session"]["pvp_session_id"], "s-final-continuous")
        self.assertEqual(
            [game["required_first_player"] for game in finished["games"]],
            ["a", "b", "a", "b", "a", "b", "a"],
        )
        self.assertEqual(
            len({tuple(game["seed"]) for game in finished["games"]}), 7
        )

    def test_legacy_series_final_phase_is_reloaded_as_regular(self):
        room = self.create(base_bo2_count=2, overtime_enabled=False)
        room_id = room["id"]
        self.store.start(room_id)
        self.record(room_id, ["a"])
        path = Path(self.temp.name) / f"{room_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["matches"][0]["phase"] = "finals"
        data["matches"][0]["games"][0]["phase"] = "finals"
        data["matches"][0]["game_plans"][0]["phase"] = "finals"
        data["matches"][0]["game_plans"][0]["pair_key"] = "finals:1"
        path.write_text(json.dumps(data), encoding="utf-8")
        loaded = TournamentStore(Path(self.temp.name))
        active = loaded.public(room_id)["active_match"]
        self.assertEqual(active["phase"], "regular")
        self.assertEqual(active["regular_game_count"], 1)
        loaded.record_game(room_id, active["id"], "a")
        updated = loaded.public(room_id)["active_match"]
        self.assertEqual(updated["points"]["a"], 3)
        self.assertEqual(updated["games"][0]["seed"], updated["games"][1]["seed"])

    def test_legacy_series_missing_bo2_losses_is_rebuilt(self):
        room = self.create(base_bo2_count=1, overtime_enabled=False)
        room_id = room["id"]
        self.store.start(room_id)
        self.record(room_id, ["a", "a"])
        path = Path(self.temp.name) / f"{room_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["matches"][0].pop("bo2_losses")
        path.write_text(json.dumps(data), encoding="utf-8")

        loaded = TournamentStore(Path(self.temp.name)).public(room_id)
        match = loaded["matches"][0]
        standings = {
            row["player_id"]: row for row in loaded["standings"]
        }
        self.assertEqual(match["bo2_losses"], {"a": 0, "b": 1})
        self.assertEqual(standings["p-1"]["bo2_losses"], 0)
        self.assertEqual(standings["p-2"]["bo2_losses"], 1)

    def test_legacy_missing_bo2_summaries_rebuild_without_double_counting(self):
        room = self.store.create(
            "legacy-round-robin-bo2",
            ["A", "B", "C"],
            base_bo2_count=1,
        )
        room_id = room["id"]
        self.store.start(room_id)
        self.record(room_id, ["a", "a"])
        path = Path(self.temp.name) / f"{room_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["matches"][0]["bo2s"] = []
        path.write_text(json.dumps(data), encoding="utf-8")

        loaded = TournamentStore(Path(self.temp.name)).public(room_id)
        match = loaded["matches"][0]
        self.assertEqual(match["bo2_count"], 1)
        self.assertEqual(match["points"], {"a": 3, "b": 0})
        self.assertEqual(match["bo2_wins"], {"a": 1, "b": 0})

    def test_legacy_series_final_phase_and_missing_bo2s_are_rebuilt(self):
        room = self.store.create(
            "legacy-series-phase",
            ["A", "B"],
            room_type="series",
            base_bo2_count=1,
            overtime_enabled=False,
        )
        room_id = room["id"]
        self.store.start(room_id)
        self.record(room_id, ["a", "a"])
        path = Path(self.temp.name) / f"{room_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        match = data["matches"][0]
        match["phase"] = "finals"
        match["bo2s"] = []
        for game in match["games"]:
            game["phase"] = "finals"
        for plan in match["game_plans"]:
            plan["phase"] = "finals"
            plan["pair_key"] = plan["pair_key"].replace(
                "regular:", "finals:", 1
            )
        path.write_text(json.dumps(data), encoding="utf-8")

        loaded = TournamentStore(Path(self.temp.name)).public(room_id)
        match = loaded["matches"][0]
        self.assertEqual(match["bo2_count"], 1)
        self.assertEqual(match["points"], {"a": 3, "b": 0})
        self.assertEqual(match["phase"], "finished")

    def test_close_room_preserves_scores_and_blocks_reentry(self):
        room = self.create(series_format="games", game_count=3)
        room_id = room["id"]
        self.store.start(room_id)
        match_id = self.store.public(room_id)["active_match_id"]
        self.record(room_id, ["a"])
        closed = self.store.close(room_id)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["matches"][0]["games"][0]["winner"], "a")
        self.assertIsNone(closed["active_match_id"])
        self.assertEqual(self.store.close(room_id)["status"], "closed")
        with self.assertRaisesRegex(TournamentError, "not running"):
            self.store.record_game(room_id, match_id, "a")
        with self.assertRaisesRegex(TournamentError, "closed"):
            self.store.join_player(room_id, room["player_access"][0]["token"])
        with self.assertRaisesRegex(TournamentError, "lobby"):
            self.store.start(room_id)
        self.assertEqual(
            TournamentStore(Path(self.temp.name)).public(room_id)["status"], "closed"
        )


class TournamentCloseApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tournament.server import TournamentApplication
        self.temp = tempfile.TemporaryDirectory()
        self.application = TournamentApplication(Path(self.temp.name), "admin-test")
        self.client = TestClient(TestServer(self.application.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_close_requires_admin_and_keeps_public_history(self):
        room = self.application.store.create(
            "close-api", ["A", "B"], room_type="series",
            series_format="games", game_count=3,
        )
        room_id = room["id"]
        self.application.store.start(room_id)
        match_id = self.application.store.public(room_id)["active_match_id"]
        self.application.store.record_game(room_id, match_id, "a")
        url = f"/api/rooms/{room_id}/close"
        unauthorized = await self.client.post(url)
        self.assertEqual(unauthorized.status, 401)
        response = await self.client.post(
            url, headers={"X-Tournament-Admin-Key": "admin-test"}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["status"], "closed")
        public = await self.client.get(f"/api/rooms/{room_id}")
        self.assertEqual((await public.json())["matches"][0]["total_game_count"], 1)

    async def test_native_close_revokes_finished_replay_session(self):
        closed_sessions = []

        async def close_session(request):
            closed_sessions.append((
                request.match_info["session_id"],
                request.headers.get("X-PVP-Internal-Key"),
            ))
            return web.json_response({"status": "closed"})

        gateway = web.Application()
        gateway.router.add_post("/api/sessions/{session_id}/close", close_session)
        server = TestServer(gateway)
        await server.start_server()
        try:
            self.application.pvp_url = str(server.make_url("")).rstrip("/")
            self.application.pvp_key = "pvp-test"
            room = self.application.store.create(
                "native-close", ["A", "B"], engine="native-pvp",
                room_type="series", series_format="games", game_count=3,
            )
            room_id = room["id"]
            self.application.store.start(room_id)
            match = self.application.store._find_match(
                self.application.store._room(room_id), "m-01"
            )
            match["session"]["pvp_session_id"] = "s-for-close"
            match["session"]["status"] = "finished"
            match["status"] = "finished"
            match["phase"] = "finished"
            stored_room = self.application.store._room(room_id)
            stored_room["active_match_id"] = None
            stored_room["status"] = "finished"
            stored_room["stage"] = "finished"
            self.application.store._save(stored_room)
            response = await self.client.post(
                f"/api/rooms/{room_id}/close",
                headers={"X-Tournament-Admin-Key": "admin-test"},
            )
            self.assertEqual(response.status, 200)
            self.assertEqual(closed_sessions, [("s-for-close", "pvp-test")])
            self.assertEqual(self.application.store.public(room_id)["status"], "closed")
        finally:
            await server.close()


class TournamentParallelNativeApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tournament.server import TournamentApplication
        self.temp = tempfile.TemporaryDirectory()
        self.created_sessions = []
        self.closed_sessions = []
        self.sealed_sessions = []
        self.replay_requests = []
        self.replay_archives = {}
        self.replay_statuses = {}
        self.native_statuses = {}
        self.create_delay = 0
        self.create_inflight = 0
        self.max_create_inflight = 0

        async def create_session(request):
            body = await request.json()
            self.create_inflight += 1
            self.max_create_inflight = max(
                self.max_create_inflight,
                self.create_inflight,
            )
            try:
                if self.create_delay:
                    await asyncio.sleep(self.create_delay)
                self.created_sessions.append(body)
                credentials = body["credentials"]
                return web.json_response({
                    "session": {
                        "id": f"s-{body['match_id']}",
                        "status": "waiting",
                        "fixed_deck": {
                            "name": "SkyStriker",
                            "canonical_sha256": "d" * 64,
                        },
                    },
                    "native": {
                        "host": "duel.example.test",
                        "port": 6009,
                        "player_passwords": {
                            "a": credentials["a"],
                            "b": credentials["b"],
                        },
                        "spectator_password": credentials["spectator"],
                    },
                })
            finally:
                self.create_inflight -= 1

        async def close_session(request):
            self.closed_sessions.append(request.match_info["session_id"])
            return web.json_response({"status": "closed"})

        async def seal_session(request):
            self.sealed_sessions.append((
                request.match_info["session_id"],
                (await request.json()).get("reason"),
            ))
            return web.json_response({
                "status": "finished",
                "series_finished": True,
                "replay_status": {
                    "state": "incomplete",
                    "available": False,
                    "complete": False,
                    "partial": False,
                    "replay_count": 0,
                    "expected_replay_count": 0,
                    "missing_game_numbers": [],
                },
            })

        async def session_status(request):
            session_id = request.match_info["session_id"]
            status = self.replay_statuses.get(session_id, {
                "state": "recording",
                "available": False,
                "complete": False,
                "partial": False,
                "replay_count": 0,
                "expected_replay_count": 1,
                "missing_game_numbers": [1],
            })
            payload = {
                "id": session_id,
                "status": "waiting",
                "connections": {
                    side: {
                        "state": "waiting",
                        "connected": False,
                        "reconnecting": False,
                    }
                    for side in ("a", "b")
                },
                "spectators": 0,
                "stall_warning_seconds": 120,
                "replay_status": status,
            }
            payload.update(self.native_statuses.get(session_id, {}))
            return web.json_response(payload)

        async def download_replays(request):
            self.assertEqual(
                request.headers.get("X-PVP-Internal-Key"),
                "pvp-test",
            )
            self.assertEqual(request.query.get("format"), "playable")
            session_id = request.match_info["session_id"]
            self.replay_requests.append(session_id)
            custom = self.replay_archives.get(session_id, ...)
            if isinstance(custom, web.HTTPException):
                raise custom
            if custom is None:
                raise web.HTTPConflict(text="replays are not available yet")
            if custom is not ...:
                return web.Response(
                    body=custom,
                    content_type="application/zip",
                )
            return web.Response(
                body=make_playable_replay_archive(
                    session_id,
                    {1: b"yrp3d" + session_id.encode("ascii")},
                ),
                content_type="application/zip",
            )

        gateway = web.Application()
        gateway.router.add_post("/api/sessions", create_session)
        gateway.router.add_get(
            "/api/sessions/{session_id}",
            session_status,
        )
        gateway.router.add_get(
            "/api/sessions/{session_id}/replays",
            download_replays,
        )
        gateway.router.add_post(
            "/api/sessions/{session_id}/close", close_session
        )
        gateway.router.add_post(
            "/api/sessions/{session_id}/seal", seal_session
        )
        self.gateway = TestServer(gateway)
        await self.gateway.start_server()

        self.application = TournamentApplication(
            Path(self.temp.name),
            "admin-test",
            pvp_url=str(self.gateway.make_url("")).rstrip("/"),
            pvp_key="pvp-test",
        )
        self.client = TestClient(TestServer(self.application.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.gateway.close()
        self.temp.cleanup()

    async def test_start_and_close_cover_every_parallel_pairing_room(self):
        room = self.application.store.create(
            "parallel-native-api",
            ["A", "B", "C"],
            engine="native-pvp",
            base_bo2_count=1,
        )
        response = await self.client.post(
            f"/api/rooms/{room['id']}/start",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )
        self.assertEqual(response.status, 200)
        started = await response.json()

        self.assertEqual(len(self.created_sessions), 3)
        self.assertEqual(
            {body["match_id"] for body in self.created_sessions},
            {f"m-{index:02d}" for index in range(1, 4)},
        )
        self.assertTrue(all(
            body["continuous_series"] for body in self.created_sessions
        ))
        self.assertEqual(len({
            code
            for body in self.created_sessions
            for code in body["credentials"].values()
        }), 9)
        self.assertTrue(all(
            match.get("observer_connection", {}).get("code")
            for match in started["active_matches"]
        ))

        closed = await self.client.post(
            f"/api/rooms/{room['id']}/close",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )
        self.assertEqual(closed.status, 200)
        self.assertEqual(
            set(self.closed_sessions),
            {f"s-m-{index:02d}" for index in range(1, 4)},
        )
        body = await closed.json()
        self.assertTrue(all(
            "observer_connection" not in match
            for match in body["matches"]
        ))

    async def test_native_adjudication_seals_stuck_session_and_scores_forfeit(self):
        room = self.application.store.create(
            "native-forfeit",
            ["A", "B", "C"],
            engine="native-pvp",
            base_bo2_count=1,
        )
        started = await self.client.post(
            f"/api/rooms/{room['id']}/start",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )
        self.assertEqual(started.status, 200)

        response = await self.client.post(
            f"/api/rooms/{room['id']}/matches/m-01/adjudicate",
            headers={"X-Tournament-Admin-Key": "admin-test"},
            json={
                "penalized_side": "a",
                "scope": "remaining",
                "reason": "模型卡死且重连失败",
            },
        )

        self.assertEqual(response.status, 200)
        self.assertIn(
            ("s-m-01", "模型卡死且重连失败"),
            self.sealed_sessions,
        )
        body = await response.json()
        match = next(
            item for item in body["matches"] if item["id"] == "m-01"
        )
        self.assertEqual(match["status"], "finished")
        self.assertEqual(match["points"], {"a": 0, "b": 3})
        self.assertEqual(match["forfeit_game_count"], 2)
        self.assertEqual(
            match["administrative_rulings"][-1]["penalized_player_name"],
            "A",
        )
        stored = self.application.store._find_match(
            self.application.store._room(room["id"]), "m-01"
        )
        self.assertIn(
            "s-m-01",
            stored["administratively_closed_sessions"],
        )

    async def test_native_force_finish_seals_session_and_assigns_points(self):
        room = self.application.store.create(
            "native-force-score",
            ["A", "B", "C"],
            engine="native-pvp",
            base_bo2_count=1,
        )
        started = await self.client.post(
            f"/api/rooms/{room['id']}/start",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )
        self.assertEqual(started.status, 200)

        response = await self.client.post(
            f"/api/rooms/{room['id']}/matches/m-01/force-finish",
            headers={"X-Tournament-Admin-Key": "admin-test"},
            json={
                "points_a": 1,
                "points_b": 1,
                "reason": "both players unavailable",
            },
        )

        self.assertEqual(response.status, 200)
        self.assertIn(
            ("s-m-01", "both players unavailable"),
            self.sealed_sessions,
        )
        body = await response.json()
        match = next(
            item for item in body["matches"] if item["id"] == "m-01"
        )
        self.assertEqual(match["status"], "finished")
        self.assertEqual(match["points"], {"a": 1, "b": 1})
        self.assertEqual(
            match["administrative_rulings"][-1]["type"],
            "forced_score",
        )

    async def test_parallel_pairing_rooms_are_provisioned_concurrently(self):
        self.create_delay = 0.05
        room = self.application.store.create(
            "parallel-native-concurrency",
            ["A", "B", "C", "D"],
            engine="native-pvp",
            base_bo2_count=1,
        )

        response = await self.client.post(
            f"/api/rooms/{room['id']}/start",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )

        self.assertEqual(response.status, 200)
        self.assertEqual(len(self.created_sessions), 6)
        self.assertGreaterEqual(self.max_create_inflight, 2)

    async def test_websocket_broadcast_keeps_player_connection_private(self):
        room = self.application.store.create(
            "personalized-websocket",
            ["A", "B"],
            engine="native-pvp",
            base_bo2_count=1,
            room_type="series",
        )
        room_id = room["id"]
        token = room["player_access"][0]["token"]
        started = await self.client.post(
            f"/api/rooms/{room_id}/start",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )
        self.assertEqual(started.status, 200)

        player_ws = await self.client.ws_connect(
            f"/ws/{room_id}?token={token}"
        )
        player_initial = await player_ws.receive_json()
        player_code = (
            player_initial["matches"][0]["session"]["connection"]["code"]
        )
        self.assertTrue(player_code)
        player_presence = await player_ws.receive_json()
        self.assertEqual(
            player_presence["matches"][0]["session"]["connection"]["code"],
            player_code,
        )

        public_ws = await self.client.ws_connect(f"/ws/{room_id}")
        public_initial = await public_ws.receive_json()
        self.assertNotIn(
            "connection",
            public_initial["matches"][0]["session"],
        )

        await self.application.broadcast(room_id)
        player_update = await player_ws.receive_json()
        public_update = await public_ws.receive_json()
        self.assertEqual(
            player_update["matches"][0]["session"]["connection"]["code"],
            player_code,
        )
        self.assertNotIn(
            "connection",
            public_update["matches"][0]["session"],
        )

        await public_ws.close()
        await player_ws.close()

    async def test_room_exposes_live_player_disconnect_and_stall_status(self):
        room = self.application.store.create(
            "native-connection-health",
            ["A", "B"],
            engine="native-pvp",
            base_bo2_count=1,
            room_type="series",
        )
        room_id = room["id"]
        started = await self.client.post(
            f"/api/rooms/{room_id}/start",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )
        self.assertEqual(started.status, 200)
        session_id = (
            self.application.store._room(room_id)["matches"][0]
            ["session"]["pvp_session_id"]
        )
        checked_at = time.time()
        self.native_statuses[session_id] = {
            "status": "dueling",
            "connections": {
                "a": {
                    "state": "stalled",
                    "connected": True,
                    "reconnecting": False,
                    "connected_at": checked_at - 300,
                    "last_activity_at": checked_at - 130,
                    "last_client_activity_at": checked_at - 130,
                    "last_server_activity_at": checked_at - 125,
                    "disconnected_at": None,
                    "reconnect_until": None,
                },
                "b": {
                    "state": "reconnecting",
                    "connected": False,
                    "reconnecting": True,
                    "reconnect_scope": "series",
                    "connected_at": None,
                    "last_activity_at": checked_at - 10,
                    "last_client_activity_at": checked_at - 10,
                    "last_server_activity_at": checked_at - 10,
                    "disconnected_at": checked_at - 8,
                    "reconnect_until": None,
                },
            },
            "spectators": 3,
            "stall_warning_seconds": 120,
        }

        response = await self.client.get(f"/api/rooms/{room_id}")
        self.assertEqual(response.status, 200)
        match = (await response.json())["matches"][0]
        health = match["session"]["connection_status"]

        self.assertEqual(health["session_id"], session_id)
        self.assertEqual(health["status"], "dueling")
        self.assertEqual(health["players"]["a"]["state"], "stalled")
        self.assertEqual(health["players"]["b"]["state"], "reconnecting")
        self.assertEqual(
            health["players"]["b"]["reconnect_scope"],
            "series",
        )
        self.assertEqual(health["spectators"], 3)
        self.assertEqual(health["stall_warning_seconds"], 120)

    async def test_room_refreshes_exact_partial_replay_status_from_pvp(self):
        room = self.application.store.create(
            "partial-replay-status",
            ["A", "B"],
            engine="native-pvp",
            base_bo2_count=1,
            room_type="series",
        )
        room_id = room["id"]
        started = await self.client.post(
            f"/api/rooms/{room_id}/start",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )
        self.assertEqual(started.status, 200)
        stored_room = self.application.store._room(room_id)
        match = self.application.store._find_match(stored_room, "m-01")
        session_id = match["session"]["pvp_session_id"]
        match["games"] = [
            {"number": 1, "winner": "a"},
            {"number": 2, "winner": "b", "replay": {"bytes": 10}},
        ]
        match["status"] = "finished"
        match["phase"] = "finished"
        match["session"]["status"] = "finished"
        stored_room["active_match_id"] = None
        stored_room["status"] = "finished"
        stored_room["stage"] = "finished"
        self.application.store._save(stored_room)
        self.replay_statuses[session_id] = {
            "state": "incomplete",
            "available": False,
            "complete": False,
            "partial": False,
            "replay_count": 1,
            "expected_replay_count": 2,
            "missing_game_numbers": [1],
        }

        incomplete = await self.client.get(f"/api/rooms/{room_id}")
        incomplete_match = (await incomplete.json())["matches"][0]

        self.assertNotIn("observer_connection", incomplete_match)
        self.assertEqual(
            incomplete_match["replay_status"]["missing_game_numbers"],
            [1],
        )

        self.replay_statuses[session_id] = {
            "state": "partial",
            "available": True,
            "complete": False,
            "partial": True,
            "replay_count": 1,
            "expected_replay_count": 2,
            "missing_game_numbers": [1],
        }
        partial = await self.client.get(f"/api/rooms/{room_id}")
        partial_match = (await partial.json())["matches"][0]

        self.assertEqual(
            partial_match["observer_connection"]["code"],
            match["session"]["spectator_code"],
        )
        spectator = await self.client.get(
            f"/api/rooms/{room_id}/native-spectator?match_id=m-01",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )
        self.assertEqual(spectator.status, 200)
        self.assertEqual((await spectator.json())["mode"], "replay")

    async def test_admin_and_player_can_download_scoped_replay_archives(self):
        room = self.application.store.create(
            "parallel-replay-api",
            ["A", "B", "C"],
            engine="native-pvp",
            base_bo2_count=1,
        )
        room_id = room["id"]
        player_token = room["player_access"][0]["token"]
        started = await self.client.post(
            f"/api/rooms/{room_id}/start",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )
        self.assertEqual(started.status, 200)

        unauthorized = await self.client.get(
            f"/api/rooms/{room_id}/matches/m-01/replays"
        )
        self.assertEqual(unauthorized.status, 401)

        single = await self.client.get(
            f"/api/rooms/{room_id}/matches/m-01/replays",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )
        self.assertEqual(single.status, 200)
        with zipfile.ZipFile(io.BytesIO(await single.read())) as archive:
            self.assertEqual(
                archive.namelist(),
                ["game-0001.yrp3d", "manifest.json"],
            )
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["format"], "mdpro3-yrp3d")
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest["missing_games"], [])

        all_admin = await self.client.get(
            f"/api/rooms/{room_id}/replays",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )
        self.assertEqual(all_admin.status, 200)
        with zipfile.ZipFile(io.BytesIO(await all_admin.read())) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "m-01/game-0001.yrp3d",
                    "m-02/game-0001.yrp3d",
                    "m-03/game-0001.yrp3d",
                    "manifest.json",
                },
            )

        player = await self.client.get(
            f"/api/rooms/{room_id}/replays",
            headers={"X-Tournament-Player-Token": player_token},
        )
        self.assertEqual(player.status, 200)
        with zipfile.ZipFile(io.BytesIO(await player.read())) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "m-01/game-0001.yrp3d",
                    "m-02/game-0001.yrp3d",
                    "manifest.json",
                },
            )

    async def test_match_replay_download_aggregates_historical_sessions(self):
        room = self.application.store.create(
            "historical-replay-api",
            ["A", "B"],
            engine="native-pvp",
            room_type="series",
            base_bo2_count=1,
        )
        room_id = room["id"]
        started = await self.client.post(
            f"/api/rooms/{room_id}/start",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )
        self.assertEqual(started.status, 200)
        match = self.application.store._room(room_id)["matches"][0]
        current_id = match["session"]["pvp_session_id"]
        match["pvp_sessions"] = [
            {"id": "s-historical", "start_game": 1},
            {"id": current_id, "start_game": 2},
        ]
        match["games"] = [
            {"number": 1, "winner": "a"},
            {"number": 2, "winner": "b"},
        ]
        self.application.store._save(
            self.application.store._room(room_id)
        )
        self.replay_archives["s-historical"] = make_playable_replay_archive(
            "s-historical",
            {1: b"yrp3ds-historical"},
        )
        self.replay_archives[current_id] = make_playable_replay_archive(
            current_id,
            {2: b"yrp3d" + current_id.encode("ascii")},
        )

        response = await self.client.get(
            f"/api/rooms/{room_id}/matches/{match['id']}/replays",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )

        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(io.BytesIO(await response.read())) as archive:
            self.assertEqual(
                archive.namelist(),
                [
                    "game-0001.yrp3d",
                    "game-0002.yrp3d",
                    "manifest.json",
                ],
            )
            self.assertEqual(
                archive.read("game-0001.yrp3d"),
                b"yrp3ds-historical",
            )
            self.assertEqual(
                archive.read("game-0002.yrp3d"),
                b"yrp3d" + current_id.encode("ascii"),
            )

    async def test_absolute_replay_manifest_does_not_double_offset_game_number(self):
        room = self.application.store.create(
            "absolute-replay-api",
            ["A", "B"],
            engine="native-pvp",
            room_type="series",
            base_bo2_count=1,
        )
        room_id = room["id"]
        match = self.application.store._room(room_id)["matches"][0]
        session_id = "s-absolute-21"
        replay_archive = make_playable_replay_archive(
            session_id,
            {21: b"yrp3dgame-21"},
        )
        match["games"] = [{"number": 21, "winner": "a"}]
        match["pvp_sessions"] = [
            {"id": session_id, "start_game": 21},
        ]
        match["session"]["pvp_session_id"] = session_id
        self.replay_archives[session_id] = replay_archive
        self.application.store._save(
            self.application.store._room(room_id)
        )

        response = await self.client.get(
            f"/api/rooms/{room_id}/matches/{match['id']}/replays",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )

        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(io.BytesIO(await response.read())) as archive:
            self.assertEqual(
                archive.namelist(),
                ["game-0021.yrp3d", "manifest.json"],
            )
            self.assertEqual(
                archive.read("game-0021.yrp3d"),
                b"yrp3dgame-21",
            )

    async def test_completed_game_with_missing_replay_returns_conflict(self):
        room = self.application.store.create(
            "missing-replay-api",
            ["A", "B"],
            engine="native-pvp",
            room_type="series",
            base_bo2_count=1,
        )
        room_id = room["id"]
        match = self.application.store._room(room_id)["matches"][0]
        match["games"] = [{"number": 1, "winner": "a"}]
        match["pvp_sessions"] = [
            {"id": "s-missing", "start_game": 1},
        ]
        match["session"]["pvp_session_id"] = "s-missing"
        self.replay_archives["s-missing"] = web.HTTPNotFound(text="session not found")
        self.application.store._save(
            self.application.store._room(room_id)
        )

        response = await self.client.get(
            f"/api/rooms/{room_id}/matches/{match['id']}/replays",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )

        self.assertEqual(response.status, 409)
        self.assertIn("missing", await response.text())

    async def test_bulk_download_skips_unplayed_rooms_without_replay_archives(self):
        room = self.application.store.create(
            "pending-replay-api", ["A", "B", "C"], engine="native-pvp",
            base_bo2_count=1,
        )
        room_id = room["id"]
        await self.client.post(
            f"/api/rooms/{room_id}/start",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )
        stored = self.application.store._room(room_id)
        stored["matches"][0]["games"] = [{"number": 1, "winner": "a"}]
        for match in stored["matches"][1:]:
            self.replay_archives[match["session"]["pvp_session_id"]] = None
        self.application.store._save(stored)
        response = await self.client.get(
            f"/api/rooms/{room_id}/replays",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )
        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(io.BytesIO(await response.read())) as archive:
            self.assertEqual(archive.namelist(), ["m-01/game-0001.yrp3d", "manifest.json"])

    async def test_replay_export_conflict_preserves_actual_reason(self):
        room = self.application.store.create(
            "replay-integrity-api", ["A", "B"], engine="native-pvp",
            room_type="series", base_bo2_count=1,
        )
        room_id = room["id"]
        match = self.application.store._room(room_id)["matches"][0]
        match["games"] = [{"number": 1, "winner": "a"}]
        match["pvp_sessions"] = [{"id": "s-integrity", "start_game": 1}]
        match["session"]["pvp_session_id"] = "s-integrity"
        self.replay_archives["s-integrity"] = web.HTTPConflict(
            text="native YRP does not match EVRP duel 18"
        )
        self.application.store._save(self.application.store._room(room_id))
        response = await self.client.get(
            f"/api/rooms/{room_id}/matches/{match['id']}/replays",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )
        self.assertEqual(response.status, 409)
        detail = await response.text()
        self.assertIn("native YRP does not match EVRP duel 18", detail)
        self.assertIn("s-integrity", detail)
        self.assertNotIn("session is missing", detail)

    async def test_declared_historical_prefix_is_downloaded_and_marked_incomplete(self):
        room = self.application.store.create(
            "historical-prefix-replay-api",
            ["A", "B"],
            engine="native-pvp",
            room_type="series",
            base_bo2_count=1,
        )
        room_id = room["id"]
        match = self.application.store._room(room_id)["matches"][0]
        session_id = "s-historical-prefix"
        match["games"] = [
            {"number": number, "winner": "a" if number % 2 else "b"}
            for number in range(1, 21)
        ]
        match["pvp_sessions"] = [
            {"id": session_id, "start_game": 1},
        ]
        match["session"]["pvp_session_id"] = session_id
        self.replay_archives[session_id] = make_playable_replay_archive(
            session_id,
            {
                number: f"yrp3dgame-{number}".encode("ascii")
                for number in range(5, 21)
            },
            missing_ordinals=[1, 2, 3, 4],
        )
        self.application.store._save(
            self.application.store._room(room_id)
        )

        response = await self.client.get(
            f"/api/rooms/{room_id}/matches/{match['id']}/replays",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )

        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(io.BytesIO(await response.read())) as archive:
            self.assertEqual(
                archive.namelist(),
                [
                    *(f"game-{number:04d}.yrp3d" for number in range(5, 21)),
                    "manifest.json",
                ],
            )
            manifest = json.loads(archive.read("manifest.json"))
            self.assertFalse(manifest["complete"])
            self.assertEqual(
                [item["ordinal"] for item in manifest["missing_games"]],
                [1, 2, 3, 4],
            )
            self.assertEqual(
                {item["reason"] for item in manifest["missing_games"]},
                {"historical_prefix_missing"},
            )

    async def test_partial_replay_archive_returns_conflict(self):
        room = self.application.store.create(
            "partial-replay-api",
            ["A", "B"],
            engine="native-pvp",
            room_type="series",
            base_bo2_count=1,
        )
        room_id = room["id"]
        match = self.application.store._room(room_id)["matches"][0]
        match["games"] = [
            {"number": 1, "winner": "a"},
            {"number": 2, "winner": "b"},
        ]
        match["pvp_sessions"] = [
            {"id": "s-partial", "start_game": 1},
        ]
        match["session"]["pvp_session_id"] = "s-partial"
        self.replay_archives["s-partial"] = make_playable_replay_archive(
            "s-partial",
            {1: b"yrp3dgame-1"},
        )
        self.application.store._save(
            self.application.store._room(room_id)
        )

        response = await self.client.get(
            f"/api/rooms/{room_id}/matches/{match['id']}/replays",
            headers={"X-Tournament-Admin-Key": "admin-test"},
        )

        self.assertEqual(response.status, 409)
        self.assertIn("2", await response.text())


class TournamentOptionalAdminApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from tournament.server import TournamentApplication
        self.temp = tempfile.TemporaryDirectory()
        self.application = TournamentApplication(Path(self.temp.name))
        self.client = TestClient(TestServer(self.application.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_admin_key_is_optional_when_not_configured(self):
        config = await self.client.get("/api/config")
        self.assertEqual(config.status, 200)
        self.assertFalse((await config.json())["admin_required"])

        created = await self.client.post(
            "/api/rooms",
            json={
                "name": "无密钥赛事",
                "room_type": "series",
                "players": ["A", "B"],
                "series_format": "games",
                "game_count": 1,
                "engine": "manual-admin",
            },
        )
        self.assertEqual(created.status, 200)
        room_id = (await created.json())["id"]

        closed = await self.client.post(f"/api/rooms/{room_id}/close")
        self.assertEqual(closed.status, 200)
        self.assertEqual((await closed.json())["status"], "closed")


if __name__ == "__main__":
    unittest.main()
