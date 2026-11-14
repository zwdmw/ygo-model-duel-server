"use strict";

const fs = require("node:fs");
const path = require("node:path");

const {
    DirScriptReaderEx,
    _OcgcoreConstants,
    createOcgcoreWrapper,
} = require("koishipro-core.js");
const {
    YGOProMsgResponseBase,
    YGOProMsgRetry,
    YGOProMsgWin,
} = require("ygopro-msg-encode");
const { YGOProYrp } = require("ygopro-yrp-encode");
const {
    YGOProResourceLoader,
} = require("../dist/src/ygopro/ygopro/YGOProResourceLoader");

const MAX_PROCESS_CALLS = 100000;
const { OcgcoreScriptConstants } = _OcgcoreConstants;
const RESPONSE_MESSAGE_TYPES = new Set([
    10, 11, 12, 13, 14, 15, 16,
    18, 19, 20, 21, 22, 23, 24, 25, 26,
    132, 140, 141, 142, 143, 144,
]);

function loadDeck(duel, deck, player) {
    for (const code of deck.main || []) {
        duel.newCard({
            code,
            owner: player,
            player,
            location: OcgcoreScriptConstants.LOCATION_DECK,
            sequence: 0,
            position: OcgcoreScriptConstants.POS_FACEDOWN_DEFENSE,
        });
    }
    for (const code of deck.extra || []) {
        duel.newCard({
            code,
            owner: player,
            player,
            location: OcgcoreScriptConstants.LOCATION_EXTRA,
            sequence: 0,
            position: OcgcoreScriptConstants.POS_FACEDOWN_DEFENSE,
        });
    }
}

async function validateReplay(replayPath, pool = "standard") {
    const raw = fs.readFileSync(replayPath);
    const replay = new YGOProYrp().fromYrp(raw);
    if (!replay.header || replay.header.seedSequence.length !== 8) {
        throw new Error("replay does not contain an eight-word seed sequence");
    }
    if (!replay.hostDeck || !replay.clientDeck) {
        throw new Error("replay does not contain two decks");
    }

    const loader = YGOProResourceLoader.get();
    const cardStorage = await loader.getPoolCardStorage(pool);
    const wrapper = await createOcgcoreWrapper({
        wasmBinary: await loader.getOcgcoreWasmBinary(),
    });
    wrapper.setCardReader(cardStorage.toCardReader());
    wrapper.setScriptReader(
        await DirScriptReaderEx(...loader.getPoolScriptPaths(pool)),
    );

    const coreErrors = [];
    wrapper.setMessageHandler((_duel, message, type) => {
        coreErrors.push({ message, type });
    });

    const duel = wrapper.createDuelV2(replay.header.seedSequence);
    let terminal = null;
    let responseIndex = 0;
    let processCalls = 0;
    const processSamples = [];
    try {
        for (let player = 0; player < 2; player += 1) {
            duel.setPlayerInfo({
                player,
                lp: replay.startLp,
                startHand: replay.startHand,
                drawCount: replay.drawCount,
            });
        }
        for (const script of [
            "./script/patches/entry.lua",
            "./script/special.lua",
            "./script/init.lua",
            ...loader.extraScriptPaths,
        ]) {
            duel.preloadScript(script);
        }
        loadDeck(duel, replay.hostDeck, 0);
        loadDeck(duel, replay.clientDeck, 1);
        duel.startDuel(replay.opt);

        while (processCalls < MAX_PROCESS_CALLS) {
            processCalls += 1;
            const result = await duel.process({ noParse: true });
            const messages = result.messages?.length
                ? result.messages
                : result.message
                    ? [result.message]
                    : [];
            const raw = result.raw
                ? Buffer.from(result.raw)
                : Buffer.alloc(0);
            const rawType = raw.length ? raw[0] : null;
            if (processSamples.length < 5) {
                processSamples.push({
                    keys: Object.keys(result),
                    status: result.status,
                    length: result.length,
                    rawBytes: result.raw ? result.raw.length : null,
                    rawType,
                    messageTypes: messages.map((message) => ({
                        constructor: message?.constructor?.name || null,
                        keys: message && typeof message === "object"
                            ? Object.keys(message)
                            : [],
                    })),
                });
            }
            let awaitingResponse = false;
            for (const message of messages) {
                if (message instanceof YGOProMsgRetry) {
                    terminal = {
                        status: "retry",
                        responseIndex: Math.max(0, responseIndex - 1),
                        responseHex: responseIndex
                            ? Buffer.from(
                                replay.responses[responseIndex - 1],
                            ).toString("hex")
                            : null,
                    };
                    break;
                }
                if (message instanceof YGOProMsgWin) {
                    terminal = {
                        status: "win",
                        winner: message.player,
                        reason: message.type,
                    };
                    break;
                }
                if (message instanceof YGOProMsgResponseBase) {
                    if (responseIndex >= replay.responses.length) {
                        terminal = {
                            status: "responses-exhausted",
                            responseIndex,
                        };
                        break;
                    }
                    duel.setResponse(replay.responses[responseIndex]);
                    responseIndex += 1;
                    awaitingResponse = true;
                    break;
                }
            }
            if (!messages.length && rawType === 1) {
                terminal = {
                    status: "retry",
                    responseIndex: Math.max(0, responseIndex - 1),
                    responseHex: responseIndex
                        ? Buffer.from(
                            replay.responses[responseIndex - 1],
                        ).toString("hex")
                        : null,
                };
            } else if (!messages.length && rawType === 5) {
                terminal = {
                    status: "win",
                    winner: raw.length > 1 ? raw[1] : null,
                    reason: raw.length > 2 ? raw[2] : null,
                };
            } else if (
                !messages.length
                && rawType !== null
                && RESPONSE_MESSAGE_TYPES.has(rawType)
            ) {
                if (responseIndex >= replay.responses.length) {
                    terminal = {
                        status: "responses-exhausted",
                        responseIndex,
                    };
                } else {
                    duel.setResponse(replay.responses[responseIndex]);
                    responseIndex += 1;
                    awaitingResponse = true;
                }
            }
            if (terminal) {
                break;
            }
            if (awaitingResponse) {
                continue;
            }
            if (result.status === 2) {
                terminal = { status: "ended-without-win" };
                break;
            }
        }
        if (!terminal) {
            terminal = { status: "process-limit" };
        }
    } finally {
        try {
            if (!duel.ended) {
                duel.endDuel();
            }
        } finally {
            wrapper.finalize();
        }
    }

    return {
        path: path.resolve(replayPath),
        players: [replay.hostName, replay.clientName],
        seedSequence: replay.header.seedSequence,
        responses: replay.responses.length,
        responsesConsumed: responseIndex,
        responsesRemaining: replay.responses.length - responseIndex,
        processCalls,
        processSamples,
        coreErrors,
        ...terminal,
    };
}

async function main() {
    const paths = process.argv.slice(2);
    if (!paths.length) {
        throw new Error("usage: node validate_evolution_yrp.js REPLAY.yrp [...]");
    }
    let failed = false;
    for (const replayPath of paths) {
        const result = await validateReplay(replayPath);
        console.log(JSON.stringify(result));
        if (result.status !== "win" || result.responsesRemaining !== 0) {
            failed = true;
        }
    }
    process.exit(failed ? 1 : 0);
}

main().catch((error) => {
    console.error(error && error.stack ? error.stack : String(error));
    process.exit(1);
});
