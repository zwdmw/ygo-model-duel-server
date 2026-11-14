"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.OCGCore = void 0;
const yuzuthread_1 = require("yuzuthread");
const ygopro_msg_encode_1 = require("ygopro-msg-encode");
const ocgcore_worker_1 = require("./ocgcore-worker");
const GameMessageMiddleware_1 = require("../middleware/GameMessageMiddleware");
const response_time_utils_1 = require("../utils/response-time-utils");
const TimerState_1 = require("../room/domain/TimerState");
const ygopro_1 = require("../ygopro");
const ygopro_deck_encode_1 = __importDefault(require("ygopro-deck-encode"));
const shuffle_decks_by_seed_1 = require("../utils/shuffle-decks-by-seed");
const generate_seed_1 = require("../utils/generate-seed");
const DuelRecord_1 = require("../room/domain/DuelRecord");
const calculate_ocgcore_deck_1 = require("../utils/calculate-ocgcore-deck");
const isUpdateMessage = (message) => message instanceof ygopro_msg_encode_1.YGOProMsgUpdateData || message instanceof ygopro_msg_encode_1.YGOProMsgUpdateCard;
class OCGCore {
    room;
    logger;
    ocgcore;
    messageSubscription = null;
    // responseSide: the side (0 or 1) currently being prompted by the core
    responseSide = null;
    lastResponseRequestMsg = null;
    _phase = null;
    isRetrying = false;
    _deckReversed = false;
    authoritativeResultObserved = false;
    advanceInFlight = 0;
    advanceIdleWaiters = [];
    // Timer state
    timerState = new TimerState_1.TimerState();
    get hasTimeLimit() {
        return this.room.hostInfo.time_limit > 0;
    }
    // Message middleware (public for external handlers)
    _gameMiddleware = new GameMessageMiddleware_1.GameMessageMiddleware();
    /**
     * Public access to game message middleware
     * External handlers can register to process game messages
     */
    get messageMiddleware() {
        return this._gameMiddleware;
    }
    constructor(room, logger) {
        this.room = room;
        this.logger = logger;
        this.ocgcore = null;
        this.registerMiddlewares();
    }
    /**
     * Register all game message middlewares
     */
    registerMiddlewares() {
        // Log all game messages (except updates)
        // Record messages for replay
        this._gameMiddleware.on(ygopro_msg_encode_1.YGOProMsgBase, (msg) => {
            if (!(msg instanceof ygopro_msg_encode_1.YGOProMsgRetry)) {
                this.room.saveMessageToDuelRecord(msg);
            }
            return msg;
        });
        // Handle new turn - reset timers to full like srvpro2's onNewTurn
        this._gameMiddleware.on(ygopro_msg_encode_1.YGOProMsgNewTurn, (msg) => {
            this.room.increaseTurn();
            if (this.hasTimeLimit) {
                const recoverMs = Math.max(0, this.room.hostInfo.time_limit) * 1000;
                for (const player of [0, 1]) {
                    this.timerState.leftMs[player] = recoverMs;
                    this.timerState.compensatorMs[player] = recoverMs;
                    this.timerState.backedMs[player] = recoverMs;
                }
            }
            return msg;
        });
        // Handle new phase
        this._gameMiddleware.on(ygopro_msg_encode_1.YGOProMsgNewPhase, (msg) => {
            this._phase = msg.phase;
            return msg;
        });
        // Track deck reversal
        this._gameMiddleware.on(ygopro_msg_encode_1.YGOProMsgReverseDeck, (msg) => {
            this._deckReversed = !this._deckReversed;
            return msg;
        });
        // Handle reset time - just return msg
        this._gameMiddleware.on(ygopro_msg_encode_1.YGOProMsgResetTime, (msg) => {
            return msg;
        });
        // Handle retry - just return msg
        this._gameMiddleware.on(ygopro_msg_encode_1.YGOProMsgRetry, (msg) => {
            return msg;
        });
        // Handle win - just return msg
        this._gameMiddleware.on(ygopro_msg_encode_1.YGOProMsgWin, (msg) => {
            return msg;
        });
    }
    async init() {
        const duelRecord = this.generateDuelRecord();
        const extraScriptPaths = [
            "./script/patches/entry.lua",
            "./script/special.lua",
            "./script/init.lua",
            ...ygopro_1.YGOProResourceLoader.get().extraScriptPaths,
        ];
        const loader = ygopro_1.YGOProResourceLoader.get();
        const cardStorage = await loader.getPoolCardStorage(this.room.cardPool);
        const scriptSearchPaths = loader.getPoolScriptPaths(this.room.cardPool);
        const ocgcoreWasmBinary = await loader.getOcgcoreWasmBinary();
        const registry = {
            duel_mode: this.room.duelMode,
            start_lp: String(this.room.hostInfo.start_lp),
            start_hand: String(this.room.hostInfo.start_hand),
            draw_count: String(this.room.hostInfo.draw_count),
            player_type_0: this.room.isPositionSwapped ? "1" : "0",
            player_type_1: this.room.isPositionSwapped ? "0" : "1",
        };
        duelRecord.players.forEach((player, index) => {
            registry[`player_name_${index}`] = player.name;
        });
        const cardReader = await loader.getPoolCardReader(this.room.cardPool);
        const players = duelRecord.toSwappedPlayers();
        const playerDecks = players.map((player) => player.deck);
        // Shuffle after applying the seat swap so a mirrored pair preserves
        // each engine seat's deck order instead of each player's deck order.
        const shuffledDecks = this.room.shuffleDeckEnabled
            ? (0, shuffle_decks_by_seed_1.shuffleDecksBySeed)(playerDecks, duelRecord.seed)
            : playerDecks;
        players.forEach((player, index) => {
            player.deck = shuffledDecks[index];
        });
        const decks = shuffledDecks.map((deck) => (0, calculate_ocgcore_deck_1.calculateOcgcoreDeck)(deck, this.room.hostInfo, cardReader));
        try {
            this.ocgcore = await (0, yuzuthread_1.initWorker)(ocgcore_worker_1.OcgcoreWorker, {
                seed: duelRecord.seed,
                hostinfo: this.room.hostInfo,
                ygoproPaths: scriptSearchPaths,
                extraScriptPaths,
                cardStorage,
                ocgcoreWasmBinary,
                registry,
                decks,
            });
            this.messageSubscription = this.ocgcore.message$.subscribe((msg) => {
                this.logger.info("Received message from OCGCoreWorker");
                this.logger.info({ message: msg.message, type: msg.type });
            });
            return duelRecord;
        }
        catch (error) {
            this.logger.error(error);
            throw error;
        }
    }
    generateDuelRecord() {
        const seed = this.room.provisionedSeed
            ? [...this.room.provisionedSeed]
            : (0, generate_seed_1.generateSeed)();
        const players = this.generatePlayers();
        return new DuelRecord_1.DuelRecord(seed, players, this.room.isPositionSwapped);
    }
    generatePlayers() {
        const sortedPlayers = [...this.room.players].sort((a, b) => a.position - b.position);
        const decks = sortedPlayers.map((_client) => {
            const deck = _client.deck;
            const ygoproDeck = new ygopro_deck_encode_1.default({
                main: deck.main.map((card) => parseInt(card.code, 10)),
                side: deck.side.map((card) => parseInt(card.code, 10)),
                extra: deck.extra.map((card) => parseInt(card.code, 10)),
            });
            return ygoproDeck;
        });
        const players = sortedPlayers.map((_client, index) => ({
            name: _client.name,
            deck: decks[index],
        }));
        return players;
    }
    async queryFieldCount({ team, location }) {
        return this.ocgcore.queryFieldCount({ player: team, location });
    }
    // Public accessors for MercuryDuelingState
    get currentResponseSide() {
        return this.responseSide;
    }
    get timeLimitEnabled() {
        return this.hasTimeLimit;
    }
    get timerLeftMs() {
        return this.timerState.leftMs;
    }
    hasOcgcore() {
        return this.ocgcore !== null;
    }
    get currentLastResponseRequestMsg() {
        return this.lastResponseRequestMsg;
    }
    get isRetryingState() {
        return this.isRetrying;
    }
    /**
     * Simplified: Get the client who should respond.
     * Based on the current response side.
     */
    get responsePlayer() {
        if (this.responseSide === null) {
            return null;
        }
        return this.getActivePlayer(this.responseSide);
    }
    clearResponseTimerState(settlePrevious) {
        this.clearResponseTimer(settlePrevious);
    }
    clearResponseRequestState() {
        this.lastResponseRequestMsg = null;
        this.isRetrying = false;
    }
    // Timer state accessor for TIME_CONFIRM handling
    get timerStateAccessor() {
        return this.timerState;
    }
    // Re-schedule timer after TIME_CONFIRM - sends TIME_LIMIT to client
    async rescheduleTimerAfterConfirm(side) {
        if (!this.hasTimeLimit || ![0, 1].includes(side)) {
            return;
        }
        const originalPos = this.getSideTeam(side);
        const elapsedMs = this.timerState.elapsedMs();
        const runningPos = this.timerState.runningPos;
        if (runningPos === undefined || runningPos !== originalPos) {
            return;
        }
        // Apply time compensation logic from srvpro2
        if (elapsedMs < 10_000 && elapsedMs <= this.timerState.compensatorMs[runningPos]) {
            this.timerState.compensatorMs[runningPos] -= elapsedMs;
        }
        else {
            this.timerState.leftMs[runningPos] = Math.max(0, this.timerState.leftMs[runningPos] - elapsedMs);
        }
        this.timerState.awaitingConfirm = false;
        // Reschedule timer
        await this.setResponseTimer(runningPos, {
            settlePrevious: false,
            awaitingConfirm: false,
        });
    }
    static DISPOSE_TIMEOUT_MS = 60_000;
    async dispose() {
        this.disposeWithTimeout(false);
    }
    disposeWithTimeout(kill) {
        const ocgcore = this.ocgcore;
        if (!ocgcore) {
            return;
        }
        this.ocgcore = null;
        this.messageSubscription?.unsubscribe();
        this.messageSubscription = null;
        this.timerState.clear();
        if (kill) {
            ocgcore.finalize().catch((error) => {
                this.logger.error("Error force finalizing ocgcore", { error });
            });
            return;
        }
        let finished = false;
        const timeout = setTimeout(() => {
            if (finished) {
                return;
            }
            this.logger.warn("OCGCore dispose timed out, forcing finalize");
            ocgcore.finalize().catch((error) => {
                this.logger.error("Error force finalizing ocgcore after timeout", { error });
            });
        }, OCGCore.DISPOSE_TIMEOUT_MS);
        ocgcore
            .dispose()
            .catch((error) => {
            this.logger.error("Error disposing ocgcore", { error });
        })
            .finally(() => {
            finished = true;
            clearTimeout(timeout);
        });
    }
    resetResponseRequestState() {
        const initialTime = this.hasTimeLimit ? Math.max(0, this.room.hostInfo.time_limit) * 1000 : 0;
        this.timerState.reset(initialTime);
        this.lastResponseRequestMsg = null;
        this.isRetrying = false;
    }
    increaseResponseTime(originalDuelPos, gameMsg, response) {
        const maxTimeMs = Math.max(0, this.room.hostInfo.time_limit || 0) * 1000;
        if (!this.hasTimeLimit ||
            ![0, 1].includes(originalDuelPos) ||
            this.timerState.backedMs[originalDuelPos] <= 0 ||
            this.timerState.leftMs[originalDuelPos] >= maxTimeMs ||
            !(0, response_time_utils_1.canIncreaseTime)(gameMsg, response)) {
            return;
        }
        this.timerState.leftMs[originalDuelPos] = Math.min(maxTimeMs, this.timerState.leftMs[originalDuelPos] + 1000);
        this.timerState.compensatorMs[originalDuelPos] += 1000;
        this.timerState.backedMs[originalDuelPos] -= 1000;
    }
    async setResponse(responseBuffer) {
        if (!this.ocgcore) {
            throw new Error("OCGCore not initialized");
        }
        await this.ocgcore.setResponse(responseBuffer);
    }
    async advance() {
        if (!this.canAdvance()) {
            return;
        }
        const ocgcore = this.ocgcore;
        this.advanceInFlight += 1;
        try {
            for await (const advanceResult of ocgcore.advance()) {
                if (!this.canAdvance()) {
                    // Duel ended, stop processing
                    return;
                }
                const terminalResult = await this.handleAdvanceResult(advanceResult);
                if (terminalResult) {
                    return;
                }
            }
        }
        catch (error) {
            await this.handleAdvanceError(error);
        }
        finally {
            this.advanceInFlight -= 1;
            if (this.advanceInFlight === 0) {
                const waiters = this.advanceIdleWaiters.splice(0);
                waiters.forEach((resolve) => resolve());
            }
        }
    }
    async waitForAdvanceIdle() {
        if (this.advanceInFlight === 0) {
            return;
        }
        await new Promise((resolve) => {
            this.advanceIdleWaiters.push(resolve);
        });
    }
    sendStartMessageForReconnect(client) {
        const playerType = this.getIngamePosition(client);
        const startMessage = new ygopro_msg_encode_1.YGOProStocGameMsg().fromPartial({
            msg: new ygopro_msg_encode_1.YGOProMsgStart().fromPartial({
                playerType,
                duelRule: this.room.hostInfo.duel_rule,
                startLp0: this.room.hostInfo.start_lp,
                startLp1: this.room.hostInfo.start_lp,
                player0: {
                    deckCount: 0,
                    extraCount: 0,
                },
                player1: {
                    deckCount: 0,
                    extraCount: 0,
                },
            }),
        });
        client.sendMessageToClient(Buffer.from(startMessage.toFullPayload()));
    }
    sendTurnMessages(client) {
        const rawTurnCount = Math.max(1, this.room.turn || 0);
        if (this.room.isTag) {
            const turnCount = rawTurnCount % 4 || 4;
            for (let index = 0; index < turnCount; index += 1) {
                const message = new ygopro_msg_encode_1.YGOProStocGameMsg().fromPartial({
                    msg: new ygopro_msg_encode_1.YGOProMsgNewTurn().fromPartial({
                        player: index % 2,
                    }),
                });
                client.sendMessageToClient(Buffer.from(message.toFullPayload()));
            }
            return;
        }
        const turnCount = rawTurnCount % 2 === 0 ? 2 : 1;
        for (let index = 0; index < turnCount; index += 1) {
            const message = new ygopro_msg_encode_1.YGOProStocGameMsg().fromPartial({
                msg: new ygopro_msg_encode_1.YGOProMsgNewTurn().fromPartial({
                    player: index,
                }),
            });
            client.sendMessageToClient(Buffer.from(message.toFullPayload()));
        }
    }
    sendPhaseMessage(client) {
        if (this._phase === null) {
            return;
        }
        const message = new ygopro_msg_encode_1.YGOProStocGameMsg().fromPartial({
            msg: new ygopro_msg_encode_1.YGOProMsgNewPhase().fromPartial({
                phase: this._phase,
            }),
        });
        client.sendMessageToClient(Buffer.from(message.toFullPayload()));
    }
    async sendRequestFieldMessage(client) {
        if (!this.ocgcore) {
            return;
        }
        const info = await this.ocgcore.queryFieldInfo();
        const message = new ygopro_msg_encode_1.YGOProStocGameMsg().fromPartial({
            msg: info.field,
        });
        client.sendMessageToClient(Buffer.from(message.toFullPayload()));
    }
    async sendRefreshZonesMessages(client) {
        const queryFlag = 0xefffff;
        const selfIngamePos = this.getIngamePosition(client);
        const opponentIngamePos = 1 - selfIngamePos;
        const locations = [
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_MZONE,
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_SZONE,
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_HAND,
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_GRAVE,
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_EXTRA,
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_REMOVED,
        ];
        const players = [opponentIngamePos, selfIngamePos];
        for (const location of locations) {
            for (const player of players) {
                await this.refreshZones({ player, location }, { queryFlag, sendToClient: client, useCache: 0 });
            }
        }
    }
    async sendDeckReversedAndTopMessages(client) {
        if (!this.ocgcore) {
            return;
        }
        if (this._deckReversed) {
            const msg = new ygopro_msg_encode_1.YGOProMsgReverseDeck();
            client.sendMessageToClient(Buffer.from(new ygopro_msg_encode_1.YGOProStocGameMsg().fromPartial({ msg }).toFullPayload()));
        }
        for (const playerPosition of [0, 1]) {
            await this.sendDeckTopMessage(client, playerPosition);
        }
    }
    async sendDeckTopMessage(client, playerPosition) {
        if (!this.ocgcore) {
            return;
        }
        const DECK_TOP_QUERY_FLAGS = ygopro_msg_encode_1.OcgcoreCommonConstants.QUERY_CODE | ygopro_msg_encode_1.OcgcoreCommonConstants.QUERY_POSITION;
        const deckQuery = await this.ocgcore.queryFieldCard({
            player: playerPosition,
            location: ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_DECK,
            queryFlag: DECK_TOP_QUERY_FLAGS,
            useCache: 0,
        });
        const cards = (deckQuery.cards ?? []);
        const topCard = cards[cards.length - 1];
        if (!topCard) {
            return;
        }
        const isFaceUp = (topCard.position & ygopro_msg_encode_1.OcgcoreCommonConstants.POS_FACEUP) !== 0;
        const shouldSendDeckTop = this._deckReversed || isFaceUp;
        if (!shouldSendDeckTop) {
            return;
        }
        const FACE_UP_CODE_FLAG = 0x80000000;
        const code = isFaceUp ? topCard.code | FACE_UP_CODE_FLAG : topCard.code;
        const msg = new ygopro_msg_encode_1.YGOProMsgDeckTop().fromPartial({
            player: playerPosition,
            sequence: 0,
            code,
        });
        client.sendMessageToClient(Buffer.from(new ygopro_msg_encode_1.YGOProStocGameMsg().fromPartial({ msg }).toFullPayload()));
    }
    async sendReconnectTimeLimitAndResponseState(client) {
        const clientDuelPos = client.position;
        const opponentDuelPos = 1 - clientDuelPos;
        await this.sendTimeLimitMessage(opponentDuelPos, client);
        if (client === this.responsePlayer) {
            await this.sendLastHintToClient(client);
            await this.resendResponseRequestToClient(client);
        }
        else {
            await this.sendWaitingAndTimeLimitToClient(client);
        }
    }
    async sendLastHintToClient(client) {
        const lastHint = this.findLastHintForClient(client);
        if (!lastHint) {
            return;
        }
        const msg = new ygopro_msg_encode_1.YGOProStocGameMsg().fromPartial({ msg: lastHint });
        client.sendMessageToClient(Buffer.from(msg.toFullPayload()));
    }
    async resendResponseRequestToClient(client) {
        if (!this.lastResponseRequestMsg) {
            return;
        }
        const ingamePos = this.getIngamePosition(client);
        const playerView = this.lastResponseRequestMsg.playerView(ingamePos);
        const msg = new ygopro_msg_encode_1.YGOProStocGameMsg().fromPartial({ msg: playerView });
        client.sendMessageToClient(Buffer.from(msg.toFullPayload()));
        await this.setResponseTimer(client.position);
    }
    async sendWaitingAndTimeLimitToClient(client) {
        const waitingMsg = new ygopro_msg_encode_1.YGOProStocGameMsg().fromPartial({
            msg: new ygopro_msg_encode_1.YGOProMsgWaiting(),
        });
        client.sendMessageToClient(Buffer.from(waitingMsg.toFullPayload()));
        await this.sendTimeLimitMessage(client.position, client);
    }
    findLastHintForClient(client) {
        const record = this.room.currentDuelRecord;
        if (!record) {
            return null;
        }
        const clientIngamePos = this.getIngamePosition(client);
        for (let i = record.messages.length - 1; i >= 0; i -= 1) {
            const message = record.messages[i];
            if (!(message instanceof ygopro_msg_encode_1.YGOProMsgHint)) {
                continue;
            }
            try {
                const targets = message.getSendTargets();
                if (targets.includes(clientIngamePos)) {
                    return message.playerView(clientIngamePos);
                }
            }
            catch {
                continue;
            }
        }
        return null;
    }
    canAdvance() {
        return !!this.ocgcore;
    }
    async handleAdvanceResult(advanceResult) {
        const { status, message, encodeError } = advanceResult;
        if (encodeError) {
            this.logger.error("Failed to decode game message in worker transport", {
                encodeError,
                status,
            });
        }
        if (!message) {
            this.logger.info("Received empty message from ocgcore", { status });
            if (status) {
                throw new Error("Cannot continue ocgcore because received empty message with non-advancing status " +
                    status);
            }
            return false;
        }
        const authoritativeResult = message instanceof ygopro_msg_encode_1.YGOProMsgWin;
        if (authoritativeResult) {
            this.authoritativeResultObserved = true;
        }
        // Process message through middleware
        const processedMessage = await this._gameMiddleware.process(message);
        if (processedMessage === null) {
            // Message blocked by middleware
            this.logger.debug("Message blocked by middleware");
            return authoritativeResult;
        }
        if (processedMessage instanceof ygopro_msg_encode_1.YGOProMsgUpdateCard) {
            await this.refreshSingleCard({
                player: processedMessage.controller,
                location: processedMessage.location,
                sequence: processedMessage.sequence,
            });
            return false;
        }
        if (processedMessage instanceof ygopro_msg_encode_1.YGOProMsgWin) {
            return true;
        }
        await this.routeGameMsg(processedMessage);
        return authoritativeResult;
    }
    async dispatchGameMessage(message, options = {}) {
        if (!options?.sendToClient) {
            if (!(message instanceof ygopro_msg_encode_1.YGOProMsgRetry)) {
                this.room.saveMessageToDuelRecord(message);
            }
        }
        if (options?.route && message) {
            await this.routeGameMsg(message, {
                sendToClient: options?.sendToClient,
            });
        }
        return message;
    }
    async handleAdvanceError(error) {
        const isTimeout = ocgcore_worker_1.OcgcoreProcessTimeoutError.is(error);
        if (this.authoritativeResultObserved || this.room.isFinished()) {
            this.logger.warn("Ignoring ocgcore shutdown error after authoritative duel result", {
                error,
                isTimeout,
            });
            return;
        }
        this.logger.error("Error while advancing ocgcore", { error, isTimeout });
        this.broadcastChat("The duel has ended in a draw due to a server error.", ygopro_msg_encode_1.ChatColor.RED);
        const drawGame = new ygopro_msg_encode_1.YGOProMsgWin().fromPartial({
            type: 0x11,
            player: 2,
        });
        this.disposeWithTimeout(isTimeout);
        await this.dispatchGameMessage(drawGame, { route: true });
    }
    broadcastChat(msg, color) {
        const chatMsg = new ygopro_msg_encode_1.YGOProStocChat().fromPartial({
            player_type: color,
            msg,
        });
        const buffer = Buffer.from(chatMsg.toFullPayload());
        for (const client of this.room.clients) {
            client.sendMessageToClient(buffer);
        }
    }
    // ============================================================
    // Route Game Msg - Entry point
    // ============================================================
    async routeGameMsg(message, options = {}) {
        if (!message) {
            return;
        }
        const shouldRefreshFirst = message instanceof ygopro_msg_encode_1.YGOProMsgResponseBase && !isUpdateMessage(message);
        if (shouldRefreshFirst) {
            await this.refreshForMessage(message);
        }
        const sendToClients = options.sendToClient
            ? new Set(Array.isArray(options.sendToClient) ? options.sendToClient : [options.sendToClient])
            : undefined;
        await this.deliverToTargets(message, sendToClients);
        if (!isUpdateMessage(message) && !shouldRefreshFirst) {
            await this.refreshForMessage(message);
        }
        await this.handleResponseOrRetry(message);
    }
    // ============================================================
    // Refresh - Field state updates
    // ============================================================
    async refreshForMessage(message) {
        // Refresh cards that need updating based on message type
        const cardsToRefresh = message.getRequireRefreshCards?.() ?? [];
        const zonesToRefresh = message.getRequireRefreshZones?.() ?? [];
        const refreshCardPromises = cardsToRefresh.map((location) => this.refreshSingleCard({
            player: location.player,
            location: location.location,
            sequence: location.sequence,
        }));
        const refreshZonePromises = zonesToRefresh.map((location) => this.refreshZone(location));
        await Promise.all([...refreshCardPromises, ...refreshZonePromises]);
    }
    async refreshSingleCard(request) {
        if (!this.ocgcore) {
            return;
        }
        const locations = this.splitRefreshLocations(request.location);
        for (const location of locations) {
            const queryResult = await this.ocgcore.queryCard({
                player: request.player,
                location,
                sequence: request.sequence,
                queryFlag: 0xf81fff,
                useCache: 0,
            });
            const cardData = queryResult.card;
            await this.dispatchGameMessage(new ygopro_msg_encode_1.YGOProMsgUpdateCard().fromPartial({
                controller: request.player,
                location,
                sequence: request.sequence,
                card: cardData,
            }), { route: true });
        }
    }
    async refreshZone(request) {
        if (!this.ocgcore) {
            return;
        }
        const locations = this.splitRefreshLocations(request.location);
        for (const location of locations) {
            const queryResult = await this.ocgcore.queryFieldCard({
                player: request.player,
                location,
                queryFlag: this.getZoneQueryFlag(location),
                useCache: 1,
            });
            const cards = queryResult.cards ?? [];
            await this.dispatchGameMessage(new ygopro_msg_encode_1.YGOProMsgUpdateData().fromPartial({
                player: request.player,
                location,
                cards: cards,
            }), { route: true });
        }
    }
    async refreshZones(zone, options = {}) {
        if (!this.ocgcore) {
            return;
        }
        const locations = this.splitRefreshLocations(zone.location);
        for (const location of locations) {
            const { cards } = await this.ocgcore.queryFieldCard({
                player: zone.player,
                location,
                queryFlag: options.queryFlag ?? this.getZoneQueryFlag(location),
                useCache: options.useCache ?? 1,
            });
            const updateDateMessage = new ygopro_msg_encode_1.YGOProMsgUpdateData().fromPartial({
                player: zone.player,
                location,
                cards: cards ?? [],
            });
            await this.dispatchGameMessage(updateDateMessage, {
                sendToClient: options.sendToClient,
                route: true,
            });
        }
    }
    getZoneQueryFlag(location) {
        if (location === ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_MZONE) {
            return 0x881fff;
        }
        if (location === ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_SZONE) {
            return 0xe81fff;
        }
        if (location === ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_HAND) {
            return 0x681fff;
        }
        if (location === ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_GRAVE) {
            return 0x081fff;
        }
        if (location === ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_EXTRA) {
            return 0xe81fff;
        }
        return 0xf81fff;
    }
    splitRefreshLocations(location) {
        const locationFlags = [
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_MZONE,
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_SZONE,
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_HAND,
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_GRAVE,
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_REMOVED,
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_EXTRA,
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_DECK,
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_OVERLAY,
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_FZONE,
            ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_PZONE,
        ];
        const locations = locationFlags.filter((flag) => (location & flag) !== 0);
        return locations.length > 0 ? locations : [location];
    }
    // ============================================================
    // Response Timer - Handle player response timeouts
    // ============================================================
    /**
     * Simplified: Send waiting message to all players except the active one for the given team.
     */
    async sendWaitingToNonOperator(side) {
        const operatingPlayer = this.getActivePlayer(side);
        const nonOperatingPlayers = this.room.players.filter((client) => client !== operatingPlayer);
        const waitingMessage = new ygopro_msg_encode_1.YGOProStocGameMsg().fromPartial({
            msg: new ygopro_msg_encode_1.YGOProMsgWaiting(),
        });
        await Promise.all(nonOperatingPlayers.map((client) => client.sendMessageToClient(Buffer.from(waitingMessage.toFullPayload()))));
    }
    async setResponseTimer(originalDuelPos, options = {}) {
        const { settlePrevious = true, sendTimeLimit = true, awaitingConfirm = true } = options;
        this.clearResponseTimer(settlePrevious);
        if (!this.hasTimeLimit || ![0, 1].includes(originalDuelPos)) {
            return;
        }
        const leftTime = Math.max(0, this.timerState.leftMs[originalDuelPos] || 0);
        if (sendTimeLimit) {
            await this.sendTimeLimitMessage(originalDuelPos);
        }
        if (leftTime <= 0) {
            await this.handleResponseTimeout(originalDuelPos);
            return;
        }
        this.timerState.schedule(originalDuelPos, leftTime, awaitingConfirm, () => {
            void this.handleResponseTimeout(originalDuelPos).catch((error) => {
                this.logger.error("Failed to handle response timeout", { error });
            });
        });
    }
    clearResponseTimer(settleElapsed = false) {
        this.timerState.clear(settleElapsed);
    }
    async sendTimeLimitMessage(originalDuelPos, toSpecificClient) {
        if (!this.hasTimeLimit || ![0, 1].includes(originalDuelPos)) {
            return;
        }
        const leftTime = Math.max(0, this.timerState.leftMs[originalDuelPos] || 0);
        const ingameDuelPos = this.toIngamePosition(originalDuelPos);
        const timeLimitMessage = new ygopro_msg_encode_1.YGOProStocTimeLimit().fromPartial({
            player: ingameDuelPos,
            left_time: Math.ceil(leftTime / 1000),
        });
        const allPlayers = [...this.room.getTeamPlayers(0), ...this.room.getTeamPlayers(1)];
        await Promise.all(allPlayers
            .filter((p) => !toSpecificClient || p === toSpecificClient)
            .map((client) => client.sendMessageToClient(Buffer.from(timeLimitMessage.toFullPayload()))));
    }
    /**
     * Returns the ingame position (side) for a client, considering isPositionSwapped.
     * Team 0 -> Side 0, Team 1 -> Side 1 (by default).
     */
    getIngamePosition(client) {
        const team = client.team;
        return this.getTeamSide(team);
    }
    /**
     * Maps a side (0 or 1) from the core perspective to a room team index (0 or 1).
     */
    getSideTeam(side) {
        return this.room.isPositionSwapped ? 1 - side : side;
    }
    /**
     * Maps a room team index (0 or 1) to a side index (0 or 1) from core perspective.
     */
    getTeamSide(team) {
        return this.room.isPositionSwapped ? 1 - team : team;
    }
    async handleResponseTimeout(originalDuelPos) {
        if (this.timerState.runningPos !== originalDuelPos) {
            return;
        }
        this.clearResponseTimer(false);
        this.timerState.leftMs[originalDuelPos] = 0;
        this.responseSide = null;
        this.logger.info("Response timeout", { originalDuelPos });
        const winnerTeam = 1 - originalDuelPos;
        const winnerEnginePos = this.getTeamSide(winnerTeam);
        const event = "FINISH_DUEL_BY_TIMEOUT";
        const msgWin = new ygopro_msg_encode_1.YGOProMsgWin().fromPartial({
            player: winnerEnginePos,
            type: 0x3,
        });
        this.room.emitRoomEvent(event, msgWin);
    }
    // ============================================================
    // Delivery - Send messages to clients
    // ============================================================
    async deliverToTargets(message, sendToClients) {
        const targetPositions = message.getSendTargets();
        const sendGameMessage = (client, msg) => client.sendMessageToClient(Buffer.from(new ygopro_msg_encode_1.YGOProStocGameMsg().fromPartial({ msg }).toFullPayload()));
        await Promise.all(targetPositions.map(async (position) => {
            if (position === ygopro_msg_encode_1.NetPlayerType.OBSERVER) {
                const observerView = this.room.spectatorView(message);
                const watchers = this.room.spectators;
                await Promise.all(watchers.map((watcher) => sendGameMessage(watcher, observerView)));
            }
            else {
                const players = this.getPlayersAtIngamePosition(position);
                await Promise.all(players.map(async (client) => {
                    if (sendToClients && !sendToClients.has(client)) {
                        return;
                    }
                    const side = this.getIngamePosition(client);
                    const playerView = message.playerView(side);
                    const actor = this.getActivePlayer(side);
                    // Response messages only go to the operating player (srvpro2 parity)
                    if (message instanceof ygopro_msg_encode_1.YGOProMsgResponseBase && client !== actor) {
                        return;
                    }
                    const finalMsg = client === actor ? playerView : playerView.teammateView();
                    await sendGameMessage(client, finalMsg);
                }));
            }
        }));
    }
    // ============================================================
    // Response handling - Response messages and retry logic
    // ============================================================
    async handleResponseOrRetry(message) {
        if (message instanceof ygopro_msg_encode_1.YGOProMsgResponseBase) {
            this.setLastResponseRequestMsg(message);
            await this.sendWaitingToNonOperator(message.responsePlayer());
            await this.setResponseTimer(this.getSideTeam(this.responseSide));
            return;
        }
        if (message instanceof ygopro_msg_encode_1.YGOProMsgRetry && this.responseSide != null) {
            const record = this.room.currentDuelRecord;
            if (record && record.responses.length > 0) {
                record.responses.pop();
            }
            this.isRetrying = true;
            await this.sendWaitingToNonOperator(this.responseSide);
            await this.setResponseTimer(this.getSideTeam(this.responseSide));
            return;
        }
        if (this.responseSide != null &&
            !this.lastResponseRequestMsg &&
            !(message instanceof ygopro_msg_encode_1.YGOProMsgResponseBase)) {
            this.responseSide = null;
        }
    }
    setLastResponseRequestMsg(message) {
        this.lastResponseRequestMsg = message;
        this.isRetrying = false;
        this.responseSide = message.responsePlayer();
    }
    // ============================================================
    // Position helpers - Convert between duel and ingame positions
    // ============================================================
    /**
     * Converts a duel position (0 or 1) to an ingame position.
     * Applies swap logic when positions are swapped (first to play becomes player 2).
     */
    toIngamePosition(duelPosition) {
        if (!this.isValidDuelPosition(duelPosition)) {
            return duelPosition;
        }
        return this.room.isPositionSwapped ? 1 - duelPosition : duelPosition;
    }
    isValidDuelPosition(position) {
        return position === 0 || position === 1;
    }
    // ============================================================
    // Player queries - Get players at specific positions
    // ============================================================
    /**
     * Returns players at given side index (0 or 1 from core perspective).
     * Applies position swap to map engine side to room team.
     */
    getPlayersAtIngamePosition(side) {
        const team = this.getSideTeam(side);
        return this.room.getTeamPlayers(team);
    }
    /**
     * Returns the active player (who must respond) for the given side.
     * Decides which teammate within the team should act based on turn rotation.
     *
     * Formula from srvpro2 (tag_duel.cpp cur_player):
     * - Side 0: idx = floor(max(0, tc - 1) / 2) % 2
     * - Side 1: idx = 1 - (floor(tc / 2) % 2)
     * Where tc = turnCount (starts at 0, incremented at each new turn)
     */
    getActivePlayer(side) {
        const team = this.getSideTeam(side);
        const teamPlayers = this.room.players.filter((p) => p.team === team);
        if (!this.room.isTag || teamPlayers.length <= 1) {
            return teamPlayers[0] ?? null;
        }
        // Use turn count from currentDuel - this.room.turn returns the current turn number
        // It starts at 1 (STARTING_TURN from YgoRoom) and increments on each new turn
        const tc = Math.max(0, this.room.turn || 0);
        // Asymmetric Tag rotation logic from srvpro2:
        let idx;
        if (side === 0) {
            // Team 0: starts with player 0, toggles every 2 turns from turn 3
            // Formula: floor(max(0, tc - 1) / 2) % 2
            idx = Math.floor(Math.max(0, tc - 1) / 2) % 2;
        }
        else {
            // Team 1: starts with player 1, toggles every 2 turns from turn 2
            // Formula: 1 - (floor(tc / 2) % 2)
            idx = 1 - (Math.floor(tc / 2) % 2);
        }
        // Get players sorted by position to match the team player order
        const sortedPlayers = [...teamPlayers].sort((a, b) => a.position - b.position);
        const active = sortedPlayers[idx] ?? sortedPlayers[0] ?? null;
        this.logger.debug("Tag Rotation Debug", {
            turnCount: tc,
            side,
            team,
            idx,
            sortedPositions: sortedPlayers.map((p) => p.position),
            activePlayer: active?.name,
        });
        return active;
    }
}
exports.OCGCore = OCGCore;
//# sourceMappingURL=ocgcore.js.map
