from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class TournamentCopyControlsTests(unittest.TestCase):
    def test_player_connection_can_copy_address_and_code_with_fallback(self):
        script = (ROOT / "tournament/static/app.js").read_text(encoding="utf-8")

        self.assertIn('data-copy-message="服务器地址已复制"', script)
        self.assertIn('data-copy-message="连接码已复制"', script)
        self.assertIn('document.execCommand("copy")', script)
        self.assertIn("window.isSecureContext", script)

    def test_finished_parallel_league_keeps_replay_rooms_until_closed(self):
        script = (ROOT / "tournament/static/app.js").read_text(encoding="utf-8")

        self.assertIn(
            'const showParallelRooms = parallelLeague && room.status !== "closed";',
            script,
        )
        self.assertIn(
            'match.stage === "round_robin" || match.stage === "final"',
            script,
        )
        self.assertIn(
            "? esc(replayPhaseLabel(match)) : esc(phaseLabel(match))",
            script,
        )
        self.assertIn(
            '${showParallelRooms ? renderMatchRooms(room, admin, playerMode) : ""}',
            script,
        )

    def test_finished_series_uses_last_match_as_replay_panel(self):
        script = (ROOT / "tournament/static/app.js").read_text(encoding="utf-8")

        self.assertIn(
            'const active = room.active_match || (!parallelLeague ? finishedReplay : null);',
            script,
        )
        self.assertIn(
            'match.status === "finished" && match.observer_connection',
            script,
        )
        self.assertIn(
            "? esc(replayPhaseLabel(active)) : esc(phaseLabel(active))",
            script,
        )
        self.assertIn("录像不完整", script)
        self.assertIn("missing_game_numbers", script)

    def test_player_can_download_one_connection_code_per_line(self):
        script = (ROOT / "tournament/static/app.js").read_text(encoding="utf-8")

        self.assertIn('id="download-codebook"', script)
        self.assertIn('codes.join("\\r\\n")', script)
        self.assertIn("-connection-codes.txt", script)
        self.assertIn("playerConnectionCodes(room)", script)

    def test_replay_downloads_are_available_per_room_and_in_bulk(self):
        script = (ROOT / "tournament/static/app.js").read_text(encoding="utf-8")

        self.assertIn('id="download-all-replays"', script)
        self.assertIn('data-replay-match="${esc(match.id)}"', script)
        self.assertIn(
            "/matches/${matchId}/replays",
            script,
        )
        self.assertIn(
            "/api/rooms/${room.id}/replays",
            script,
        )

    def test_admin_can_adjudicate_and_undo_stuck_match_forfeits(self):
        script = (ROOT / "tournament/static/app.js").read_text(encoding="utf-8")
        styles = (ROOT / "tournament/static/app.css").read_text(encoding="utf-8")

        self.assertIn('data-adjudication-form="${esc(match.id)}"', script)
        self.assertIn('option value="remaining"', script)
        self.assertIn('option value="all"', script)
        self.assertIn("/matches/${matchId}/adjudicate", script)
        self.assertIn("/matches/${matchId}/adjudication/undo", script)
        self.assertIn("管理员判罚", script)
        self.assertIn("adjudicationDrafts", script)
        self.assertIn("data-adjudication-details", script)
        self.assertIn('form.addEventListener("input", saveDraft)', script)
        self.assertIn(".adjudication-controls", styles)
        self.assertIn(".adjudication-result", styles)

    def test_admin_can_force_finish_round_robin_with_final_points(self):
        script = (ROOT / "tournament/static/app.js").read_text(encoding="utf-8")

        self.assertIn('data-force-finish-form="${esc(match.id)}"', script)
        self.assertIn('name="points_a"', script)
        self.assertIn('name="points_b"', script)
        self.assertIn("/matches/${matchId}/force-finish", script)
        self.assertIn("forceFinishDrafts", script)
        self.assertIn("管理员强制结算", script)
        self.assertIn("已完成牌局和录像会保留", script)

    def test_live_native_rooms_show_disconnect_reconnect_and_stall_health(self):
        script = (ROOT / "tournament/static/app.js").read_text(encoding="utf-8")
        styles = (ROOT / "tournament/static/app.css").read_text(encoding="utf-8")

        self.assertIn("function playerConnectionState(match, side)", script)
        self.assertIn('"疑似卡住"', script)
        self.assertIn('"已断线，等待重连"', script)
        self.assertIn('"已退出 / 断线"', script)
        self.assertIn("last_activity_at", script)
        self.assertIn("disconnected_at", script)
        self.assertIn('renderPlayerConnectionState(match, "a")', script)
        self.assertIn('renderPlayerConnectionState(match, "b")', script)
        self.assertIn(".player-connection-state.stalled", styles)
        self.assertIn(".player-connection-state.reconnecting", styles)

    def test_champion_human_bo7_completion_policy_is_configurable(self):
        script = (ROOT / "tournament/static/app.js").read_text(encoding="utf-8")

        self.assertIn('name="final_completion_policy"', script)
        self.assertIn('value="first_to_four"', script)
        self.assertIn('value="play_all_seven"', script)
        self.assertIn(
            'final_completion_policy: data.get("final_completion_policy")',
            script,
        )
        self.assertIn(
            'match.completion_policy === "play_all_seven"',
            script,
        )
        self.assertIn("固定打满 7 局", script)


class OutboundRoomModelControlsTests(unittest.TestCase):
    def test_reconnecting_room_remains_visible_and_active(self):
        script = (
            ROOT / "panel/ygo-admin/app/static/outbound.js"
        ).read_text(encoding="utf-8")

        self.assertIn('reconnecting:"正在重连"', script)
        self.assertIn('"connecting","reconnecting","joining"', script)

    def test_each_room_has_a_model_selector_and_sends_structured_rooms(self):
        script = (
            ROOT / "panel/ygo-admin/app/static/outbound.js"
        ).read_text(encoding="utf-8")

        self.assertIn('class="outbound-model"', script)
        self.assertIn("function outboundRoomValues()", script)
        self.assertIn("body.rooms=rooms", script)
        self.assertIn('id="outbound-model-reset"', script)

    def test_bulk_passwords_do_not_pass_array_indexes_as_model_ids(self):
        script = (
            ROOT / "panel/ygo-admin/app/static/outbound.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "unique.map(value=>outboundPasswordRow(value)).join(\"\")",
            script,
        )
        self.assertNotIn("unique.map(outboundPasswordRow)", script)


if __name__ == "__main__":
    unittest.main()
