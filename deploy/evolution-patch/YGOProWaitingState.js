"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.YGOProWaitingState = void 0;
const PlayerInfoMessage_1 = require("../../../../edopro/messages/client-to-server/PlayerInfoMessage");
const Commands_1 = require("../../../../shared/messages/Commands");
const ReconnectionTokenIssuer_1 = require("../../../../shared/room/application/reconnect/ReconnectionTokenIssuer");
const SystemChat_1 = require("../../../../shared/room/domain/chat/SystemChat");
const RoomCreationNotice_1 = require("../../../../shared/room/domain/chat/RoomCreationNotice");
const ygopro_msg_encode_1 = require("ygopro-msg-encode");
const DeckError_1 = require("../../../../shared/deck/domain/errors/DeckError");
const encodeDeckErrorCode_1 = require("../../../../shared/deck/domain/errors/encodeDeckErrorCode");
const YGOProRoomState_1 = require("../YGOProRoomState");
const config_1 = require("../../../config");
const config_2 = require("../../../../config");
const YGOProJoinGameMessage_1 = require("../../../messages/YGOProJoinGameMessage");
class YGOProWaitingState extends YGOProRoomState_1.YGOProRoomState {
    admitToRoom;
    logger;
    deckCreator;
    deckValidator;
    starting = false;
    constructor(admitToRoom, eventEmitter, logger, deckCreator, deckValidator) {
        super(eventEmitter);
        this.admitToRoom = admitToRoom;
        this.logger = logger;
        this.deckCreator = deckCreator;
        this.deckValidator = deckValidator;
        this.logger = logger.child({ file: "MercuryWaitingState" });
        this.eventEmitter.on("JOIN", (message, room, socket) => void this.handleJoin.bind(this)(message, room, socket));
        this.eventEmitter.on("MATCH_ADMIT", (playerInfo, room, socket) => void this.handleMatchAdmit.bind(this)(playerInfo, room, socket));
        this.eventEmitter.on(Commands_1.Commands.TRY_START, (message, room, client) => void this.handleTryStart.bind(this)(message, room, client));
        this.eventEmitter.on(Commands_1.Commands.OBSERVER, (message, room, client) => void this.handleToObserver.bind(this)(message, room, client));
        this.eventEmitter.on(Commands_1.Commands.TO_DUEL, (message, room, client) => void this.handleToDuel.bind(this)(message, room, client));
        this.eventEmitter.on(Commands_1.Commands.UPDATE_DECK, (message, room, client) => void this.handleUpdateDeck.bind(this)(message, room, client));
        this.eventEmitter.on(Commands_1.Commands.NOT_READY, (message, room, client) => void this.handleNotReady.bind(this)(message, room, client));
    }
    async handleJoin(message, room, socket) {
        this.logger.info(`handleJoin: ${message.data.toString("hex")}`);
        if (!this.rejectOnVersionMismatch(message.data, room, socket)) {
            return;
        }
        const playerInfoMessage = new PlayerInfoMessage_1.PlayerInfoMessage(message.previousMessage, message.data.length);
        await this.admit(room, socket, playerInfoMessage);
    }
    /**
     * Server-side admission door for a match-admitted socket. The client never
     * sends a JOIN frame on this path, so there is no version to check — every
     * other gate is identical to handleJoin, funnelled through the shared
     * admit() so both doors can never drift.
     */
    async handleMatchAdmit(playerInfo, room, socket) {
        this.logger.info(`handleMatchAdmit: socket=${socket.id}`);
        await this.admit(room, socket, playerInfo);
    }
    /**
     * Shared admission gates for both handleJoin and handleMatchAdmit:
     * reservation → reconnect-or-duplicate-name → AdmitToRoom.
     */
    async admit(room, socket, playerInfo) {
        // Reservation gate before any admission work: a reserved room's join
        // string is not a key — only the stamped identities, the bot's
        // token-marked socket, or a watch-stamped spectator (stands only, by
        // construction) get past this door.
        if (!room.reservationAdmits(socket)) {
            room.rejectReservedJoin(socket);
            return;
        }
        await room.mutex.runExclusive(async () => {
            const existing = room.players.find((player) => player.name === playerInfo.name);
            if (existing) {
                if (room.provisioning?.continuous_series === true && existing.socket.closed) {
                    // The gateway authenticates the player password and rewrites
                    // PLAYER_INFO to the original engine name. Reattach that proven
                    // identity to its existing lobby object instead of rejecting the
                    // duplicate name or allocating a different seat.
                    existing.notReady();
                    room.reconnect(existing, socket);
                    existing.clearReconnecting();
                    return;
                }
                this.sendNameTakenError(room, playerInfo.name, socket);
                return;
            }
            const result = await this.admitToRoom.run(socket, playerInfo, room.admissionTarget(socket, playerInfo));
            this.sendRoomCreationNoticeToCreator(room, socket, result);
            if (result?.kind === "spectator" && room.seriesReplay) {
                const spectator = room.spectators.find(
                    (client) => client.socket.id === socket.id
                );
                if (spectator) {
                    setImmediate(() => void room.playSeriesReplay(spectator));
                }
            }
        });
    }
    /**
     * The edopro pipeline sends this right after CREATE_GAME. This pipeline
     * instead creates the room inside the JOIN flow (findOrCreateRoom) and
     * only seats the creator once admission succeeds here — so this is the
     * one point that can fire it, guarded to the creator's own first seat so
     * later joiners, spectators, rejections and reconnects never see it.
     */
    sendRoomCreationNoticeToCreator(room, socket, result) {
        if (result?.kind === "player" &&
            socket.id === room.createdBySocketId &&
            room.players.length === 1) {
            socket.send((0, RoomCreationNotice_1.roomCreationNotice)({
                ranked: room.league.isRanked,
                rankingEnabled: config_2.config.ranking.enabled,
            }));
        }
    }
    /**
     * Whether the joiner's client version matches the server's; rejects it if not.
     *
     * Returns a boolean instead of throwing (the shape the EDOPro base's
     * `validateVersion` used). handleJoin is async and the "JOIN" listener voids its
     * promise, so a throw here became an unhandled rejection: Node routes it to the
     * global uncaughtException hook (src/shared/error-handler/error-handler.ts),
     * which logs a routine client-version mismatch as "Excepción no capturada" — and
     * because nothing after the throw ran, the rejected socket stayed open as a
     * live-but-rejected connection. Encoding was never the problem here:
     * VersionErrorClientMessage.create is byte-identical to the ygopro frame
     * (its `decimalToBytesBuffer(0, 3)` already covers STOC_ErrorMsg's alignment
     * padding). We go through the ygopro repository anyway, so the ygopro path owns
     * its own wire format instead of borrowing EDOPro's.
     */
    rejectOnVersionMismatch(data, room, socket) {
        const joinMessage = new YGOProJoinGameMessage_1.YGOProJoinGameMessage(data);
        if (joinMessage.version === config_1.mercuryConfig.version) {
            return true;
        }
        socket.send(room.messageSender.errorMessage(ygopro_msg_encode_1.ErrorMessageType.VERERROR, config_1.mercuryConfig.version));
        socket.close();
        return false;
    }
    /**
     * Reject a joiner whose name is already seated.
     *
     * Must NOT reuse RoomState.sendExistingPlayerErrorMessage: that helper writes
     * EDOPro-flavoured frames (a 0xF3 server-error frame the classic ygopro client
     * does not know, plus an EDOPro-packed STOC_ERROR_MSG of size 6). ygopro drops
     * STOC_ERROR_MSG when `len < 1 + sizeof(STOC_ErrorMsg)` — 9 bytes once the
     * struct's alignment padding is counted — so the joiner got no error at all and
     * sat on the connecting screen. Encode through the ygopro repository instead,
     * mirroring YGOProRoom.admissionTarget's rejectAdmission path: a red STOC_CHAT
     * explaining why, then the real JOINERROR, then a graceful close() so both
     * frames flush before teardown (see SocketCloseOnError.test.ts).
     */
    sendNameTakenError(room, name, socket) {
        socket.send((0, SystemChat_1.createSystemChat)(ygopro_msg_encode_1.ChatColor.RED, `Nickname '${name}' is already in use — choose another.`));
        socket.send(room.messageSender.errorMessage(ygopro_msg_encode_1.ErrorMessageType.JOINERROR, 0));
        socket.close();
    }
    startReadyRoom(room) {
        const requiredPlayerCount = room.team0 + room.team1;
        if (this.starting ||
            room.players.length !== requiredPlayerCount ||
            room.players.some((player) => player.socket.closed) ||
            !room.allPlayersReady) {
            return false;
        }
        this.starting = true;
        try {
        for (const player of room.clients) {
            player.sendMessageToClient(room.messageSender.duelStartMessage());
            room.sendDeckCountMessage(player);
        }
        // Issue a per-player reconnection token at match start so every duel phase
        // (RPS, choosing order, dueling, side-decking) supports token reconnect.
        // WindBot rooms (noReconnect) are skipped — bots never reconnect.
        if (!room.noReconnect) {
            for (const player of room.players) {
                player.sendMessageToClient(ReconnectionTokenIssuer_1.ReconnectionTokenIssuer.issue(player, room.id));
            }
        }
        room.createMatch();
        if (room.requiredFirstPlayer) {
            // Deterministic tournament rooms already define the first player.
            // Keep the normal client handshake, but bypass RPS and turn-choice
            // messages so the native engine starts with the planned seat.
            // Provisioned admission assigns each canonical player identity to
            // its planned physical seat, independent of connection order.
            room.setPositionSwapped(false);
            room.dueling();
        }
        else {
            this.toRPS(room);
            room.rps();
        }
        return true;
        }
        catch (error) {
            this.starting = false;
            this.logger.error(`startReadyRoom failed: ${String(error)}`);
            return false;
        }
    }
    handleTryStart(_message, room, player) {
        player.logger.info("handleTryStart");
        if (!player.host) {
            return;
        }
        void room.mutex.runExclusive(() => this.startReadyRoom(room)).catch((error) => {
            this.starting = false;
            this.logger.error(`startReadyRoom failed: ${String(error)}`);
        });
    }
    handleToObserver(message, room, player) {
        player.logger.info(`handleToObserver: ${message.data.toString("hex")}`);
        room.mutex.runExclusive(() => {
            if (player.isSpectator) {
                return;
            }
            if (!player.host) {
                room.playerToSpectatorUnsafe(player);
            }
        });
    }
    handleToDuel(_message, room, player) {
        player.logger.info("handleToDuel");
        room.mutex.runExclusive(() => {
            if (player.isSpectator) {
                // Taking a seat always passes admission: a spectator may only sit if
                // the room's league accepts how it authenticated. A wrong-league
                // spectator stays in the stands (closes the escalation through the
                // stands door, mirroring the JOIN door).
                const credential = player.credential ?? { kind: "guest", name: player.name };
                if (!room.league.admitsAsPlayer(credential)) {
                    return;
                }
                // In a reserved room only a reserved identity may leave the stands:
                // the JOIN gate admits watch spectators, so this promotion door must
                // run the seat-taking reservation check or a non-reserved watcher
                // could TO_DUEL into a reserved seat before the duel starts.
                if (!room.reservationPermitsSeat(player.socket)) {
                    return;
                }
                room.spectatorToPlayerUnsafe(player);
                return;
            }
            room.movePlayerToAnotherCellUnsafe(player);
        });
    }
    async handleUpdateDeck(message, room, player) {
        player.logger.info(`handleUpdateDeck: ${message.data.toString("hex")}`);
        const updateDeckMessage = new ygopro_msg_encode_1.YGOProCtosUpdateDeck().fromPayload(message.data);
        if (player.isSpectator) {
            return;
        }
        const deckOrError = await this.deckCreator.build({
            main: updateDeckMessage.deck.main,
            side: updateDeckMessage.deck.side,
            banListHash: room.banListHash,
        });
        if (deckOrError instanceof DeckError_1.DeckError) {
            this.logger.warn(`Deck build error: type=0x${deckOrError.type.toString(16)}, code=${deckOrError.code}, rule=${room.hostInfo.rule}, pool=${room.cardPool}`);
            room.notReadyUnsafe(player);
            player.sendMessageToClient(room.messageSender.errorMessage(ygopro_msg_encode_1.ErrorMessageType.DECKERROR, (0, encodeDeckErrorCode_1.encodeDeckErrorCode)(deckOrError.type, deckOrError.code)));
            return;
        }
        const deck = deckOrError;
        if (player.isInternal) {
            await room.mutex.runExclusive(() => {
                room.setDecksToPlayerUnsafe(player.position, deck);
                if (room.provisioning?.continuous_series === true) {
                    this.startReadyRoom(room);
                }
            });
            return;
        }
        const hasError = room.shouldValidateDeck() && this.deckValidator.validate(deck);
        if (hasError) {
            const failedCard = deck.allCards.find((c) => Number(c.code) === hasError.code);
            this.logger.warn(`Deck validation error: type=0x${hasError.type.toString(16)}, code=${hasError.code}, cardOt=${failedCard?.variant ?? "N/A"}, rule=${room.hostInfo.rule}, pool=${room.cardPool}`);
            room.notReadyUnsafe(player);
            player.sendMessageToClient(room.messageSender.errorMessage(ygopro_msg_encode_1.ErrorMessageType.DECKERROR, (0, encodeDeckErrorCode_1.encodeDeckErrorCode)(hasError.type, hasError.code)));
            return;
        }
        await room.mutex.runExclusive(() => {
            room.setDecksToPlayerUnsafe(player.position, deck);
            if (room.provisioning?.continuous_series === true) {
                this.startReadyRoom(room);
            }
        });
    }
    async handleNotReady(message, room, player) {
        player.logger.info(`handleNotReady: ${message.data.toString("hex")}`);
        room.mutex.runExclusive(() => {
            room.notReadyUnsafe(player);
        });
    }
}
exports.YGOProWaitingState = YGOProWaitingState;
//# sourceMappingURL=YGOProWaitingState.js.map
