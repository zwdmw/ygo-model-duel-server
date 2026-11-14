#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const zlib = require("node:zlib");

const EVOLUTION_ROOT = process.env.YGO_EVOLUTION_ROOT || "/opt/ygo-evolution";
const {
  createDuelFromYrp,
  createOcgcoreWrapper,
  DirCardReader,
  DirScriptReaderEx,
} = require("koishipro-core.js");
const initSqlJs = require("sql.js");
const { YGOProYrp } = require("ygopro-yrp-encode");
const {
  OcgcoreScriptConstants,
  YGOProMessages,
  YGOProMsgRetry,
  YGOProMsgStart,
  YGOProMsgUpdateCard,
  YGOProMsgUpdateData,
  YGOProMsgWin,
} = require("ygopro-msg-encode");
const { DuelRecord } = require(path.join(
  EVOLUTION_ROOT,
  "dist/src/ygopro/room/domain/DuelRecord.js",
));
const { spectatorView } = require(path.join(
  EVOLUTION_ROOT,
  "dist/src/ygopro/room/domain/SpectatorView.js",
));

function usage() {
  console.error(
    "usage: recover_evrp_from_yrp.js INPUT.yrp OUTPUT.evrp [full_spectator]",
  );
  process.exit(2);
}

function standardResourcePaths() {
  const manifestPath = path.join(EVOLUTION_ROOT, "resources.manifest.json");
  const resourcesRoot = path.join(
    EVOLUTION_ROOT,
    "resources/current/ygopro",
  );
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8"));
  const leaves = manifest?.runtime?.ygopro?.standard;
  if (!Array.isArray(leaves) || leaves.length === 0) {
    throw new Error("resource manifest has no standard YGOPro pool");
  }
  return leaves.map((leaf) => path.join(resourcesRoot, leaf));
}

function replayDate(yrp) {
  const timestamp = Number(yrp.header?.hash);
  if (Number.isFinite(timestamp) && timestamp > 0) {
    return new Date(timestamp * 1000);
  }
  return new Date();
}

const REFRESH_LOCATIONS = [
  OcgcoreScriptConstants.LOCATION_MZONE,
  OcgcoreScriptConstants.LOCATION_SZONE,
  OcgcoreScriptConstants.LOCATION_HAND,
  OcgcoreScriptConstants.LOCATION_GRAVE,
  OcgcoreScriptConstants.LOCATION_REMOVED,
  OcgcoreScriptConstants.LOCATION_EXTRA,
  OcgcoreScriptConstants.LOCATION_DECK,
  OcgcoreScriptConstants.LOCATION_OVERLAY,
  OcgcoreScriptConstants.LOCATION_FZONE,
  OcgcoreScriptConstants.LOCATION_PZONE,
];

function splitRefreshLocations(location) {
  const locations = REFRESH_LOCATIONS.filter(
    (candidate) => (location & candidate) !== 0,
  );
  return locations.length > 0 ? locations : [location];
}

function zoneQueryFlag(location) {
  if (location === OcgcoreScriptConstants.LOCATION_MZONE) return 0x881fff;
  if (location === OcgcoreScriptConstants.LOCATION_SZONE) return 0xe81fff;
  if (location === OcgcoreScriptConstants.LOCATION_HAND) return 0x681fff;
  if (location === OcgcoreScriptConstants.LOCATION_GRAVE) return 0x081fff;
  if (location === OcgcoreScriptConstants.LOCATION_EXTRA) return 0xe81fff;
  return 0xf81fff;
}

function refreshCard(messages, duel, request) {
  for (const location of splitRefreshLocations(request.location)) {
    const queryResult = duel.queryCard({
      player: request.player,
      location,
      sequence: request.sequence,
      queryFlag: 0xf81fff,
      useCache: 0,
    });
    messages.push(new YGOProMsgUpdateCard().fromPartial({
      controller: request.player,
      location,
      sequence: request.sequence,
      card: queryResult.card,
    }));
  }
}

function refreshZone(messages, duel, request) {
  for (const location of splitRefreshLocations(request.location)) {
    const queryResult = duel.queryFieldCard({
      player: request.player,
      location,
      queryFlag: zoneQueryFlag(location),
      useCache: 1,
    });
    messages.push(new YGOProMsgUpdateData().fromPartial({
      player: request.player,
      location,
      cards: queryResult.cards ?? [],
    }));
  }
}

function refreshForMessage(messages, duel, message) {
  const cards = message.getRequireRefreshCards?.() ?? [];
  const zones = message.getRequireRefreshZones?.() ?? [];
  for (const request of cards) refreshCard(messages, duel, request);
  for (const request of zones) refreshZone(messages, duel, request);
}

function appendInitialMessages(messages, duel, yrp) {
  const deck0 = duel.queryFieldCount({
    player: 0,
    location: OcgcoreScriptConstants.LOCATION_DECK,
  });
  const extra0 = duel.queryFieldCount({
    player: 0,
    location: OcgcoreScriptConstants.LOCATION_EXTRA,
  });
  const deck1 = duel.queryFieldCount({
    player: 1,
    location: OcgcoreScriptConstants.LOCATION_DECK,
  });
  const extra1 = duel.queryFieldCount({
    player: 1,
    location: OcgcoreScriptConstants.LOCATION_EXTRA,
  });
  messages.push(new YGOProMsgStart().fromPartial({
    playerType: 0x10,
    duelRule: OcgcoreScriptConstants.MASTER_RULE_2020,
    startLp0: yrp.startLp,
    startLp1: yrp.startLp,
    player0: {
      deckCount: deck0,
      extraCount: extra0,
    },
    player1: {
      deckCount: deck1,
      extraCount: extra1,
    },
  }));
  refreshZone(messages, duel, {
    player: 0,
    location: OcgcoreScriptConstants.LOCATION_EXTRA,
  });
  refreshZone(messages, duel, {
    player: 1,
    location: OcgcoreScriptConstants.LOCATION_EXTRA,
  });
}

async function main() {
  const [input, output, fullSpectatorArg = "true"] = process.argv.slice(2);
  if (!input || !output) usage();
  const fullSpectator = !["0", "false", "no"].includes(
    fullSpectatorArg.toLowerCase(),
  );

  const yrpBytes = new Uint8Array(fs.readFileSync(input));
  const yrp = new YGOProYrp().fromYrp(yrpBytes);
  const resourcePaths = standardResourcePaths();
  const wrapper = await createOcgcoreWrapper();
  const SQL = await initSqlJs();
  const cardReader = await DirCardReader(SQL, ...resourcePaths);
  const scriptReader = await DirScriptReaderEx(...resourcePaths);
  const coreLogs = [];
  wrapper
    .setCardReader(cardReader, true)
    .setScriptReader(scriptReader, true)
    .setMessageHandler((_duel, message, type) => {
      coreLogs.push({ type, message });
    }, true);

  const messages = [];
  let remainingResponses = yrp.responses.length;
  let steps = 0;
  let refreshCards = 0;
  let refreshZones = 2;
  let duel;
  let responses = [];
  try {
    const created = createDuelFromYrp(wrapper, yrp);
    duel = created.duel;
    responses = created.yrp.responses.slice();
    appendInitialMessages(messages, duel, created.yrp);
    for (const result of duel.advance(() => responses.shift())) {
      steps += 1;
      remainingResponses = responses.length;
      const parsed = result.messages
        || (result.raw.length
          ? YGOProMessages.getInstancesFromPayload(result.raw)
          : []);
      for (const message of parsed) {
        if (message instanceof YGOProMsgRetry) {
          throw new Error(`replay retry after ${steps} engine steps`);
        }
        messages.push(message);
        if (message instanceof YGOProMsgWin) {
          continue;
        }
        if (message instanceof YGOProMsgUpdateCard) {
          refreshCards += splitRefreshLocations(message.location).length;
          refreshCard(messages, duel, {
            player: message.controller,
            location: message.location,
            sequence: message.sequence,
          });
          continue;
        }
        if (message instanceof YGOProMsgUpdateData) {
          continue;
        }
        const cards = message.getRequireRefreshCards?.() ?? [];
        const zones = message.getRequireRefreshZones?.() ?? [];
        refreshCards += cards.reduce(
          (total, request) =>
            total + splitRefreshLocations(request.location).length,
          0,
        );
        refreshZones += zones.reduce(
          (total, request) =>
            total + splitRefreshLocations(request.location).length,
          0,
        );
        refreshForMessage(messages, duel, message);
      }
    }
    remainingResponses = responses.length;
  } finally {
    duel?.endDuel();
    cardReader.finalize?.();
    wrapper.finalize();
  }

  if (remainingResponses !== 0) {
    throw new Error(
      `replay ended with ${remainingResponses} unconsumed responses`,
    );
  }
  const win = messages.findLast((message) => message instanceof YGOProMsgWin);
  if (!win) {
    throw new Error("replay produced no terminal win message");
  }

  const players = [
    { name: yrp.hostName || "", deck: yrp.hostDeck },
    { name: yrp.clientName || "", deck: yrp.clientDeck },
  ];
  const record = new DuelRecord(
    [...(yrp.header?.seedSequence || [])],
    players,
    false,
  );
  record.startTime = replayDate(yrp);
  record.endTime = new Date();
  record.winPosition = win.player;
  record.winReason = win.type;
  record.messages = messages;

  const frames = record.toEvrpFrames();
  const spectatorFrames = Array.from(
    record.toPlayback((message) => spectatorView(message, fullSpectator)),
    (message) => Buffer.from(message.toFullPayload()).toString("base64"),
  );
  if (spectatorFrames.length === 0) {
    throw new Error("replay produced no spectator frames");
  }

  const envelope = {
    version: 1,
    meta: {
      players: players.map((player) => ({ name: player.name })),
      hostInfo: {
        start_lp: yrp.startLp,
        start_hand: yrp.startHand,
        draw_count: yrp.drawCount,
        duel_rule: OcgcoreScriptConstants.MASTER_RULE_2020,
      },
      startTime: record.startTime.toISOString(),
      endTime: record.endTime.toISOString(),
      recoveredFromYrp: true,
    },
    duels: [{
      seed: record.seed,
      startTime: record.startTime.toISOString(),
      endTime: record.endTime.toISOString(),
      winPosition: record.winPosition,
      winReason: record.winReason,
      frames,
      spectatorFrames,
    }],
  };
  const archive = zlib.gzipSync(Buffer.from(JSON.stringify(envelope), "utf8"));
  fs.writeFileSync(output, archive, { mode: 0o600 });

  const spectatorIdentifiers = spectatorFrames.map((encoded) => {
    const frame = Buffer.from(encoded, "base64");
    if (
      frame.length < 4
      || frame.readUInt16LE(0) !== frame.length - 2
      || frame[2] !== 1
    ) {
      throw new Error("generated spectator frame is not a native GAME_MSG");
    }
    return frame[3];
  });
  const identifierCounts = {};
  for (const identifier of spectatorIdentifiers) {
    identifierCounts[identifier] = (identifierCounts[identifier] || 0) + 1;
  }
  console.log(JSON.stringify({
    input,
    output,
    bytes: archive.length,
    steps,
    responses: yrp.responses.length,
    responses_remaining: remainingResponses,
    messages: messages.length,
    refresh_cards: refreshCards,
    refresh_zones: refreshZones,
    frames: frames.length,
    spectator_frames: spectatorFrames.length,
    spectator_identifiers: identifierCounts,
    winner: record.winPosition,
    win_reason: record.winReason,
    full_spectator: fullSpectator,
    core_logs: coreLogs.slice(-20),
  }, null, 2));
}

main().catch((error) => {
  console.error(error?.stack || String(error));
  process.exit(1);
});
