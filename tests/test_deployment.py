import argparse
from contextlib import redirect_stdout
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools import deploy


SOURCE = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(SOURCE / "room/admin"),
                str(SOURCE / "room/app-305m-admin-20260915")]
from registered_catalog import RegisteredCatalog


class DeploymentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self.room = self.repo / "room"
        self.app = self.room / "app-305m-admin-20260915"
        self.training = self.repo / "training"
        self.patch = patch.multiple(deploy, REPO=self.repo, ROOM=self.room, APP=self.app)
        self.patch.start()
        for name in deploy.ASSETS:
            path = self.app / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(name.encode())
            paired = self.training / name.removeprefix("project/")
            paired.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, paired)
        source_hash = "a" * 64
        (self.app / "native-build.json").write_text(json.dumps({
            "module": deploy.MODULE, "sha256": deploy.digest(self.app / deploy.MODULE),
            "source_hashes": {"ygopro.h": source_hash},
        }))
        (self.room / "outbound").mkdir()
        (self.room / "outbound/native_contract.json").write_text(json.dumps({"source_sha256": source_hash}))
        for name in ["project/assets/deck/BestowedDragon.ydk", "project/script/c1.lua", "bin/mono"]:
            path = self.app / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture")
        self.weights = self.repo / "trained.flax_model"
        self.weights.write_bytes(b"synthetic-test-weights")
        self.sidecar = self.repo / "trained.flax_model.json"
        self.sidecar.write_text(json.dumps({
            "schema": "ygo-training-checkpoint/v2", "architecture": "decision-v1",
            "model_args": {"decision_layers": 2},
        }))
        self.args = argparse.Namespace(checkpoint=self.weights, sidecar=self.sidecar,
                                       training_project=self.training, steps=640679936,
                                       label=None, activate=True)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def register(self):
        with redirect_stdout(io.StringIO()):
            deploy.register_model(self.args)
        return RegisteredCatalog(self.room / "admin/models.json", self.app)

    def test_register_stage_and_unranked_listing(self):
        catalog = self.register()
        listing = catalog.listing()
        self.assertEqual(listing["ranking"], "registered_unranked")
        self.assertIsNone(listing["items"][0]["win_rate"])
        model = catalog.stage(listing["items"][0]["id"])
        self.assertEqual(model["checkpoint_steps"], self.args.steps)
        self.assertEqual(model, json.loads((self.room / "data/admin/active-model.json").read_text()))

    def test_registration_is_idempotent_without_reactivation(self):
        self.register()
        self.args.activate = False
        catalog = self.register()
        self.assertEqual(len(catalog.entries()), 1)

    def test_refuses_runtime_training_card_pool_mismatch(self):
        (self.training / "scripts/code_list.txt").write_text("different")
        with self.assertRaisesRegex(ValueError, "Training assets"):
            self.register()
        self.assertFalse((self.room / "admin/models.json").exists())

    def test_refuses_unknown_architecture(self):
        document = json.loads(self.sidecar.read_text())
        document["architecture"] = "arbitrary-pytorch-policy"
        self.sidecar.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "sidecar"):
            self.register()

    def test_refuses_live_initial_activation(self):
        (self.room / "service.pid").write_text("123")
        with self.assertRaisesRegex(ValueError, "initial offline"):
            self.register()

    def test_sidecar_checkpoint_identity_must_match(self):
        document = json.loads(self.sidecar.read_text())
        document["sha256"] = "0" * 64
        self.sidecar.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "checkpoint hash"):
            self.register()

    def test_sidecar_training_steps_must_match(self):
        document = json.loads(self.sidecar.read_text())
        document["global_step"] = self.args.steps + 1
        self.sidecar.write_text(json.dumps(document))
        with self.assertRaisesRegex(ValueError, "Training steps"):
            self.register()

    def test_rejects_tampered_weights(self):
        catalog = self.register()
        row = catalog.entries()[0]
        (self.app / row["model"]["checkpoint_file"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(RuntimeError, "integrity"):
            catalog.stage(row["model"]["checkpoint_sha256"])

    def test_rejects_tampered_sidecar(self):
        catalog = self.register()
        row = catalog.entries()[0]
        sidecar = self.app / (row["model"]["checkpoint_file"] + ".json")
        sidecar.write_text("{}")
        with self.assertRaisesRegex(ValueError, "sidecar"):
            catalog.stage(row["model"]["checkpoint_sha256"])

    def test_rejects_runtime_asset_drift(self):
        catalog = self.register()
        row = catalog.entries()[0]
        (self.app / "project/scripts/code_list.txt").write_text("changed")
        with self.assertRaisesRegex(ValueError, "Training assets"):
            catalog.stage(row["model"]["checkpoint_sha256"])

    def test_rejects_catalog_path_traversal(self):
        catalog = self.register()
        path = self.room / "admin/models.json"
        document = json.loads(path.read_text())
        document["models"][0]["model"]["checkpoint_file"] = "../../escape"
        path.write_text(json.dumps(document))
        with self.assertRaises(ValueError):
            catalog.entries()

    def test_prepare_manifest_contains_native_assets_and_scripts_not_weights(self):
        self.register()
        with redirect_stdout(io.StringIO()):
            deploy.prepare_runtime(argparse.Namespace())
        files = json.loads((self.app / "manifest.json").read_text())["files"]
        self.assertTrue(set(deploy.ASSETS).issubset(files))
        self.assertIn("project/script/c1.lua", files)
        self.assertFalse(any(name.endswith(".flax_model") for name in files))

    def test_native_contract_mismatch_fails_closed(self):
        (self.room / "outbound/native_contract.json").write_text(json.dumps({"source_sha256": "b" * 64}))
        with self.assertRaisesRegex(ValueError, "contracts"):
            deploy.runtime_assets()

    def test_init_compute_does_not_print_or_rotate_credentials(self):
        output = io.StringIO()
        args = argparse.Namespace(gateway="https://compute.example.org/")
        with redirect_stdout(output):
            deploy.init_compute(args)
        private = json.loads((self.room / "private/panel-bootstrap.json").read_text())
        self.assertNotIn(private["token"], output.getvalue())
        self.assertNotIn(private["agent_key"], output.getvalue())
        saved = (self.room / "admin/control.key").read_bytes()
        with self.assertRaisesRegex(ValueError, "rotation"):
            deploy.init_compute(args)
        self.assertEqual(saved, (self.room / "admin/control.key").read_bytes())

    def test_endpoint_rejects_plaintext_and_credential_urls(self):
        for value in ["http://example.org", "https://user:pass@example.org",
                      "https://example.org/path", "https://example.org/?token=secret"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                deploy.endpoint(value)
        self.assertEqual(deploy.endpoint("http://127.0.0.1:8090", localhost=True),
                         "http://127.0.0.1:8090")

    def test_panel_bootstrap_generates_private_configuration(self):
        with redirect_stdout(io.StringIO()):
            deploy.init_compute(argparse.Namespace(gateway="https://compute.example.org"))
        deck = self.repo / "model.ydk"
        deck.write_text("#main\n" + "\n".join(map(str, range(1, 41))) + "\n#extra\n!side\n")
        args = argparse.Namespace(
            origin="https://admin.example.org", game_host="game.example.org",
            bootstrap=self.room / "private/panel-bootstrap.json", deck=deck, username="operator")
        imports = [str(SOURCE / "panel/ygo-tcp-bridge/app"),
                   str(SOURCE / "panel/ygo-admin/app")]
        password = "fixture-only-long-password"
        output = io.StringIO()
        with patch.object(sys, "path", [*imports, *sys.path]), patch(
                "getpass.getpass", side_effect=[password, password]), redirect_stdout(output):
            deploy.init_panel(args)
        site = json.loads((self.repo / "panel/private/site.json").read_text())
        user = json.loads((self.repo / "panel/private/user.json").read_text())
        bridge = json.loads((self.repo / "panel/private/bridge.json").read_text())
        self.assertEqual(site["agent_url"], "https://compute.example.org/control")
        self.assertEqual(user["username"], "operator")
        self.assertIsInstance(user["password"], dict)
        self.assertEqual(len(bridge["rooms"]), 2)
        self.assertNotIn(password, output.getvalue())
        self.assertNotIn(site["agent_key"], output.getvalue())
        self.assertNotIn(bridge["token"], output.getvalue())
        with self.assertRaisesRegex(ValueError, "rotation"):
            deploy.init_panel(args)

    def test_initial_preflight_verifies_registered_snapshot(self):
        self.register()
        with redirect_stdout(io.StringIO()):
            deploy.prepare_runtime(argparse.Namespace())
            deploy.preflight(argparse.Namespace(warmup=False))

    def test_tournament_unit_uses_private_loopback_and_required_env_file(self):
        linux_repo = Path("unit-fixture")
        output = linux_repo / "deploy" / "generated"
        try:
            with patch.object(deploy, "REPO", linux_repo), patch.object(
                    deploy.sys, "platform", "linux"), redirect_stdout(io.StringIO()):
                deploy.units(argparse.Namespace(role="tournament", user="ygo"))
            unit = (output / "ygo-tournament.service").read_text()
            self.assertIn("ExecStart=", unit)
            self.assertIn("-m tournament.server", unit)
            self.assertIn("--host 127.0.0.1 --port 8787", unit)
            self.assertIn("EnvironmentFile=/etc/ygo-tournament.env", unit)
            self.assertIn("StateDirectory=ygo-tournament", unit)
        finally:
            shutil.rmtree(linux_repo, ignore_errors=True)

    def test_native_runtime_unit_keeps_evolution_ports_out_of_service_arguments(self):
        unit = (SOURCE / "deploy/ygo-evolution.service.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("--env-file=/opt/ygo-evolution/.env", unit)
        self.assertIn("/opt/ygo-evolution/dist/src/index.js", unit)
        self.assertIn("NODE_OPTIONS=--max-old-space-size=256", unit)
        self.assertIn("User=ygo-evolution", unit)

    def test_native_gateway_units_match_isolated_remote_install_roots(self):
        pvp = (SOURCE / "deploy/ygo-pvp.service.example").read_text(encoding="utf-8")
        tournament = (SOURCE / "deploy/ygo-tournament.service.example").read_text(
            encoding="utf-8"
        )
        evolution = (SOURCE / "deploy/ygo-evolution.service.example").read_text(
            encoding="utf-8"
        )
        tournament_server = (SOURCE / "tournament/server.py").read_text(
            encoding="utf-8"
        )
        pvp_server = (SOURCE / "pvp/server.py").read_text(encoding="utf-8")
        evolution_room = (
            SOURCE / "deploy/evolution-patch/YGOProRoom.js"
        ).read_text(encoding="utf-8")
        evolution_dueling = (
            SOURCE / "deploy/evolution-patch/YGOProDuelingState.js"
        ).read_text(encoding="utf-8")
        evolution_waiting = (
            SOURCE / "deploy/evolution-patch/YGOProWaitingState.js"
        ).read_text(encoding="utf-8")
        evolution_evrp = (
            SOURCE / "deploy/evolution-patch/EvrpSerializer.js"
        ).read_text(encoding="utf-8")
        evolution_disconnect = (
            SOURCE / "deploy/evolution-patch/DisconnectHandler.js"
        ).read_text(encoding="utf-8")
        evolution_core = (
            SOURCE / "deploy/evolution-patch/ocgcore.js"
        ).read_text(encoding="utf-8")
        spectator_view = (
            SOURCE / "deploy/evolution-patch/SpectatorView.js"
        ).read_text(encoding="utf-8")
        replay_recovery = (
            SOURCE / "tools/recover_evrp_from_yrp.js"
        ).read_text(encoding="utf-8")
        replay_e2e = (
            SOURCE / "tools/native_replay_e2e.py"
        ).read_text(encoding="utf-8")
        continuous_deploy = (
            SOURCE / "deploy/aliyun-continuous-series.sh"
        ).read_text(encoding="utf-8")
        replay_deploy = (
            SOURCE / "deploy/aliyun-series-replay.sh"
        ).read_text(encoding="utf-8")
        replay_access_deploy = (
            SOURCE / "deploy/aliyun-tournament-replay-access.sh"
        ).read_text(encoding="utf-8")
        transition_deploy = (
            SOURCE / "deploy/aliyun-series-transition-guard.sh"
        ).read_text(encoding="utf-8")
        reconnect_downloads_deploy = (
            SOURCE / "deploy/aliyun-tournament-reconnect-downloads.sh"
        ).read_text(encoding="utf-8")
        overtime_replay_deploy = (
            SOURCE / "deploy/aliyun-final-overtime-replay-fix.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("WorkingDirectory=/opt/ygo-pvp/app", pvp)
        self.assertIn("/opt/ygo-pvp/venv/bin/python -m pvp.server", pvp)
        self.assertIn(
            "--fixed-deck /etc/ygo-pvp/fixed-decks/SkyStriker.ydk",
            pvp,
        )
        self.assertIn("--room-registry-dir /run/ygo-provisioning", pvp)
        self.assertIn("RuntimeDirectory=ygo-provisioning", pvp)
        self.assertIn(
            "Environment=YGO_PROVISIONING_DIR=/run/ygo-provisioning",
            evolution,
        )
        self.assertIn("WorkingDirectory=/opt/ygo-tournament/app", tournament)
        self.assertIn("/opt/ygo-tournament/venv/bin/python -m tournament.server", tournament)
        self.assertIn("--pvp-url http://127.0.0.1:8790", tournament)
        self.assertIn('"format": room.get("native_format", "nc")', tournament_server)
        self.assertIn(
            "value.seat_plan[value.required_first_player] !== 0",
            evolution_room,
        )
        self.assertIn('typeof value.players.a !== "string"', evolution_room)
        self.assertIn("plannedPlace(playerName)", evolution_room)
        self.assertIn(
            "const plannedPlace = this.plannedPlace(playerInfo.name)",
            evolution_room,
        )
        self.assertIn(
            "restoreTemporarySeatOneOccupantUnsafe()",
            evolution_room,
        )
        self.assertIn(
            "plannedPosition === 1 &&",
            evolution_room,
        )
        self.assertIn(
            "playerChangeMessage(oldPosition, 1)",
            evolution_room,
        )
        self.assertIn("displaced.playerPosition(1, team)", evolution_room)
        restore_block_start = evolution_room.index(
            "restoreTemporarySeatOneOccupantUnsafe() {"
        )
        restore_block_end = evolution_room.index(
            "// Adapter that lets AdmitToRoom",
            restore_block_start,
        )
        restore_block = evolution_room[restore_block_start:restore_block_end]
        self.assertIn("const wasReady = displaced.isReady;", restore_block)
        self.assertNotIn("displaced.notReady();", restore_block)
        self.assertIn(
            "wasReady\n"
            "                    ? ygopro_msg_encode_1.PlayerChangeState.READY",
            restore_block,
        )
        self.assertIn(
            "typeChangeMessage(\n"
            "                displaced.position,\n"
            "                displaced.host,",
            evolution_room,
        )
        temporary_seat = evolution_room.index(
            "plannedPosition === 1 &&"
        )
        restore_before_seat_zero = evolution_room.index(
            "if (plannedPosition === 0)",
        )
        self.assertLess(restore_before_seat_zero, temporary_seat)
        native_smoke = (
            SOURCE / "tools/native_pvp_smoke.py"
        ).read_text(encoding="utf-8")
        native_series = (
            SOURCE / "tools/native_series_e2e.py"
        ).read_text(encoding="utf-8")
        self.assertIn("self.lobby_admitted = threading.Event()", native_smoke)
        self.assertIn(
            "clients[first_label].lobby_admitted.wait(args.timeout)",
            native_smoke,
        )
        self.assertNotIn('if state == "joining":\n            connected.set()', native_smoke)
        self.assertIn("positions[:2] != [0, 1]", native_series)
        self.assertIn(
            'natural_mirror = os.environ.get("TEST_NATURAL_MIRROR") == "1"',
            native_series,
        )
        self.assertIn(
            'os.environ.get("TEST_NATURAL_RESULT") == "1"',
            native_series,
        )
        self.assertIn(
            'command.extend(("--surrender-plan", "natural,natural"))',
            native_series,
        )
        self.assertIn("def natural_win_reason(value: object) -> bool:", native_series)
        self.assertIn("value in (1, 2)", native_series)
        self.assertIn('"natural,b"', native_series)
        self.assertIn(
            'side not in ("a", "b", "natural")',
            native_smoke,
        )
        self.assertIn(
            '"players": copy.deepcopy(native_player_names(session))',
            pvp_server,
        )
        self.assertNotIn("_begin_backend_admission", pvp_server)
        self.assertNotIn("expectedFirstSeat", evolution_room)
        self.assertIn("full_spectator", evolution_room)
        self.assertIn(
            "hostInfo = { ...hostInfo, time_limit: provisioning.time_limit }",
            evolution_room,
        )
        self.assertIn(
            '"time_limit": TOURNAMENT_TIME_LIMIT_SECONDS',
            pvp_server,
        )
        self.assertNotIn("full_spectator", evolution_core)
        self.assertIn("this.room.spectatorView(message)", evolution_core)
        self.assertIn("this.provisioning?.full_spectator === true", evolution_room)
        self.assertIn(
            "record.toPlayback((message) => this.spectatorView(message))",
            evolution_room,
        )
        self.assertIn("LOCATION_HAND", spectator_view)
        self.assertIn("MSG_DECK_TOP", spectator_view)
        self.assertIn("return message.observerView();", spectator_view)
        self.assertIn("createDuelFromYrp", replay_recovery)
        self.assertIn("new YGOProMsgStart()", replay_recovery)
        self.assertIn("new YGOProMsgUpdateData()", replay_recovery)
        self.assertIn("new YGOProMsgUpdateCard()", replay_recovery)
        self.assertIn('"field_update"', replay_e2e)
        self.assertIn("observer.field_updates > 0", replay_e2e)
        self.assertIn('"$stage/SpectatorView.js"', continuous_deploy)
        self.assertIn("domain/SpectatorView.js", continuous_deploy)
        self.assertIn('[[ "$source" != "SpectatorView.js" ]]', continuous_deploy)
        self.assertIn(
            "const hasSeed = value.seed !== null && value.seed !== undefined",
            evolution_room,
        )
        self.assertIn("required_first_player: null", evolution_room)
        self.assertIn('"$stage/ocgcore.js"', continuous_deploy)
        self.assertIn("ocgcore-worker/ocgcore.js", continuous_deploy)
        self.assertIn('"$stage/pvp/protocol.py"', continuous_deploy)
        self.assertIn("/opt/ygo-pvp/app/pvp/protocol.py", continuous_deploy)
        self.assertIn("authoritativeResultObserved = false;", evolution_core)
        self.assertIn("async waitForAdvanceIdle()", evolution_core)
        self.assertIn(
            "if (this.authoritativeResultObserved || this.room.isFinished())",
            evolution_core,
        )
        swapped_players = evolution_core.index(
            "const players = duelRecord.toSwappedPlayers();"
        )
        seat_shuffle = evolution_core.index(
            "shuffleDecksBySeed)(playerDecks, duelRecord.seed)"
        )
        replay_deck_update = evolution_core.index(
            "player.deck = shuffledDecks[index];"
        )
        core_deck_conversion = evolution_core.index(
            "const decks = shuffledDecks.map("
        )
        self.assertLess(swapped_players, seat_shuffle)
        self.assertLess(seat_shuffle, replay_deck_update)
        self.assertLess(replay_deck_update, core_deck_conversion)
        generate_players_start = evolution_core.index("generatePlayers() {")
        generate_players_end = evolution_core.index(
            "async queryFieldCount",
            generate_players_start,
        )
        self.assertNotIn(
            "shuffleDecksBySeed",
            evolution_core[generate_players_start:generate_players_end],
        )
        natural_win_start = evolution_core.index(
            "if (processedMessage instanceof ygopro_msg_encode_1.YGOProMsgWin)"
        )
        natural_win_end = evolution_core.index(
            "await this.routeGameMsg(processedMessage)",
            natural_win_start,
        )
        natural_win_block = evolution_core[natural_win_start:natural_win_end]
        self.assertIn("return true;", natural_win_block)
        self.assertNotIn("this.ocgcore?.dispose()", natural_win_block)
        self.assertGreaterEqual(
            evolution_dueling.count("await this.ocgCore.waitForAdvanceIdle();"),
            3,
        )
        self.assertIn(
            "await this.ocgCore.waitForAdvanceIdle();\n"
            "        this.disposeCore();",
            evolution_dueling,
        )
        self.assertIn(
            "const SERIES_GAME_TRANSITION_DELAY_MS = 1000;",
            evolution_dueling,
        )
        self.assertIn(
            "const SERIES_SURRENDER_TRANSITION_DELAY_MS = 3000;",
            evolution_dueling,
        )
        self.assertIn(
            "const SERIES_SURRENDER_GUARD_MS = 250;",
            evolution_dueling,
        )
        self.assertIn("seriesSurrenderIgnoreUntil = 0;", evolution_room)
        self.assertIn("suppressSeriesSurrendersFor(durationMs)", evolution_room)
        self.assertIn("isSeriesSurrenderSuppressed()", evolution_room)
        self.assertIn("seriesTransitioning = false;", evolution_room)
        self.assertIn("beginSeriesTransition()", evolution_room)
        self.assertIn("isSeriesTransitionInProgress()", evolution_room)
        self.assertIn("hasConnectedSeriesPlayers()", evolution_room)
        self.assertIn('throw new Error("player_disconnected_between_games")', evolution_room)
        self.assertIn("finishSeriesTransition()", evolution_room)
        self.assertIn("seriesReplay = null;", evolution_room)
        self.assertIn("loadSeriesReplay(normalized)", evolution_room)
        self.assertIn("async playSeriesReplay(spectator)", evolution_room)
        self.assertIn("duel.spectatorFrames", evolution_room)
        self.assertIn("spectator.disconnectGracefully(60000)", evolution_room)
        self.assertIn(
            'this.provisioning?.series_finished === true',
            evolution_room,
        )
        self.assertIn("room.playSeriesReplay(spectator)", evolution_waiting)
        self.assertIn("startReadyRoom(room)", evolution_waiting)
        self.assertIn(
            "room.players.length !== requiredPlayerCount",
            evolution_waiting,
        )
        self.assertIn(
            "room.players.some((player) => player.socket.closed)",
            evolution_waiting,
        )
        self.assertIn(
            "this.starting = false;\n"
            "            this.logger.error(`startReadyRoom failed:",
            evolution_waiting,
        )
        self.assertIn(
            "room.provisioning?.continuous_series === true",
            evolution_waiting,
        )
        self.assertGreaterEqual(
            evolution_waiting.count("await room.mutex.runExclusive"),
            2,
        )
        self.assertIn("spectatorFrames: Array.from(", evolution_evrp)
        self.assertIn(
            "room.spectatorView(message)",
            evolution_evrp,
        )
        self.assertIn(
            "duelRecord.gameNumber = this.room.seriesGameNumber",
            evolution_dueling,
        )
        self.assertIn("gameNumber: record.gameNumber", evolution_evrp)
        self.assertIn("record.toSwappedPlayers()", evolution_evrp)
        spectator_lookup = evolution_disconnect.index(
            "const player = room.players.find",
            evolution_disconnect.index("handleYGOPro(room)"),
        )
        empty_player_check = evolution_disconnect.index(
            "if (room.hasNoConnectedPlayers)",
            spectator_lookup,
        )
        self.assertLess(spectator_lookup, empty_player_check)
        self.assertIn(
            "room.hasNoConnectedPlayers && room.spectators.length === 0",
            evolution_disconnect,
        )
        self.assertIn("record_series_replay_chunk", pvp_server)
        self.assertIn("series_replay_available", pvp_server)
        self.assertIn("elif opcode == P.EVRP_EXPORT:", pvp_server)
        self.assertIn(
            "refusing deployment while tournament players are connected",
            replay_deploy,
        )
        self.assertIn("/var/lib/ygo-pvp/replays", replay_deploy)
        self.assertIn("series-replay-$stamp", replay_deploy)
        self.assertIn('"$stage/DisconnectHandler.js"', replay_deploy)
        self.assertIn(
            "shared/room/application/DisconnectHandler.js",
            replay_deploy,
        )
        self.assertIn(
            "refusing deployment while tournament players are connected",
            replay_access_deploy,
        )
        self.assertIn("replay-access-$stamp", replay_access_deploy)
        self.assertIn("systemctl restart ygo-tournament", replay_access_deploy)
        self.assertIn(
            "refusing deployment while tournament players are connected",
            transition_deploy,
        )
        self.assertIn("deployment baseline changed", transition_deploy)
        self.assertIn("systemctl restart ygo-pvp ygo-evolution", transition_deploy)
        self.assertIn(
            "refusing deployment while a tournament session is connected "
            "or recently active",
            reconnect_downloads_deploy,
        )
        self.assertIn(
            'force_live_deploy="${FORCE_LIVE_DEPLOY:-0}"',
            reconnect_downloads_deploy,
        )
        self.assertIn(
            "connected tournament clients will be disconnected",
            reconnect_downloads_deploy,
        )
        self.assertIn("deployment baseline changed", reconnect_downloads_deploy)
        self.assertIn("rollback()", reconnect_downloads_deploy)
        self.assertIn(
            'replay_source="$stage/pvp/replay.py"',
            reconnect_downloads_deploy,
        )
        self.assertIn(
            'replay_target="/opt/ygo-pvp/app/pvp/replay.py"',
            reconnect_downloads_deploy,
        )
        self.assertIn(
            'evrp_source="$stage/EvrpSerializer.js"',
            reconnect_downloads_deploy,
        )
        self.assertIn(
            "room/domain/replay/EvrpSerializer.js",
            reconnect_downloads_deploy,
        )
        self.assertIn("replay_existed=0", reconnect_downloads_deploy)
        self.assertIn(
            'sudo -n rm -f "$replay_target"',
            reconnect_downloads_deploy,
        )
        self.assertIn(
            '"$backup/evolution/EvrpSerializer.js" "$evrp_target"',
            reconnect_downloads_deploy,
        )
        self.assertIn(
            '"$replay_source" "$replay_target"',
            reconnect_downloads_deploy,
        )
        self.assertIn(
            '"$evrp_source" "$evrp_target"',
            reconnect_downloads_deploy,
        )
        self.assertIn("systemctl restart ygo-evolution", reconnect_downloads_deploy)
        self.assertIn("systemctl restart ygo-pvp", reconnect_downloads_deploy)
        self.assertIn("systemctl restart ygo-tournament", reconnect_downloads_deploy)
        self.assertIn(
            "refusing deployment while port 6009 has established connections",
            overtime_replay_deploy,
        )
        self.assertIn(
            "refusing deployment while tournament players are connected",
            overtime_replay_deploy,
        )
        self.assertIn("deployment baseline changed", overtime_replay_deploy)
        self.assertIn(
            "next native session was not provisioned",
            overtime_replay_deploy,
        )
        self.assertNotIn("game 21", overtime_replay_deploy)
        self.assertNotIn('{"a": 1, "b": 0}', overtime_replay_deploy)
        self.assertIn(
            "restore_service_states",
            overtime_replay_deploy,
        )
        self.assertIn(
            "systemctl stop ygo-tournament",
            overtime_replay_deploy,
        )
        surrender_guard = evolution_dueling.index(
            "if (this.room.isSeriesSurrenderSuppressed())"
        )
        surrender_execution = evolution_dueling.index(
            "this.endedBySurrender = true;"
        )
        self.assertLess(surrender_guard, surrender_execution)
        continuous_start = evolution_dueling.index(
            "if (this.room.provisioning?.continuous_series)"
        )
        change_side = evolution_dueling.index(
            "client.sendMessageToClient(this.room.messageSender.changeSideMessage())",
            continuous_start,
        )
        wait_next_game = evolution_dueling.index(
            "const moreGames = await this.room.waitForNextSeriesGame();",
            continuous_start,
        )
        surrender_delay = evolution_dueling.index(
            "? SERIES_SURRENDER_TRANSITION_DELAY_MS",
            change_side,
        )
        suppression = evolution_dueling.index(
            "this.room.suppressSeriesSurrendersFor("
            "transitionDelayMs + SERIES_SURRENDER_GUARD_MS)",
            surrender_delay,
        )
        reset_delay = evolution_dueling.index(
            "await new Promise((resolve) => setTimeout(resolve, "
            "transitionDelayMs));",
            suppression,
        )
        refreshed_guard = evolution_dueling.index(
            "this.room.suppressSeriesSurrendersFor("
            "SERIES_SURRENDER_GUARD_MS)",
            reset_delay,
        )
        connected_wait = evolution_dueling.index(
            "await this.room.waitForSeriesPlayersConnected("
            "SERIES_PLAYER_RECONNECT_TIMEOUT_MS)",
            refreshed_guard,
        )
        finish_transition = evolution_dueling.index(
            "this.room.finishSeriesTransition()",
            connected_wait,
        )
        transition_end = evolution_dueling.index(
            "this.room.dueling();",
            finish_transition,
        )
        self.assertNotIn(
            "duelStartMessage()",
            evolution_dueling[finish_transition:transition_end],
        )
        lp_reset = evolution_dueling.index(
            "this.room.seriesGameNumber > 1"
        )
        next_duel = evolution_dueling.index(
            "client.sendMessageToClient(this.room.messageSender.duelStartMessage())",
            lp_reset,
        )
        next_start = evolution_dueling.index(
            "const createStartMsg",
            next_duel,
        )
        self.assertLess(change_side, surrender_delay)
        self.assertLess(change_side, wait_next_game)
        self.assertLess(surrender_delay, suppression)
        self.assertLess(suppression, reset_delay)
        self.assertLess(reset_delay, refreshed_guard)
        self.assertLess(refreshed_guard, wait_next_game)
        self.assertLess(wait_next_game, connected_wait)
        self.assertLess(connected_wait, finish_transition)
        self.assertLess(next_duel, next_start)
        self.assertIn(
            "const SERIES_PLAYER_RECONNECT_TIMEOUT_MS = 600000;",
            evolution_dueling,
        )
        self.assertIn(
            "if (this.room.isSeriesTransitionInProgress())",
            evolution_dueling,
        )
        self.assertIn("SERIES_LEAVE_GUARD_SECONDS = 10", pvp_server)
        self.assertIn("ignored stale between-game LEAVE_GAME", pvp_server)
        self.assertIn(
            '"/api/sessions/{session_id}/replays"',
            pvp_server,
        )
        self.assertIn(
            '"/api/rooms/{room_id}/matches/{match_id}/replays"',
            tournament_server,
        )


class DeploymentSafetyContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.deploy_script = (
            SOURCE / "deploy/aliyun-final-overtime-replay-fix.sh"
        ).read_text(encoding="utf-8")
        cls.recovery_script = (
            SOURCE / "deploy/aliyun-recover-finished-replay.sh"
        ).read_text(encoding="utf-8")
        cls.pvp_unit = (
            SOURCE / "deploy/ygo-pvp.service.example"
        ).read_text(encoding="utf-8")
        cls.evolution_unit = (
            SOURCE / "deploy/ygo-evolution.service.example"
        ).read_text(encoding="utf-8")
        cls.tournament_unit = (
            SOURCE / "deploy/ygo-tournament.service.example"
        ).read_text(encoding="utf-8")

    def test_shell_scripts_pass_syntax_validation(self):
        bash = shutil.which("bash")
        if bash is None:
            self.skipTest("bash is not installed")
        for relative in (
            "deploy/aliyun-administrative-settlement-replay.sh",
            "deploy/aliyun-between-game-retention.sh",
            "deploy/aliyun-final-overtime-replay-fix.sh",
            "deploy/aliyun-recover-finished-replay.sh",
            "deploy/aliyun-tournament-reconnect-downloads.sh",
        ):
            with self.subTest(script=relative):
                result = subprocess.run(
                    [bash, "-n", relative],
                    cwd=SOURCE,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_deployment_validates_stage_before_production_preflight(self):
        script = self.deploy_script
        first_stage_compile = script.index(
            "validate_python_source "
            "/opt/ygo-tournament/venv/bin/python \"$tournament_source\""
        )
        baseline = script.index(
            'check_baseline "$tournament_target"'
        )
        live_check = script.index(
            'assert_no_live_connections "pre-maintenance"'
        )
        first_install = script.index(
            'sudo -n install -m 640 -o ygo-tournament'
        )
        self.assertLess(first_stage_compile, baseline)
        self.assertLess(baseline, live_check)
        self.assertLess(live_check, first_install)

    def test_between_game_deployment_only_forces_stale_one_sided_sessions(self):
        script = (
            SOURCE / "deploy/aliyun-between-game-retention.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('force_stale_deploy="${FORCE_STALE_DEPLOY:-0}"', script)
        self.assertIn(
            "forced deployment requires exactly one connected side per ",
            script,
        )
        self.assertIn('"active session"', script)
        self.assertIn(
            "connected side is not stale enough for forced deployment",
            script,
        )
        self.assertIn("timestamp - last_activity < 300", script)
        self.assertIn("rollback()", script)

    def test_deployment_uses_verified_production_baselines(self):
        self.assertIn(
            'expected_tournament="'
            '56e29fce7dd5762e12db96c61eb5d3b855b2789da29d1006ec1ec9558175c104"',
            self.deploy_script,
        )
        self.assertIn(
            'expected_pvp="'
            '66132d151e6f60e2531e514401b6a5569b8e7caf1c3eaa23254921a793a36c8e"',
            self.deploy_script,
        )

    def test_deployment_maintenance_backup_and_rollback_are_ordered(self):
        script = self.deploy_script
        precheck = script.index(
            'assert_no_live_connections "pre-maintenance"'
        )
        stop = script.index("sudo -n systemctl stop ygo-tournament", precheck)
        recheck = script.index(
            'assert_no_live_connections "maintenance-window"',
            stop,
        )
        archive = script.index(
            '"$backup/data/ygo-tournament.tar"',
            recheck,
        )
        verified = script.index("backup_ready=1", archive)
        mutation = script.index("mutation_started=1", verified)
        first_install = script.index(
            'sudo -n install -m 640 -o ygo-tournament',
            mutation,
        )
        self.assertLess(precheck, stop)
        self.assertLess(stop, recheck)
        self.assertLess(recheck, archive)
        self.assertLess(archive, verified)
        self.assertLess(verified, mutation)
        self.assertLess(mutation, first_install)
        self.assertIn("trap 'on_error \"$?\"' ERR", script)
        self.assertIn("trap 'on_exit \"$?\"' EXIT", script)
        self.assertIn("restore_data_archive", script)
        self.assertIn("restore_service_states", script)
        self.assertIn("sha256sum -c SHA256SUMS", script)
        self.assertIn("data/ygo-pvp.tar", script)
        self.assertIn("data/ygo-tournament.tar", script)

    def test_deployment_next_game_check_is_derived_and_idempotent(self):
        script = self.deploy_script
        self.assertIn(
            'expected_game = len(match.get("games") or []) + 1',
            script,
        )
        self.assertIn(
            'native.get("seat_plan") != plan.get("seat_map")',
            script,
        )
        self.assertIn(
            'native.get("required_first_player")',
            script,
        )
        self.assertIn(
            "deployment already applied and next native session is valid",
            script,
        )
        self.assertNotIn("game 21", script)
        self.assertNotIn('{"a": 1, "b": 0}', script)

    def test_recovery_requires_bound_identity_and_rejects_sha_conflicts(self):
        script = self.recovery_script
        for field in (
            '"session_id": session_id',
            '"room_id": session.get("room_id")',
            '"match_id": session.get("match_id")',
            '"duels": len(envelope["duels"])',
            '"sha256": candidate_sha',
        ):
            with self.subTest(field=field):
                self.assertIn(field, script)
        self.assertIn(
            "EVRP duel count does not match the finished session expectation",
            script,
        )
        self.assertIn("expected_series_duels", script)
        self.assertIn(
            "has a different SHA-256",
            script,
        )
        self.assertIn('allow_replace="${ALLOW_REPLACE_REPLAY:-0}"', script)
        self.assertIn(
            'allow_replace="${ALLOW_REPLACE_REPLAY_PREFIX:-0}"',
            script,
        )
        self.assertIn("replay recovery already applied", script)

    def test_deployment_can_mount_a_verified_replay_prefix(self):
        script = self.deploy_script
        self.assertIn('prefix_candidate="${4:-}"', script)
        self.assertIn(
            'allow_prefix_replace="${ALLOW_REPLACE_REPLAY_PREFIX:-0}"',
            script,
        )
        self.assertIn("def validate_series_replay", (
            SOURCE / "pvp/server.py"
        ).read_text(encoding="utf-8"))
        self.assertIn('f"{session_id}.prefix.evrp"', script)
        self.assertIn('"series_replay_prefix"', script)
        self.assertIn('"initial_generation"', script)
        self.assertIn('"game_generation"', script)
        self.assertIn(
            "prefix duel count must equal the session initial generation "
            "minus one",
            script,
        )
        self.assertIn(
            "existing prefix has a different SHA-256",
            script,
        )
        self.assertIn("stage/prefix.evrp", script)
        self.assertIn("stage/prefix.identity.json", script)
        self.assertEqual(
            script.count(
                'initial_generation = session.get("initial_generation")'
            ),
            2,
        )
        self.assertEqual(
            script.count("initial_generation = duels + 1"),
            2,
        )
        self.assertEqual(
            script.count(
                "game_generation must equal the current game_number"
            ),
            2,
        )
        self.assertIn('session["game_generation"] = game_number', script)
        self.assertNotIn("session.setdefault(\"game_generation\"", script)

    def test_deployment_backfills_historical_pvp_session_replay_index(self):
        script = self.deploy_script
        self.assertIn("sync_target_pvp_sessions()", script)
        self.assertIn(
            'glob.glob("/var/lib/ygo-pvp/s-*.json")',
            script,
        )
        self.assertIn(
            'session.get("room_id") != room_id',
            script,
        )
        self.assertIn(
            'session.get("match_id") != match_id',
            script,
        )
        self.assertIn(
            "completed game {number} must have exactly one replay session",
            script,
        )
        self.assertIn("def verified_replay(metadata, label):", script)
        self.assertIn(
            "stored replay verification failed for {label}",
            script,
        )
        self.assertIn(
            "hashlib.sha256(replay_path.read_bytes()).hexdigest() != digest",
            script,
        )
        self.assertIn('game["session_id"] = session_id', script)
        self.assertIn('match["pvp_sessions"] = desired_entries', script)
        self.assertIn(
            'prefix=".tournament-pvp-index-"',
            script,
        )
        preflight = script.index(
            'sync_target_pvp_sessions check >"$session_index_json"'
        )
        backup = script.index("backup_ready=1", preflight)
        apply = script.index(
            'sync_target_pvp_sessions apply >"$session_index_json"',
            backup,
        )
        self.assertLess(preflight, backup)
        self.assertLess(backup, apply)
        self.assertIn(
            'audit/session-index-preflight.json',
            script,
        )
        self.assertIn(
            '"$session_index_ready" = 1',
            script,
        )

    def test_session_index_preflight_executes_against_legacy_fixture(self):
        script = self.deploy_script
        function_start = script.index("sync_target_pvp_sessions() {")
        python_marker = "python3 - <<'PY'\n"
        code_start = script.index(python_marker, function_start) + len(
            python_marker
        )
        code_end = script.index("\nPY\n}", code_start)
        code = script[code_start:code_end]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tournament_root = root / "ygo-tournament"
            pvp_root = root / "ygo-pvp"
            replay_root = pvp_root / "replays"
            tournament_root.mkdir()
            replay_root.mkdir(parents=True)
            room_id = "r-fixture"
            match_id = "m-final"
            room_path = tournament_root / f"{room_id}.json"
            room_path.write_text(json.dumps({
                "id": room_id,
                "matches": [{
                    "id": match_id,
                    "games": [
                        {"number": 1, "winner": "a"},
                        {"number": 2, "winner": "b"},
                    ],
                    "session": {"pvp_session_id": "s-current"},
                }],
            }), encoding="utf-8")

            for number in (1, 2):
                session_id = f"s-history-{number}"
                payload = b"yrp1" + bytes((number,))
                replay_path = replay_root / f"{session_id}.yrp"
                replay_path.write_bytes(payload)
                (pvp_root / f"{session_id}.json").write_text(json.dumps({
                    "id": session_id,
                    "room_id": room_id,
                    "match_id": match_id,
                    "game_number": number,
                    "status": "finished",
                    "replay": {
                        "path": str(replay_path),
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    },
                }), encoding="utf-8")
            (pvp_root / "s-current.json").write_text(json.dumps({
                "id": "s-current",
                "room_id": room_id,
                "match_id": match_id,
                "game_number": 3,
                "initial_generation": 3,
                "game_generation": 3,
                "status": "waiting",
            }), encoding="utf-8")

            code = code.replace(
                'Path("/var/lib/ygo-tournament")',
                f"Path({str(tournament_root)!r})",
            ).replace(
                'glob.glob("/var/lib/ygo-pvp/s-*.json")',
                f"glob.glob({str(pvp_root / 's-*.json')!r})",
            ).replace(
                'Path("/var/lib/ygo-pvp/replays")',
                f"Path({str(replay_root)!r})",
            )
            output = io.StringIO()
            with patch.dict(os.environ, {
                "TARGET_ROOM": room_id,
                "TARGET_MATCH": match_id,
                "SESSION_INDEX_MODE": "check",
            }), redirect_stdout(output):
                exec(compile(code, "<session-index-preflight>", "exec"), {})

        result = json.loads(output.getvalue())
        self.assertTrue(result["changed"])
        self.assertEqual(result["indexed_sessions"], 3)
        self.assertEqual(
            result["pvp_sessions"],
            [
                {"id": "s-history-1", "start_game": 1},
                {"id": "s-history-2", "start_game": 2},
                {"id": "s-current", "start_game": 3},
            ],
        )
        self.assertEqual(
            result["game_session_ids"],
            {"1": "s-history-1", "2": "s-history-2"},
        )

    def test_recovery_prefix_mode_uses_prefix_metadata_without_registry_contract(self):
        script = self.recovery_script
        self.assertIn('if [[ "${1:-}" == "--prefix" ]]', script)
        self.assertIn(
            'suffix = "prefix.evrp" if mode == "prefix" else "evrp"',
            script,
        )
        self.assertIn(
            'session["series_replay_prefix"] = metadata',
            script,
        )
        registry_block = script[script.index(
            'if [[ "$pvp_state" = "active" && "$mode" = "final" ]]'
        ):]
        self.assertNotIn("series_replay_prefix", registry_block)
        self.assertEqual(
            script.count(
                'if [[ "$pvp_state" = "active" && "$mode" = "final" ]]; then'
            ),
            2,
        )
        self.assertNotIn(
            'if [[ "$pvp_state" = "active" ]]; then\n'
            '    registry="/run/ygo-provisioning/',
            script,
        )
        self.assertEqual(
            script.count(
                'initial_generation = session.get("initial_generation")'
            ),
            2,
        )
        self.assertEqual(
            script.count(
                'initial_generation = len(envelope["duels"]) + 1'
            ),
            2,
        )
        self.assertEqual(
            script.count(
                "game_generation must equal the current game_number"
            ),
            2,
        )
        self.assertIn('session["game_generation"] = game_number', script)
        self.assertNotIn("session.setdefault(\"game_generation\"", script)

    def test_recovery_failure_path_restores_data_and_service_state(self):
        script = self.recovery_script
        precheck = script.index(
            'assert_no_live_connections "pre-maintenance"'
        )
        stop = script.index("sudo -n systemctl stop ygo-tournament", precheck)
        recheck = script.index(
            'assert_no_live_connections "maintenance-window"',
            stop,
        )
        backup = script.index("backup_ready=1", recheck)
        mutation = script.index("mutation_started=1", backup)
        atomic_write = script.index("atomic_bytes(target", mutation)
        self.assertLess(precheck, stop)
        self.assertLess(stop, recheck)
        self.assertLess(recheck, backup)
        self.assertLess(backup, mutation)
        self.assertLess(mutation, atomic_write)
        self.assertIn("trap 'on_error \"$?\"' ERR", script)
        self.assertIn("trap 'on_exit \"$?\"' EXIT", script)
        self.assertIn("restore_data_archive", script)
        self.assertIn("restore_service_states", script)
        self.assertIn("sha256sum -c SHA256SUMS", script)

    def test_native_units_have_one_way_pvp_dependencies(self):
        for name, unit in (
            ("evolution", self.evolution_unit),
            ("tournament", self.tournament_unit),
        ):
            with self.subTest(unit=name):
                self.assertIn("Requires=ygo-pvp.service", unit)
                self.assertIn(
                    "After=network-online.target ygo-pvp.service",
                    unit,
                )
                self.assertIn("PartOf=ygo-pvp.service", unit)
        self.assertNotIn("ygo-evolution.service", self.pvp_unit)
        self.assertNotIn("ygo-tournament.service", self.pvp_unit)


if __name__ == "__main__":
    unittest.main()
