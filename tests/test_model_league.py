import unittest

from tools.model_league import (
    LeagueModel,
    apply_summary,
    empty_standings,
    ranked_standings,
    summarize_results,
)


class ModelLeagueTest(unittest.TestCase):
    def setUp(self):
        self.a = LeagueModel("a" * 64, "A", 100)
        self.b = LeagueModel("b" * 64, "B", 200)
        self.models = {
            self.a.model_id: self.a,
            self.b.model_id: self.b,
        }

    def test_bo2_maps_engine_seats_and_awards_three_points(self):
        summary = summarize_results(
            [
                {"game": 1, "outbound": {"winner": 0, "model_seat": 1}},
                {"game": 2, "outbound": {"winner": 1, "model_seat": 0}},
            ],
            self.a.model_id,
            self.b.model_id,
            self.models,
        )
        self.assertEqual(summary["bo2s"][0]["game_winners"],
                         [self.a.model_id, self.a.model_id])
        self.assertEqual(summary["points"],
                         {self.a.model_id: 3, self.b.model_id: 0})

    def test_bo2_split_is_a_draw_with_one_point_each(self):
        summary = summarize_results(
            [
                {"game": 1, "outbound": {"winner": 0, "model_seat": 1}},
                {"game": 2, "outbound": {"winner": 0, "model_seat": 0}},
            ],
            self.a.model_id,
            self.b.model_id,
            self.models,
        )
        self.assertEqual(summary["bo2s"][0]["result"], "draw")
        self.assertEqual(summary["points"],
                         {self.a.model_id: 1, self.b.model_id: 1})

    def test_standings_rank_by_points_then_wins(self):
        standings = empty_standings([self.a, self.b])
        standings[self.a.model_id]["points"] = 3
        standings[self.a.model_id]["bo2_wins"] = 1
        standings[self.b.model_id]["points"] = 1
        apply_summary(
            standings,
            {
                "model_a": self.a.public(),
                "model_b": self.b.public(),
                "points": {self.a.model_id: 0, self.b.model_id: 3},
                "bo2_wins": {self.a.model_id: 0, self.b.model_id: 1},
                "bo2_draws": 0,
                "game_wins": {self.a.model_id: 0, self.b.model_id: 2},
                "bo2_count": 1,
                "games": 2,
            },
        )
        ranked = ranked_standings(standings, [self.a, self.b])
        self.assertEqual(ranked[0]["model"]["id"], self.b.model_id)
        self.assertEqual(ranked[0]["points"], 4)
        self.assertEqual(ranked[1]["model"]["id"], self.a.model_id)


if __name__ == "__main__":
    unittest.main()
