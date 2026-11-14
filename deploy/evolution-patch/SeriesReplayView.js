"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.prepareSeriesReplayView = prepareSeriesReplayView;

function playerNames(players) {
    if (!Array.isArray(players) || players.length !== 2 ||
        players.some((player) => typeof player?.name !== "string" || !player.name) ||
        players[0].name === players[1].name) {
        throw new Error("series replay player identities are invalid");
    }
    return players.map((player) => player.name);
}

function startFrame(duel) {
    const starts = duel.spectatorFrames.filter((frame) =>
        frame.length >= 4 && frame[2] === 0x01 && frame[3] === 0x04);
    if (starts.length !== 1 || starts[0].length < 5 ||
        ![0x10, 0x11].includes(starts[0][4])) {
        throw new Error("series replay observer start is invalid");
    }
    return starts[0];
}

function prepareSeriesReplayView(replay) {
    const first = replay.duels[0];
    const firstStart = startFrame(first);
    const names = playerNames(first.players ?? replay.meta.players);
    // Per-duel names are core ordered; old native metadata is lobby ordered.
    if ((first.players != null || replay.meta.recoveredFromYrp === true) &&
        firstStart[4] === 0x11) {
        names.reverse();
    }
    const duels = replay.duels.map((duel) => {
        const start = startFrame(duel);
        if (duel.players == null) {
            return duel;
        }
        const coreNames = playerNames(duel.players);
        const same = coreNames[0] === names[0] && coreNames[1] === names[1];
        const swapped = coreNames[0] === names[1] && coreNames[1] === names[0];
        if (!same && !swapped) {
            throw new Error("series replay player identities change between duels");
        }
        const playerType = swapped ? 0x11 : 0x10;
        if (start[4] === playerType) {
            return duel;
        }
        // Keep identities stable while queued clients rotate the core's field.
        const viewStart = Buffer.from(start);
        viewStart[4] = playerType;
        return {
            ...duel,
            spectatorFrames: duel.spectatorFrames.map((frame) =>
                frame === start ? viewStart : frame),
        };
    });
    return { ...replay, players: names, duels };
}
