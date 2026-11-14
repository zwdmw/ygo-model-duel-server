"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.YGOProDuelingState = void 0;
const PlayerInfoMessage_1 = require("../../../../edopro/messages/client-to-server/PlayerInfoMessage");
const YGOProRoomState_1 = require("../YGOProRoomState");
const ocgcore_1 = require("../../../ocgcore-worker/ocgcore");
const dependency_injection_1 = require("../../../../shared/dependency-injection");
const EventBus_1 = require("../../../../shared/event-bus/EventBus");
const Commands_1 = require("../../../../shared/messages/Commands");
const DuelEventDecoders_1 = require("../../../../shared/room/domain/duel-events/DuelEventDecoders");
const Team_1 = require("../../../../shared/room/Team");
const ReconnectionTokenIssuer_1 = require("../../../../shared/room/application/reconnect/ReconnectionTokenIssuer");
const ReconnectionAckMessage_1 = require("../../../../shared/messages/server-to-client/ReconnectionAckMessage");
const MatchLifecycleHooks_1 = require("../../../../shared/room/application/lifecycle/MatchLifecycleHooks");
const RoomAnnounce_1 = require("../../../../shared/room/domain/lifecycle/RoomAnnounce");
const config_1 = require("../../../../../src/config");
const YGOProClient_1 = require("../../../client/domain/YGOProClient");
const FinalizeYGOProRoom_1 = require("../../application/FinalizeYGOProRoom");
const resolveJoinerIdentityWithTimeout_1 = require("../resolveJoinerIdentityWithTimeout");
const findMidDuelReconnectingPlayer_1 = require("../../../../shared/room/domain/findMidDuelReconnectingPlayer");
const response_time_utils_1 = require("../../../utils/response-time-utils");
const ygopro_msg_encode_1 = require("ygopro-msg-encode");
const EvrpSerializer_1 = require("../replay/EvrpSerializer");
const GameOverDomainEvent_1 = require("../../../../shared/room/domain/match/domain/domain-events/GameOverDomainEvent");
const WebSocketSingleton_1 = __importDefault(require("../../../../../src/web-socket-server/WebSocketSingleton"));
const SERIES_GAME_TRANSITION_DELAY_MS = 1000;
const SERIES_SURRENDER_TRANSITION_DELAY_MS = 3000;
const SERIES_SURRENDER_GUARD_MS = 250;
const SERIES_PLAYER_RECONNECT_TIMEOUT_MS = 600000;
class YGOProDuelingState extends YGOProRoomState_1.YGOProRoomState {
    room;
    logger;
    eventBus;
    ocgCore;
    pendingSurrenders = new Set();
    endedBySurrender = false;
    constructor(room, eventEmitter, logger) {
        super(eventEmitter);
        this.room = room;
        this.logger = logger;
        this.logger = logger.child({ file: "MercuryDuelingState" });
        this.ocgCore = new ocgcore_1.OCGCore(this.room, this.logger);
        this.handle();
        this.eventBus = dependency_injection_1.container.get(EventBus_1.EventBus);
        this.eventEmitter.on("JOIN", (message, room, socket) => void this.handleJoin.bind(this)(message, room, socket));
        this.eventEmitter.on(Commands_1.Commands.UPDATE_DECK, (message, room, socket) => void this.handleUpdateDeck.bind(this)(message, room, socket));
        this.eventEmitter.on(Commands_1.Commands.RESPONSE, (message, room, client) => void this.handleResponse.bind(this)(message, room, client));
        this.eventEmitter.on(Commands_1.Commands.TIME_CONFIRM, (message, room, client) => void this.handleTimeConfirm.bind(this)(message, room, client));
        this.eventEmitter.on("FINISH_DUEL_BY_TIMEOUT", (winMsg) => void this.handleWinCondition.bind(this)(winMsg));
        this.eventEmitter.on(Commands_1.Commands.SURRENDER, (_message, _room, client) => void this.handleSurrender.bind(this)(client));
        this.eventEmitter.on("EXPRESS_RECONNECT", (message, room, socket) => void this.handleExpressReconnect.bind(this)(message, room, socket));
    }
    async handle() {
        this.logger.info("handle");
        const duelRecord = await this.ocgCore.init();
        if (Number.isInteger(this.room.seriesGameNumber) &&
            this.room.seriesGameNumber > 0) {
            duelRecord.gameNumber = this.room.seriesGameNumber;
        }
        this.room.addDuelRecord(duelRecord);
        this.ocgCore.resetResponseRequestState();
        this.ocgCore.messageMiddleware.on(ygopro_msg_encode_1.YGOProMsgWin, (msg) => {
            this.logger.info(`Winner: player=${msg.player}, type=${msg.type}`);
            // Clients receive MSG_WIN early in this sequence; if anything after
            // that throws, they would wait forever for CHANGE_SIDE/DUEL_END.
            // Fail towards closing the room so nobody is stranded on WinScreen.
            this.handleWinCondition(msg).catch(async (error) => {
                this.logger.error(`handleWinCondition failed: ${String(error)}`);
                await this.ocgCore.waitForAdvanceIdle();
                this.disposeCore();
                try {
                    this.sendDuelEndAndDisconnect();
                }
                catch (sendError) {
                    this.logger.error(`sendDuelEndAndDisconnect failed: ${String(sendError)}`);
                }
                this.removeRoom();
            });
            return msg;
        }, 100);
        this.ocgCore.messageMiddleware.on(ygopro_msg_encode_1.YGOProMsgNewTurn, (msg) => {
            if (this.room.isTag && (msg.player & 0x2) === 0) {
                this.pendingSurrenders.clear();
            }
            this.publishTurnStart(msg.player);
            this.broadcastRoomUpdate();
            return msg;
        });
        this.ocgCore.messageMiddleware.on(ygopro_msg_encode_1.YGOProMsgDamage, (msg) => {
            const team = this.resolveTeam(msg.player);
            this.room.decreaseLps(team, msg.value);
            this.publishLpEvent("duel.damage", msg.player, msg.value);
            this.broadcastRoomUpdate();
            return msg;
        });
        this.ocgCore.messageMiddleware.on(ygopro_msg_encode_1.YGOProMsgRecover, (msg) => {
            const team = this.resolveTeam(msg.player);
            this.room.increaseLps(team, msg.value);
            this.publishLpEvent("duel.recover", msg.player, msg.value);
            this.broadcastRoomUpdate();
            return msg;
        });
        this.ocgCore.messageMiddleware.on(ygopro_msg_encode_1.YGOProMsgPayLpCost, (msg) => {
            const team = this.resolveTeam(msg.player);
            this.room.decreaseLps(team, msg.cost);
            this.publishLpEvent("duel.lp-cost", msg.player, msg.cost);
            this.broadcastRoomUpdate();
            return msg;
        });
        const [player0DeckCount, player0ExtraCount, player1DeckCount, player1ExtraCount] = await Promise.all([
            this.ocgCore.queryFieldCount({
                team: Team_1.Team.PLAYER,
                location: ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_DECK,
            }),
            this.ocgCore.queryFieldCount({
                team: Team_1.Team.PLAYER,
                location: ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_EXTRA,
            }),
            this.ocgCore.queryFieldCount({
                team: Team_1.Team.OPPONENT,
                location: ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_DECK,
            }),
            this.ocgCore.queryFieldCount({
                team: Team_1.Team.OPPONENT,
                location: ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_EXTRA,
            }),
        ]);
        if (this.room.provisioning?.continuous_series === true &&
            this.room.seriesGameNumber > 1) {
            // Reset the client only after the new core is ready. Keeping DUEL_START
            // adjacent to MSG_START prevents MDPro3 from displaying a temporary
            // 0-LP board while the next game's OCGCore is still initializing.
            this.room.clients.forEach((client) => {
                client.sendMessageToClient(this.room.messageSender.duelStartMessage());
            });
        }
        const createStartMsg = (playerType) => new ygopro_msg_encode_1.YGOProStocGameMsg().fromPartial({
            msg: new ygopro_msg_encode_1.YGOProMsgStart().fromPartial({
                playerType,
                duelRule: this.room.hostInfo.duel_rule,
                startLp0: this.room.hostInfo.start_lp,
                startLp1: this.room.hostInfo.start_lp,
                player0: {
                    deckCount: player0DeckCount,
                    extraCount: player0ExtraCount,
                },
                player1: {
                    deckCount: player1DeckCount,
                    extraCount: player1ExtraCount,
                },
            }),
        });
        const side0Players = this.ocgCore.getPlayersAtIngamePosition(0);
        const side1Players = this.ocgCore.getPlayersAtIngamePosition(1);
        side0Players.forEach((p) => p.sendMessageToClient(Buffer.from(createStartMsg(0).toFullPayload())));
        side1Players.forEach((p) => p.sendMessageToClient(Buffer.from(createStartMsg(1).toFullPayload())));
        const watcherType = this.room.isPositionSwapped ? 0x11 : 0x10;
        const watcherStartMessage = createStartMsg(watcherType);
        const spectators = this.room.spectators;
        spectators.forEach((spectator) => {
            spectator.sendMessageToClient(Buffer.from(watcherStartMessage.toFullPayload()));
        });
        this.room.saveMessageToDuelRecord(watcherStartMessage.msg);
        this.ocgCore.refreshZones({
            player: Team_1.Team.PLAYER,
            location: ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_EXTRA,
        });
        this.ocgCore.refreshZones({
            player: Team_1.Team.OPPONENT,
            location: ygopro_msg_encode_1.OcgcoreScriptConstants.LOCATION_EXTRA,
        });
        this.notifyDuelStart(this.room);
        this.ocgCore.advance();
    }
    async handleJoin(message, room, socket) {
        this.logger.info("handleJoin");
        // Reservation gate: past WAITING, a reserved room keeps its seats closed
        // to third parties — the join string alone never buys a seat. The gate
        // admits watch-stamped sockets (spectate-only by construction) alongside
        // a reserved player's own reconnect (its ticket-resolved identity is in
        // the reservation set); both proceed below unchanged.
        if (!room.reservationAdmits(socket)) {
            room.rejectReservedJoin(socket);
            return;
        }
        const playerInfoMessage = new PlayerInfoMessage_1.PlayerInfoMessage(message.previousMessage, message.data.length);
        // The identity resolution awaits the database, yielding the event loop,
        // so the eligibility check and the seat change must run as ONE exclusive
        // section — otherwise two concurrent joins could both observe the seat
        // as reclaimable before either takes it. Same lock (and same
        // hold-across-resolver idiom) as the WAITING admission.
        await room.mutex.runExclusive(async () => {
            // A ranked seat is bound to an account id, so the joiner's identity is
            // resolved BEFORE the reconnect decision — the display name alone never
            // identifies anyone. Casual rooms bind by address and liveness instead
            // and never consult the resolver.
            let joinerUserId = null;
            if (room.ranked) {
                try {
                    joinerUserId = await (0, resolveJoinerIdentityWithTimeout_1.resolveJoinerIdentityWithTimeout)(room, socket, playerInfoMessage);
                }
                catch (error) {
                    // Fail closed: with the resolver unreachable — erroring, or hung
                    // past the bounded timeout — nobody's identity is proven, so no
                    // seat may change hands. The joiner waits in the stands (a legit
                    // player can simply rejoin once the resolver recovers) instead of
                    // hanging with no response. This catch keeps the resolver failure
                    // off the voided JOIN promise; a later synchronous throw inside
                    // this exclusive section still surfaces to the global handler
                    // (the process survives it and the mutex releases).
                    this.logger.error(`resolveJoinerIdentity failed: ${String(error)}`);
                    this.admitAsSpectator(room, socket, playerInfoMessage.name);
                    return;
                }
            }
            // The shared seam honors the watch stamp: a "w,<roomId>" joiner asked to
            // spectate, never to take a seat, so a name match must not reconnect it.
            const playerAlreadyInRoom = (0, findMidDuelReconnectingPlayer_1.findMidDuelReconnectingPlayer)({
                players: room.players,
                name: playerInfoMessage.name,
                socket,
                roomId: room.id,
                ranked: room.ranked,
                joinerUserId,
            });
            if (!(playerAlreadyInRoom instanceof YGOProClient_1.YGOProClient)) {
                this.admitAsSpectator(room, socket, playerInfoMessage.name);
                return;
            }
            this.room.reconnect(playerAlreadyInRoom, socket);
        });
    }
    admitAsSpectator(room, socket, name) {
        const spectator = room.createSpectatorUnsafe(socket, name);
        room.addSpectatorUnsafe(spectator);
        spectator.sendMessageToClient(Buffer.from(new ygopro_msg_encode_1.YGOProStocDuelStart().toFullPayload()));
        room.sendPreviousDuelsHistoricalMessages(spectator);
        room.sendCurrentDuelHistoricalMessages(spectator);
    }
    async handleUpdateDeck(message, _room, player) {
        player.logger.info("handleUpdateDeck");
        if (!player.isReconnecting || !player.deck) {
            return;
        }
        const updateDeckMessage = new ygopro_msg_encode_1.YGOProCtosUpdateDeck().fromPayload(message.data);
        if (!player.deck.isSideDeckValid(updateDeckMessage.deck.main, updateDeckMessage.deck.side)) {
            const status = (player.position << 4) | 0x0a;
            player.sendMessageToClient(this.room.messageSender.playerChangeMessage(player.position, status));
            return;
        }
        if (this.room.isSeriesTransitionInProgress()) {
            player.clearReconnecting();
            return;
        }
        await this.resyncBoard(player);
        player.clearReconnecting();
    }
    // Re-sends the full in-duel board state to a (re)connecting player. Reused by
    // both the deck-resubmission reconnect (handleUpdateDeck) and the token-based
    // express reconnect (handleExpressReconnect). The board re-sync is OCGCore
    // specific and therefore intentionally NOT part of the shared reconnect layer.
    async resyncBoard(player) {
        player.sendMessageToClient(Buffer.from(new ygopro_msg_encode_1.YGOProStocDuelStart().toFullPayload()));
        this.ocgCore.sendStartMessageForReconnect(player);
        this.ocgCore.sendTurnMessages(player);
        this.ocgCore.sendPhaseMessage(player);
        await this.ocgCore.sendRequestFieldMessage(player);
        await this.ocgCore.sendRefreshZonesMessages(player);
        await this.ocgCore.sendDeckReversedAndTopMessages(player);
        await this.ocgCore.sendReconnectTimeLimitAndResponseState(player);
    }
    async handleExpressReconnect(message, room, socket) {
        this.logger.info("EXPRESS_RECONNECT");
        const token = message.data.toString("utf8");
        const player = ReconnectionTokenIssuer_1.ReconnectionTokenIssuer.resolve(token, room.id, (client) => client instanceof YGOProClient_1.YGOProClient);
        if (!player) {
            this.logger.info(`EXPRESS_RECONNECT: no player for token ${token}`);
            socket.send(ReconnectionAckMessage_1.ReconnectionAckMessage.failure());
            socket.destroy();
            return;
        }
        // 1. Acknowledge success on the freshly reopened socket.
        socket.send(ReconnectionAckMessage_1.ReconnectionAckMessage.success());
        // 2. Re-associate the socket and replay the lobby/init frames (room.reconnect
        //    mirrors the name-match reconnect path for ygopro).
        room.reconnect(player, socket);
        if (room.isSeriesTransitionInProgress()) {
            player.sendMessageToClient(ReconnectionTokenIssuer_1.ReconnectionTokenIssuer.rotate(player, room.id));
            player.clearReconnecting();
            return;
        }
        // 3. Replay the live board state.
        await this.resyncBoard(player);
        // 4. Rotate the token (single-use) and finish.
        player.sendMessageToClient(ReconnectionTokenIssuer_1.ReconnectionTokenIssuer.rotate(player, room.id));
        player.clearReconnecting();
    }
    async handleResponse(message, room, player) {
        player.logger.info("handleResponse");
        if (this.ocgCore.currentResponseSide === null ||
            player !== this.ocgCore.responsePlayer ||
            !this.ocgCore.hasOcgcore()) {
            return;
        }
        const responseSide = this.ocgCore.currentResponseSide;
        const responseTeam = this.ocgCore.getSideTeam(responseSide);
        const responseRequestMsg = this.ocgCore.currentLastResponseRequestMsg;
        const responseBuffer = Buffer.from(message.data);
        room.currentDuelRecord?.responses.push(responseBuffer);
        // Handle time limit compensation
        if (this.ocgCore.timeLimitEnabled) {
            this.ocgCore.clearResponseTimerState(true);
            const msgType = this.ocgCore.isRetryingState
                ? 0x02 // MSG_RETRY
                : responseRequestMsg
                    ? (0, response_time_utils_1.getMessageIdentifier)(responseRequestMsg)
                    : 0;
            this.ocgCore.increaseResponseTime(responseTeam, msgType, responseBuffer);
        }
        // Clear response request state (NOT responsePosition)
        this.ocgCore.clearResponseRequestState();
        // Send response to OCGCore and advance
        try {
            await this.ocgCore.setResponse(responseBuffer);
        }
        catch (error) {
            player.logger.error("Failed to set response in ocgcore", { error });
            room.setDuelFinished();
            return;
        }
        await this.ocgCore.advance();
    }
    async handleTimeConfirm(_message, _room, player) {
        player.logger.info("handleTimeConfirm");
        // Check if time limit is enabled
        if (!this.ocgCore.timeLimitEnabled) {
            return;
        }
        // Check if there's an active response request
        const responseSide = this.ocgCore.currentResponseSide;
        if (responseSide === null) {
            return;
        }
        const timerState = this.ocgCore.timerStateAccessor;
        // Check if timer is running and waiting for confirm
        if (timerState.runningPos === null || !timerState.awaitingConfirm) {
            return;
        }
        // Check if the player responding is the one who sent TIME_CONFIRM
        if (timerState.runningPos !== this.ocgCore.getSideTeam(responseSide)) {
            return;
        }
        // Verify the player is the one who should respond
        const responsePlayer = this.ocgCore.responsePlayer;
        if (!responsePlayer || player !== responsePlayer) {
            return;
        }
        // Handle TIME_CONFIRM - reschedule timer and send TIME_LIMIT to client
        await this.ocgCore.rescheduleTimerAfterConfirm(responseSide);
        // Continue the duel after confirming time
    }
    async handleSurrender(client) {
        if (client.isSpectator) {
            return;
        }
        if (this.room.isSeriesSurrenderSuppressed()) {
            this.logger.info(`Ignored stale series surrender by ${client.name} (position=${client.position})`);
            return;
        }
        this.logger.info(`Surrender by ${client.name} (position=${client.position})`);
        if (!this.room.isTag) {
            await this.executeSurrender(client);
            return;
        }
        const teammate = this.getTeammate(client);
        if (!teammate || teammate.isReconnecting) {
            this.pendingSurrenders.clear();
            await this.executeSurrender(client);
            return;
        }
        if (this.pendingSurrenders.has(client.position)) {
            return;
        }
        if (this.pendingSurrenders.has(teammate.position)) {
            this.pendingSurrenders.clear();
            await this.executeSurrender(client);
            return;
        }
        this.pendingSurrenders.add(client.position);
        const surrenderMsg = Buffer.from(new ygopro_msg_encode_1.YGOProStocTeammateSurrender().toFullPayload());
        client.sendMessageToClient(surrenderMsg);
        teammate.sendMessageToClient(surrenderMsg);
    }
    async executeSurrender(client) {
        this.endedBySurrender = true;
        const winnerEnginePos = 1 - this.ocgCore.getIngamePosition(client);
        const winMsg = new ygopro_msg_encode_1.YGOProMsgWin().fromPartial({
            player: winnerEnginePos,
            type: 0x0,
        });
        await this.handleWinCondition(winMsg);
    }
    getTeammate(client) {
        return this.room.players.find((p) => p.team === client.team && p !== client);
    }
    async handleWinCondition(winMsg) {
        const winner = this.ocgCore.toIngamePosition(winMsg.player);
        this.logger.info(`handleWinCondition: player=${winMsg.player}, type=${winMsg.type}, winner=${winner}`);
        if (this.room.isFinished()) {
            return;
        }
        await this.processDuelEnd(winMsg, winner);
        await this.determineNextPhase(winner);
    }
    async processDuelEnd(winMsg, winner) {
        this.room.finished();
        this.room.duelWinner(winner);
        this.room.players.forEach((client) => client.notReady());
        this.broadcastWinMessage(winMsg);
        this.updateDuelRecord(winner, winMsg.type);
    }
    broadcastWinMessage(winMsg) {
        this.room.clients.forEach((client) => {
            client.sendMessageToClient(this.room.messageSender.winMessage(winMsg.player, winMsg.type));
        });
    }
    updateDuelRecord(winner, winType) {
        const duelRecord = this.room.currentDuelRecord;
        if (!duelRecord)
            return;
        duelRecord.winPosition = winner;
        duelRecord.winReason = winType;
        duelRecord.endTime = new Date();
    }
    async determineNextPhase(winner) {
        if (this.room.provisioning?.continuous_series) {
            this.room.beginSeriesTransition();
            // Move clients off the WinScreen immediately. Waiting for the
            // tournament callback first can leave MDPro3/YGOPro2 on the single-
            // duel result screen long enough for them to close the room socket.
            this.room.players.forEach((client) => {
                client.sendMessageToClient(this.room.messageSender.changeSideMessage());
            });
            this.room.spectators.forEach((client) => {
                client.sendMessageToClient(this.room.messageSender.waitingSideMessage());
            });
            // MDPro3 closes its local surrender confirmation asynchronously.
            // A surrender needs a longer transition, then a short server-side
            // guard prevents a delayed old CTOS_SURRENDER from ending game two.
            const transitionDelayMs = this.endedBySurrender
                ? SERIES_SURRENDER_TRANSITION_DELAY_MS
                : SERIES_GAME_TRANSITION_DELAY_MS;
            if (this.endedBySurrender) {
                this.room.suppressSeriesSurrendersFor(transitionDelayMs + SERIES_SURRENDER_GUARD_MS);
            }
            await new Promise((resolve) => setTimeout(resolve, transitionDelayMs));
            if (this.endedBySurrender) {
                this.room.suppressSeriesSurrendersFor(SERIES_SURRENDER_GUARD_MS);
            }
            const moreGames = await this.room.waitForNextSeriesGame();
            if (!moreGames) {
                await dependency_injection_1.container.get(MatchLifecycleHooks_1.MatchLifecycleHooks).runEnding(this.buildEndContext());
                await this.finalizeWithReplays();
                this.dispatchGameOverDomainEvent();
                this.removeRoom();
                return;
            }
            await this.ocgCore.waitForAdvanceIdle();
            this.disposeCore();
            this.pendingSurrenders.clear();
            await this.room.waitForSeriesPlayersConnected(SERIES_PLAYER_RECONNECT_TIMEOUT_MS);
            this.room.finishSeriesTransition();
            // The server owns the fixed decks and turn order. Reuse them without
            // requiring a side-deck submission or a loser-chooses prompt.
            this.room.dueling();
            return;
        }
        const score = this.room.matchScore();
        this.logger.info(`Score: team0=${score.team0}, team1=${score.team1}, bestOf=${this.room.bestOf}, isWinMatch=${this.room.isMatchFinished()}`);
        if (this.room.isMatchFinished()) {
            await dependency_injection_1.container.get(MatchLifecycleHooks_1.MatchLifecycleHooks).runEnding(this.buildEndContext());
            await this.finalizeWithReplays();
            this.dispatchGameOverDomainEvent();
            this.removeRoom();
            return;
        }
        await this.transitionToSideDecking(winner);
    }
    buildEndContext() {
        return {
            roomId: this.room.id,
            matchId: this.room.matchId,
            ranked: this.room.ranked,
            banListName: this.room.banListName ?? "N/A",
            season: config_1.config.season,
            players: this.room.matchPlayersHistory.map((player) => ({
                id: player.id,
                team: player.team,
                name: player.name,
                winner: player.winner,
            })),
            announce: (0, RoomAnnounce_1.createRoomAnnounce)(this.room),
        };
    }
    async transitionToSideDecking(winner) {
        await this.ocgCore.waitForAdvanceIdle();
        this.disposeCore();
        this.room.sideDecking();
        this.room.players.forEach((client) => {
            client.sendMessageToClient(this.room.messageSender.changeSideMessage());
        });
        this.room.spectators.forEach((client) => {
            client.sendMessageToClient(this.room.messageSender.waitingSideMessage());
        });
        this.assignSideDeckChoice(winner);
    }
    assignSideDeckChoice(winner) {
        // A DRAW (ingame position 2, neither team) has no loser to hand the turn
        // choice to. KDE Tournament Policy §IV.F: "In the case of a Duel ending in
        // a Draw, another random method should be employed to choose the deciding
        // Duelist" — flag the room so side-decking re-enters RPS.
        if (winner !== Team_1.Team.PLAYER && winner !== Team_1.Team.OPPONENT) {
            this.room.turnChoiceRequiresRps = true;
            return;
        }
        this.room.turnChoiceRequiresRps = false;
        const looser = this.room.players.find((client) => {
            const isLoserTeam = winner === Team_1.Team.PLAYER ? client.team === Team_1.Team.OPPONENT : client.team === Team_1.Team.PLAYER;
            const positionMod = winner === Team_1.Team.PLAYER
                ? client.position % this.room.team1
                : client.position % this.room.team0;
            return positionMod === Team_1.Team.PLAYER && isLoserTeam;
        });
        if (looser) {
            this.room.setClientWhoChoosesTurn(looser);
        }
    }
    async finalizeWithReplays() {
        this.logger.info("Finalizing match and sending replays");
        this.removeAllListener();
        await this.ocgCore.waitForAdvanceIdle();
        this.disposeCore();
        await this.sendAllReplays();
        await this.sendAllEvrp();
        this.sendDuelEndAndDisconnect();
    }
    /**
     * Public so YGOProRoom.setDuelFinished() can dispose THIS state's OCGCore
     * before delegating to waiting() on the ocgcore-error recovery path
     * (handleResponse's catch, above). Every other exit from dueling disposes
     * before transitioning (see finalizeWithReplays and
     * transitionToSideDecking) — this path must too, or the core leaks.
     */
    disposeCore() {
        if (this.ocgCore.hasOcgcore()) {
            this.ocgCore.dispose();
            this.logger.info("OCGCore disposed");
        }
    }
    async sendAllReplays() {
        const duelRecords = this.room.duelRecords;
        for (let i = 0; i < duelRecords.length; i++) {
            const replayBuffer = this.generateReplayBuffer(i, duelRecords[i]);
            if (!replayBuffer)
                continue;
            this.broadcastReplay(i + 1, duelRecords.length, replayBuffer);
        }
    }
    /**
     * Serialize all duel records into the .evrp envelope and broadcast the
     * chunked frames to every connected client.
     *
     * Called AFTER sendAllReplays() so .yrp delivery is never interrupted (R2).
     * Any serialization or send failure is caught and logged; it MUST NOT
     * propagate to the caller (R2 failure isolation).
     */
    async sendAllEvrp() {
        try {
            const gz = EvrpSerializer_1.EvrpSerializer.serialize(this.room, this.room.duelRecords);
            const frames = EvrpSerializer_1.EvrpSerializer.toFrames(gz);
            this.logger.info(`Sending EVRP export: ${frames.length} chunk(s), ~${gz.length} bytes gzip`);
            for (const frame of frames) {
                this.room.clients.forEach((client) => {
                    client.sendMessageToClient(frame);
                });
            }
        }
        catch (err) {
            this.logger.error(String(err), { context: "sendAllEvrp" });
        }
    }
    generateReplayBuffer(index, duelRecord) {
        try {
            const replayData = duelRecord.toYrp(this.room);
            const replayMsg = new ygopro_msg_encode_1.YGOProStocReplay().fromPartial({
                replay: replayData,
            });
            return Buffer.from(replayMsg.toFullPayload());
        }
        catch (err) {
            this.logger.error(String(err), { duelIndex: index });
            return null;
        }
    }
    broadcastReplay(index, total, replayBuffer) {
        const hintMsg = new ygopro_msg_encode_1.YGOProStocChat().fromPartial({
            player_type: ygopro_msg_encode_1.ChatColor.LIGHTBLUE,
            msg: `#{replay_hint_part1}${index}#{replay_hint_part2}`,
        });
        const hintBuffer = Buffer.from(hintMsg.toFullPayload());
        this.room.clients.forEach((client) => {
            client.sendMessageToClient(hintBuffer);
            client.sendMessageToClient(replayBuffer);
        });
        this.logger.info(`Sent replay ${index}/${total} (${replayBuffer.length} bytes)`);
    }
    sendDuelEndAndDisconnect() {
        const duelEndBuffer = Buffer.from(new ygopro_msg_encode_1.YGOProStocDuelEnd().toFullPayload());
        this.room.clients.forEach((client) => {
            client.sendMessageToClient(duelEndBuffer);
            client.disconnectGracefully();
        });
        this.logger.info("Duel end sent and all clients closing gracefully");
    }
    dispatchGameOverDomainEvent() {
        void this.eventBus.publish(GameOverDomainEvent_1.GameOverDomainEvent.DOMAIN_EVENT, new GameOverDomainEvent_1.GameOverDomainEvent({
            roomId: this.room.id,
            matchId: this.room.matchId,
            duelIds: [...this.room.duelIds],
            bestOf: this.room.bestOf,
            players: this.room.matchPlayersHistory,
            date: new Date(),
            banListHash: this.room.edoBanListHash,
            banListName: this.room.banListName ?? "N/A",
            ranked: this.room.ranked,
        }));
    }
    duelEventContext() {
        return {
            roomId: this.room.id,
            duelId: this.room.duelId,
            turn: this.room.turn,
            resolveTeam: (player) => this.resolveTeam(player),
        };
    }
    publishLpEvent(kind, player, amount) {
        this.duelEvents.dispatch(kind, (0, DuelEventDecoders_1.buildDamageDealt)({ player, amount }, this.duelEventContext()), this.room);
    }
    /**
     * The room counter has already incremented when this pipeline's NEW_TURN
     * middleware runs (ocgcore.ts registers increaseTurn first), so the current
     * value is the number of the turn that just started.
     */
    publishTurnStart(player) {
        this.duelEvents.dispatch("duel.turn-start", (0, DuelEventDecoders_1.buildTurnStarted)({ player }, this.duelEventContext()), this.room);
    }
    resolveTeam(side) {
        return (this.room.isTag ? side : this.ocgCore.getSideTeam(side));
    }
    broadcastRoomUpdate() {
        WebSocketSingleton_1.default.getInstance().broadcast({
            action: "UPDATE-ROOM",
            data: this.room.toRealTimePresentation(),
        });
    }
    removeRoom() {
        FinalizeYGOProRoom_1.FinalizeYGOProRoom.run(this.room);
    }
}
exports.YGOProDuelingState = YGOProDuelingState;
//# sourceMappingURL=YGOProDuelingState.js.map
