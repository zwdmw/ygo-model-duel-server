"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.DisconnectHandler = void 0;
const Client_1 = require("../../../edopro/client/domain/Client");
const PlayerChangeClientMessage_1 = require("../../../edopro/messages/server-to-client/PlayerChangeClientMessage");
const WatchChangeClientMessage_1 = require("../../../edopro/messages/server-to-client/WatchChangeClientMessage");
const Room_1 = require("../../../edopro/room/domain/Room");
const RoomList_1 = __importDefault(require("../../../edopro/room/infrastructure/RoomList"));
const YGOProClient_1 = require("../../../ygopro/client/domain/YGOProClient");
const YGOProRoom_1 = require("../../../ygopro/room/domain/YGOProRoom");
const WebSocketSingleton_1 = __importDefault(require("../../../web-socket-server/WebSocketSingleton"));
const YgoRoom_1 = require("../domain/YgoRoom");
const FinalizeYGOProRoom_1 = require("../../../ygopro/room/application/FinalizeYGOProRoom");
const AbortMatchmakingRoom_1 = require("../../../ygopro/matchmaking/application/AbortMatchmakingRoom");
const MatchmakingQueue_1 = require("../../../ygopro/matchmaking/application/MatchmakingQueue");
const NOOP_LOGGER = {
    debug: () => undefined,
    error: () => undefined,
    info: () => undefined,
    warn: () => undefined,
    child: () => NOOP_LOGGER,
};
class DisconnectHandler {
    socket;
    roomFinder;
    logger;
    constructor(socket, roomFinder, logger = NOOP_LOGGER) {
        this.socket = socket;
        this.roomFinder = roomFinder;
        this.logger = logger;
    }
    run(address) {
        if (!this.socket.id) {
            return;
        }
        this.dequeueFromMatchmaking();
        const room = this.roomFinder.run(this.socket.id);
        if (!room) {
            return;
        }
        if (room instanceof Room_1.Room) {
            this.handle(room, address);
            return;
        }
        if (room instanceof YGOProRoom_1.YGOProRoom) {
            this.handleYGOPro(room);
            return;
        }
    }
    handle(room, address) {
        if (room.hasNoConnectedPlayers) {
            RoomList_1.default.deleteRoom(room);
            WebSocketSingleton_1.default.getInstance().broadcast({
                action: "REMOVE-ROOM",
                data: room.toRealTimePresentation(),
            });
            return;
        }
        const player = room.players.find((client) => client.socket.id === this.socket.id);
        if (!(player instanceof Client_1.Client)) {
            this.removeSpectator(room);
            return;
        }
        if (player.host && room.duelState === YgoRoom_1.DuelState.WAITING) {
            RoomList_1.default.deleteRoom(room);
            WebSocketSingleton_1.default.getInstance().broadcast({
                action: "REMOVE-ROOM",
                data: room.toRealTimePresentation(),
            });
            return;
        }
        if (room.duelState === YgoRoom_1.DuelState.WAITING) {
            room.removePlayer(player);
            player.socket.removeAllListeners();
            const status = (player.position << 4) | 0xb;
            const message = PlayerChangeClientMessage_1.PlayerChangeClientMessage.create({ status });
            room.players.forEach((client) => {
                client.sendMessage(message);
            });
            room.spectators.forEach((spectator) => {
                spectator.sendMessage(message);
            });
            return;
        }
    }
    dequeueFromMatchmaking() {
        if (!MatchmakingQueue_1.MatchmakingQueue.isInitialized()) {
            return;
        }
        const dequeued = MatchmakingQueue_1.MatchmakingQueue.getInstance().dequeueBySocketId(this.socket.id);
        if (dequeued) {
            this.logger.info("matchmaking.dequeued", { reason: "disconnect" });
        }
    }
    handleYGOPro(room) {
        if (room.isMatchmaking && room.duelState === YgoRoom_1.DuelState.WAITING) {
            AbortMatchmakingRoom_1.AbortMatchmakingRoom.run(room);
            return;
        }
        const player = room.players.find((client) => client.socket.id === this.socket.id);
        if (!(player instanceof YGOProClient_1.YGOProClient)) {
            this.removeMercurySpectator(room);
            // Replay-only rooms have no players, so an empty-player check before
            // removing this spectator would tear down every concurrent viewer.
            // Keep the room while any other spectator is still playing it.
            if (room.hasNoConnectedPlayers && room.spectators.length === 0) {
                FinalizeYGOProRoom_1.FinalizeYGOProRoom.run(room);
            }
            return;
        }
        if (room.hasNoConnectedPlayers) {
            FinalizeYGOProRoom_1.FinalizeYGOProRoom.run(room);
            return;
        }
        if (room.noHost) {
            FinalizeYGOProRoom_1.FinalizeYGOProRoom.run(room);
            return;
        }
        if (room.duelState === YgoRoom_1.DuelState.WAITING) {
            room.playerLeave(player);
            player.destroy();
        }
    }
    removeSpectator(room) {
        const spectator = room.spectators.find((client) => client.socket.id === this.socket.id);
        if (!(spectator instanceof Client_1.Client)) {
            return;
        }
        room.removeSpectator(spectator);
        spectator.socket.removeAllListeners();
        const message = WatchChangeClientMessage_1.WatchChangeClientMessage.create({
            count: room.spectators.length,
        });
        room.players.forEach((_client) => {
            _client.sendMessage(message);
        });
        room.spectators.forEach((_client) => {
            _client.sendMessage(message);
        });
        if (room.hasNoConnectedPlayers && room.spectators.length === 0) {
            RoomList_1.default.deleteRoom(room);
            WebSocketSingleton_1.default.getInstance().broadcast({
                action: "REMOVE-ROOM",
                data: room.toRealTimePresentation(),
            });
        }
    }
    removeMercurySpectator(room) {
        const spectator = room.spectators.find((client) => client.socket.id === this.socket.id);
        if (!(spectator instanceof YGOProClient_1.YGOProClient)) {
            return;
        }
        room.spectatorLeave(spectator);
        spectator.destroy();
    }
}
exports.DisconnectHandler = DisconnectHandler;
//# sourceMappingURL=DisconnectHandler.js.map
