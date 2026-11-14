import json
from pathlib import Path
import shutil
import subprocess
import unittest


SOURCE = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "Node.js is required to test the series replay view")
class SeriesReplayViewTest(unittest.TestCase):
    def test_deployment_includes_the_replay_view_dependency(self):
        for script_name, stage in (
            ("aliyun-native-pvp-activate.sh", "$EVOLUTION_PATCH_ROOT"),
            ("aliyun-series-replay.sh", "$stage"),
        ):
            source = (SOURCE / "deploy" / script_name).read_text(encoding="utf-8")
            self.assertIn(f'test -f "{stage}/SeriesReplayView.js"', source)
            self.assertIn("dist/src/ygopro/room/domain/SeriesReplayView.js", source)
            self.assertIn(f'"{stage}/SeriesReplayView.js" \\', source)

    def run_node(self, body):
        script = r"""
const assert = require("node:assert/strict");
const { prepareSeriesReplayView } = require(HELPER);
const names = (a, b) => [{ name: a }, { name: b }];
const start = (type = 0x10) => Buffer.from([3, 0, 1, 4, type]);
const frame = Buffer.from([3, 0, 1, 40, 0]);
const duel = (players, type = 0x10) => ({
  players, winPosition: 1, winReason: 1,
  spectatorFrames: [start(type), frame],
});
const replay = (duels, meta = { players: names("GGBOY", "DuelHeart") }) =>
  ({ meta, duels });
""".replace(
            "HELPER",
            json.dumps(str(SOURCE / "deploy/evolution-patch/SeriesReplayView.js")),
        )
        result = subprocess.run(
            [NODE, "-e", script + "\n" + body],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_uses_first_duel_instead_of_final_lobby_names(self):
        self.run_node(r"""
const input = replay([duel(names("DuelHeart", "GGBOY"))]);
const view = prepareSeriesReplayView(input);
assert.deepEqual(view.players, ["DuelHeart", "GGBOY"]);
assert.equal(view.meta, input.meta);
assert.equal(view.duels[0], input.duels[0]);
""")

    def test_all_twenty_alternating_native_games_keep_correct_names(self):
        self.run_node(r"""
const input = replay(Array.from({length: 20}, (_, index) => index % 2
  ? duel(names("GGBOY", "DuelHeart"), 0x11)
  : duel(names("DuelHeart", "GGBOY"), 0x10)));
const view = prepareSeriesReplayView(input);
assert.deepEqual(view.players, ["DuelHeart", "GGBOY"]);
view.duels.forEach((game, index) => assert.equal(game, input.duels[index]));
""")

    def test_first_game_can_use_reversed_observer_view(self):
        self.run_node(r"""
const view = prepareSeriesReplayView(replay([
  duel(names("GGBOY", "DuelHeart"), 0x11),
  duel(names("DuelHeart", "GGBOY"), 0x10),
]));
assert.deepEqual(view.players, ["DuelHeart", "GGBOY"]);
assert.deepEqual(view.duels.map(game => game.spectatorFrames[0][4]), [0x11, 0x10]);
""")

    def test_recovered_games_rotate_view_without_mutating_recorded_data(self):
        self.run_node(r"""
const input = replay([
  duel(names("DuelHeart", "GGBOY")),
  duel(names("GGBOY", "DuelHeart")),
]);
const snapshots = input.duels.map(game => game.spectatorFrames.map(f => f.toString("hex")));
const view = prepareSeriesReplayView(input);
assert.equal(view.duels[1].spectatorFrames[0][4], 0x11);
assert.equal(input.duels[1].spectatorFrames[0][4], 0x10);
assert.equal(view.duels[1].spectatorFrames[1], frame);
assert.equal(view.duels[1].players, input.duels[1].players);
assert.equal(view.duels[1].winPosition, input.duels[1].winPosition);
assert.equal(view.duels[1].winReason, input.duels[1].winReason);
assert.deepEqual(input.duels.map(game => game.spectatorFrames.map(f => f.toString("hex"))), snapshots);
const differences = Array.from(view.duels[1].spectatorFrames[0])
  .flatMap((byte, index) => byte === input.duels[1].spectatorFrames[0][index] ? [] : [index]);
assert.deepEqual(differences, [4]);
""")

    def test_old_native_metadata_remains_lobby_ordered(self):
        self.run_node(r"""
const input = replay([duel(undefined, 0x11), duel(undefined, 0x10)],
  { players: names("DuelHeart", "GGBOY") });
const view = prepareSeriesReplayView(input);
assert.deepEqual(view.players, ["DuelHeart", "GGBOY"]);
view.duels.forEach((game, index) => assert.equal(game, input.duels[index]));
""")

    def test_single_recovered_replay_uses_core_ordered_metadata(self):
        self.run_node(r"""
const view = prepareSeriesReplayView(replay([duel(undefined, 0x11)],
  { players: names("GGBOY", "DuelHeart"), recoveredFromYrp: true }));
assert.deepEqual(view.players, ["DuelHeart", "GGBOY"]);
""")

    def test_invalid_or_changed_identities_are_rejected(self):
        self.run_node(r"""
for (const players of [[], names("", "GGBOY"), names("DuelHeart", "DuelHeart"), [{name: 1}, {name: "GGBOY"}]]) {
  assert.throws(() => prepareSeriesReplayView(replay([duel(players)])), /identities/);
}
assert.throws(() => prepareSeriesReplayView(replay([
  duel(names("DuelHeart", "GGBOY")), duel(names("DuelHeart", "Other")),
])), /identities change/);
""")

    def test_missing_duplicate_or_nonobserver_start_is_rejected(self):
        self.run_node(r"""
for (const frames of [[frame], [start(), start()], [start(0)], [start(0x12)], [Buffer.from([2, 0, 1, 4])]]) {
  const game = duel(names("DuelHeart", "GGBOY"));
  game.spectatorFrames = frames;
  assert.throws(() => prepareSeriesReplayView(replay([game])), /observer start/);
}
""")

    def test_room_sends_correct_names_and_unchanged_game_frames(self):
        room_path = json.dumps(str(SOURCE / "deploy/evolution-patch/YGOProRoom.js"))
        self.run_node(r"""
const fs = require("node:fs");
const vm = require("node:vm");
const moduleExports = {};
const context = {
  exports: moduleExports, Buffer, setImmediate,
  require(name) {
    if (name.startsWith("node:")) return require(name);
    if (name === "./SeriesReplayView") return { prepareSeriesReplayView };
    if (name.endsWith("/YgoRoom")) return { YgoRoom: class {} };
    if (name === "./RuleMappings") return {
      ruleMappings: {}, formatRuleMappings: {}, priorityRuleMappings: {},
    };
    if (name === "ygopro-msg-encode") return {
      GameMode: { SINGLE: 0, MATCH: 1, TAG: 2 },
      YGOProStocDuelEnd: class { toFullPayload() { return [1, 0, 0x16]; } },
    };
    return {};
  },
};
vm.runInNewContext(fs.readFileSync(ROOM_PATH, "utf8"), context);
const { YGOProRoom } = moduleExports;
(async () => {
  const archive = prepareSeriesReplayView(replay([
    duel(names("DuelHeart", "GGBOY")),
    duel(names("GGBOY", "DuelHeart"), 0x11),
  ]));
  const sent = [];
  const spectator = {
    socket: {closed: false},
    sendMessageToClient(message) { sent.push(message); },
    disconnectGracefully(delay) { assert.equal(delay, 60000); },
  };
  const room = {
    seriesReplay: archive,
    provisioning: {series_finished: true, players: {a: "DuelHeart", b: "GGBOY"}, seat_plan: {a: 1, b: 0}},
    _messageRepository: {
      playerEnterMessage(name, position) { return {name, position}; },
      duelStartMessage() { return "duel_start"; },
      waitingSideMessage() { return "waiting_side"; },
    },
    _logger: {error(message) { throw new Error(message); }},
  };
  await YGOProRoom.prototype.playSeriesReplay.call(room, spectator);
  assert.deepEqual(sent.slice(0, 3), [
    {name: "DuelHeart", position: 0}, {name: "GGBOY", position: 1}, "duel_start",
  ]);
  assert.deepEqual(sent.slice(3, 9), [
    ...archive.duels[0].spectatorFrames, "waiting_side", "duel_start", ...archive.duels[1].spectatorFrames,
  ]);
  const count = sent.length;
  await YGOProRoom.prototype.playSeriesReplay.call(room, spectator);
  assert.equal(sent.length, count);
  await YGOProRoom.prototype.playSeriesReplay.call({...room, provisioning: {series_finished: false}}, {...spectator, seriesReplayStarted: false});
  assert.equal(sent.length, count);
})().catch(error => {console.error(error); process.exitCode = 1;});
""".replace("ROOM_PATH", room_path))


if __name__ == "__main__":
    unittest.main()
