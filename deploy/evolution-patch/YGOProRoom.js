"use strict";
var __importDefault = (this && this.__importDefault) || function (mod) {
    return (mod && mod.__esModule) ? mod : { "default": mod };
};
Object.defineProperty(exports, "__esModule", { value: true });
exports.YGOProRoom = void 0;
const BanListMemoryRepository_1 = __importDefault(require("../../../edopro/ban-list/infrastructure/BanListMemoryRepository"));
const Team_1 = require("../../../shared/room/Team");
const RoomLeague_1 = require("../../../shared/room/admission/domain/RoomLeague");
const Seat_1 = require("../../../shared/room/admission/domain/Seat");
const AdmitToRoom_1 = require("../../room/admission/application/AdmitToRoom");
const UserAuth_1 = require("../../../shared/user-auth/application/UserAuth");
const UserProfilePostgresRepository_1 = require("../../../shared/user-profile/infrastructure/postgres/UserProfilePostgresRepository");
const CredentialResolver_1 = require("../../room/admission/application/CredentialResolver");
const RoomAdmission_1 = require("../../../shared/room/admission/domain/RoomAdmission");
const YgoRoom_1 = require("../../../shared/room/domain/YgoRoom");
const RoomType_1 = require("../../../shared/room/domain/RoomType");
const YGOProBanListMemoryRepository_1 = __importDefault(require("../../ban-list/infrastructure/YGOProBanListMemoryRepository"));
const YGOProClient_1 = require("../../client/domain/YGOProClient");
const RuleMappings_1 = require("./RuleMappings");
const YGOProChoosingOrderState_1 = require("./states/YGOProChoosingOrderState");
const YGOProDuelingState_1 = require("./states/YGOProDuelingState");
const YGOProRockPaperScissorState_1 = require("./states/YGOProRockPaperScissorState");
const YGOProSideDeckingState_1 = require("./states/YGOProSideDeckingState");
const YGOProWaitingState_1 = require("./states/YGOProWaitingState");
const LobbyDuelFlags_1 = require("./LobbyDuelFlags");
const ygopro_msg_encode_1 = require("ygopro-msg-encode");
const YGOProDeckCreator_1 = require("../../deck/application/YGOProDeckCreator");
const YGOProDeckValidator_1 = require("../../deck/domain/YGOProDeckValidator");
const CardYGOProRepository_1 = require("../../card/infrastructure/CardYGOProRepository");
const YGOProBanList_1 = require("../../ban-list/domain/YGOProBanList");
const fs_1 = require("node:fs");
const path_1 = require("node:path");
const crypto_1 = require("node:crypto");
const zlib_1 = require("node:zlib");
const SpectatorView_1 = require("./SpectatorView");
const SeriesReplayView_1 = require("./SeriesReplayView");
const MAX_SERIES_REPLAY_BYTES = 64 * 1024 * 1024;
const MAX_SERIES_REPLAY_JSON_BYTES = 256 * 1024 * 1024;
const MAX_SERIES_REPLAY_DUELS = 10000;
const MAX_SERIES_REPLAY_FRAMES = 2000000;
const BEST_OF = {
    [ygopro_msg_encode_1.GameMode.SINGLE]: 1,
    [ygopro_msg_encode_1.GameMode.MATCH]: 3,
    [ygopro_msg_encode_1.GameMode.TAG]: 1,
};
// Computed once at module load (not per YGOProRoom.create() call).
// Order matters: later tiers overwrite earlier ones for the same hostInfo key.
const MODE_TIER = Object.values(RuleMappings_1.ruleMappings);
const FORMAT_TIER = Object.values(RuleMappings_1.formatRuleMappings);
const PRIORITY_TIER = Object.values(RuleMappings_1.priorityRuleMappings);
const RULE_MAPPING_TIERS = [MODE_TIER, FORMAT_TIER, PRIORITY_TIER];
function loadSeriesReplay(provisioning) {
    if (provisioning?.series_finished !== true) {
        return null;
    }
    const metadata = provisioning.series_replay;
    // The live room observes series_finished before it emits EVRP, so the
    // provisioning file briefly has no archive metadata. That state is valid
    // for finalization; the gateway will not admit a replay spectator until
    // the completed archive is published.
    if (metadata == null) {
        return null;
    }
    if (typeof metadata.path !== "string" ||
        !Number.isInteger(metadata.bytes) ||
        metadata.bytes <= 0 ||
        metadata.bytes > MAX_SERIES_REPLAY_BYTES ||
        typeof metadata.sha256 !== "string" ||
        !/^[0-9a-f]{64}$/.test(metadata.sha256)) {
        throw new Error("series replay metadata is invalid");
    }
    const compressed = (0, fs_1.readFileSync)(metadata.path);
    if (compressed.length !== metadata.bytes) {
        throw new Error("series replay size does not match provisioning");
    }
    const digest = (0, crypto_1.createHash)("sha256")
        .update(compressed)
        .digest("hex");
    if (digest !== metadata.sha256) {
        throw new Error("series replay checksum does not match provisioning");
    }
    const raw = (0, zlib_1.gunzipSync)(compressed, {
        maxOutputLength: MAX_SERIES_REPLAY_JSON_BYTES,
    });
    const envelope = JSON.parse(raw.toString("utf8"));
    if (envelope?.version !== 1 ||
        !envelope.meta ||
        !Array.isArray(envelope.duels) ||
        envelope.duels.length < 1 ||
        envelope.duels.length > MAX_SERIES_REPLAY_DUELS) {
        throw new Error("series replay envelope is invalid");
    }
    let totalFrames = 0;
    const duels = envelope.duels.map((duel) => {
        if (!duel ||
            !Array.isArray(duel.spectatorFrames) ||
            duel.spectatorFrames.length === 0) {
            throw new Error("series replay spectator frames are missing");
        }
        totalFrames += duel.spectatorFrames.length;
        if (totalFrames > MAX_SERIES_REPLAY_FRAMES) {
            throw new Error("series replay contains too many frames");
        }
        const spectatorFrames = duel.spectatorFrames.map((encoded) => {
            if (typeof encoded !== "string" || encoded.length > 90000) {
                throw new Error("series replay contains an invalid frame");
            }
            const frame = Buffer.from(encoded, "base64");
            if (frame.toString("base64") !== encoded ||
                frame.length < 3 ||
                frame.length > 0x10000 ||
                frame.readUInt16LE(0) !== frame.length - 2 ||
                frame[2] !== 0x01) {
                throw new Error("series replay contains an invalid native frame");
            }
            return frame;
        });
        return { ...duel, spectatorFrames };
    });
    return (0, SeriesReplayView_1.prepareSeriesReplayView)({ meta: envelope.meta, duels });
}
function loadProvisioning(configuration) {
    const directory = process.env.YGO_PROVISIONING_DIR;
    const roomKey = configuration.split(",")[0]?.trim() ?? "";
    if (!directory || !/^p[0-9a-f]+$/i.test(roomKey)) {
        return null;
    }
    const file = (0, path_1.join)(directory, `${roomKey}.json`);
    try {
        const value = JSON.parse((0, fs_1.readFileSync)(file, "utf8"));
        const hasSeed = value.seed !== null && value.seed !== undefined;
        const validSeed = Array.isArray(value.seed) &&
            value.seed.length === 8 &&
            value.seed.every((word) => Number.isInteger(word) &&
                word >= 0 &&
                word <= 0xffffffff);
        if (value?.room_key !== roomKey ||
            (hasSeed && !validSeed) ||
            !value.seat_plan ||
            ![0, 1].includes(value.seat_plan.a) ||
            ![0, 1].includes(value.seat_plan.b) ||
            value.seat_plan.a === value.seat_plan.b ||
            !value.players ||
            typeof value.players.a !== "string" ||
            typeof value.players.b !== "string" ||
            !value.players.a ||
            !value.players.b ||
            value.players.a === value.players.b ||
            (value.time_limit !== undefined &&
                (!Number.isInteger(value.time_limit) ||
                    value.time_limit < 1 ||
                    value.time_limit > 999)) ||
            (hasSeed && !["a", "b"].includes(value.required_first_player))) {
            throw new Error("invalid deterministic duel provisioning");
        }
        if (hasSeed && value.seat_plan[value.required_first_player] !== 0) {
            throw new Error("deterministic duel seat plan is inconsistent");
        }
        // Non-seeded rooms still use provisioning for scoped spectator policy,
        // but must retain Evolution's ordinary RPS/turn-choice flow.
        const normalized = hasSeed ? value : { ...value, required_first_player: null };
        return { ...normalized, seriesReplay: loadSeriesReplay(normalized) };
    }
    catch (error) {
        if (error?.code === "ENOENT") {
            return null;
        }
        throw new Error(`failed to load deterministic duel provisioning for ${roomKey}: ${String(error)}`);
    }
}
class YGOProRoom extends YgoRoom_1.YgoRoom {
    name;
    password;
    league;
    createdBySocketId;
    banListHash;
    cardPool;
    //TODO: compatibility with edopro list and rank;
    _edoBanListHash;
    _logger;
    _roomState = null;
    _isPositionSwapped = false;
    _duelRecords = [];
    _currentDuelRecord;
    _hostInfo;
    _messageRepository;
    _cardRepository;
    _deckRules;
    windbot;
    noHost = false;
    noReconnect = false;
    /** True only for rooms pre-created by the matchmaking queue. Their WAITING
     * lifecycle is atomic: if either participant leaves, the reservation aborts. */
    isMatchmaking = false;
    /** Present only on rooms whose seats were assigned before anyone connected
     * (matchmaking): the userIds allowed through the JOIN door. Undefined for
     * ordinary rooms, which admit by (name, password) alone. */
    reservedUserIds;
    // Set to true when the room begins teardown (removeRoom entry point in YGOProDuelingState).
    // The WindBotJoinStrategy retry-abort callback reads this to stop retrying when the room
    // is being torn down.
    finalizing = false;
    // Set when a duel in a match ends in a DRAW. KDE Tournament Policy §IV.F:
    // after a drawn duel the loser-chooses rule does not apply — "another random
    // method should be employed", so side-decking must re-enter RPS instead of
    // reusing the previous game's chooser.
    turnChoiceRequiresRps = false;
    provisioning = null;
    provisionedSeed = null;
    requiredFirstPlayer = null;
    seriesGameNumber = 0;
    seriesSurrenderIgnoreUntil = 0;
    seriesTransitioning = false;
    seriesReplay = null;
    constructor({ id, name, password = "", hostInfo, team0, team1, league, createdBySocketId, bestOf, startLp, messageRepository, banListHash, cardPool, provisioning = null, }) {
        super({
            team0,
            team1,
            ranked: league.isRanked,
            bestOf,
            startLp,
            id,
            notes: "",
            roomType: RoomType_1.RoomType.MERCURY,
        });
        this.name = name;
        this.password = password;
        this.league = league;
        this._players = [];
        this._hostInfo = hostInfo;
        this._state = YgoRoom_1.DuelState.WAITING;
        this.banListHash = banListHash;
        this.cardPool = cardPool;
        this.provisioning = provisioning;
        this.provisionedSeed = provisioning?.seed ? [...provisioning.seed] : null;
        this.requiredFirstPlayer = provisioning?.required_first_player ?? null;
        this.seriesGameNumber = provisioning?.continuous_series ? provisioning.game_number : 0;
        this.seriesReplay = provisioning?.seriesReplay ?? null;
        this.createdBySocketId = createdBySocketId;
        this._messageRepository = messageRepository;
        this._cardRepository = new CardYGOProRepository_1.CardYGOProRepository(this.cardPool);
        this._deckRules = new YgoRoom_1.DeckRules({
            mainMin: 40,
            mainMax: 60,
            extraMin: 0,
            extraMax: 15,
            sideMin: 0,
            sideMax: 15,
            rule: this._hostInfo.rule,
            maxDeckPoints: this._hostInfo.max_deck_points,
        });
        const banList = YGOProBanListMemoryRepository_1.default.findByHash(banListHash);
        const edoBanList = BanListMemoryRepository_1.default.findByName(banList?.name ?? "");
        this._edoBanListHash = edoBanList?.hash ?? 0;
    }
    static create(id, command, logger, emitter, playerInfo, createdBySocketId, messageRepository, rankedOverride) {
        let hostInfo = {
            lflist: YGOProBanListMemoryRepository_1.default.getFirstTCGIndex(),
            rule: 1,
            mode: ygopro_msg_encode_1.GameMode.SINGLE,
            duel_rule: 5,
            no_check_deck: 0,
            no_shuffle_deck: 0,
            start_lp: 8000,
            start_hand: 5,
            draw_count: 1,
            time_limit: 450,
            max_deck_points: 100,
            best_of: BEST_OF[ygopro_msg_encode_1.GameMode.SINGLE],
        };
        const [configuration, password = ""] = command.split("#");
        const provisioning = loadProvisioning(configuration);
        const options = configuration
            .toLowerCase()
            .split(",")
            .map((_) => _.trim());
        // One loop over the three tiers (mode → format → priority), each tier
        // fully applied to every option before the next tier starts — so later
        // tiers still overwrite earlier ones for the same hostInfo key, and a
        // double-match is only ever checked WITHIN a single tier's mapping list.
        for (const tierMappings of RULE_MAPPING_TIERS) {
            options.forEach((option) => {
                const items = tierMappings.filter((item) => item.validate(option));
                if (items.length > 1) {
                    throw new Error(`Error: param match with two rules.`);
                }
                const mapping = items.shift();
                if (mapping) {
                    const rule = mapping.get(option);
                    hostInfo = { ...hostInfo, ...rule };
                }
            });
        }
        if (provisioning?.time_limit !== undefined) {
            hostInfo = { ...hostInfo, time_limit: provisioning.time_limit };
        }
        const teamCount = hostInfo.mode === ygopro_msg_encode_1.GameMode.TAG ? 2 : 1;
        // The host's explicit "casual" token wins over any ranked default — a
        // ticket-authenticated user must be able to host unranked rooms.
        const casual = options.includes("casual");
        const league = RoomLeague_1.RoomLeague.determine({
            casual,
            rankedOverride,
            hasPin: Boolean(playerInfo.password),
        });
        const cardPool = (0, RuleMappings_1.resolveCardPool)(options);
        const banList = YGOProBanListMemoryRepository_1.default.findLFListByIndex(hostInfo.lflist);
        const banListHash = banList?.hash ?? 0;
        const room = new YGOProRoom({
            id,
            hostInfo,
            name: configuration,
            password,
            team0: teamCount,
            team1: teamCount,
            league,
            createdBySocketId,
            bestOf: hostInfo.best_of,
            startLp: hostInfo.start_lp,
            messageRepository,
            banListHash,
            cardPool,
            provisioning,
        });
        room._logger = logger.child({ file: "MercuryRoom" });
        room.emitter = emitter;
        return room;
    }
    shouldValidateDeck() {
        return !this._hostInfo.no_check_deck;
    }
    get isTag() {
        return (this.hostInfo.mode & 0x2) !== 0;
    }
    get mode() {
        return this.hostInfo.mode > 2 ? (this.isTag ? 2 : 1) : this.hostInfo.mode;
    }
    getTeamPlayers(team) {
        return this.players
            .filter((client) => client.team === team)
            .sort((a, b) => a.position - b.position);
    }
    get isMatch() {
        return this.bestOf > 1;
    }
    get duelMode() {
        return this.isTag ? "tag" : this.isMatch ? "match" : "single";
    }
    get shuffleDeckEnabled() {
        return !this.hostInfo.no_shuffle_deck;
    }
    setPositionSwapped(value) {
        this._isPositionSwapped = value;
    }
    suppressSeriesSurrendersFor(durationMs) {
        if (this.provisioning?.continuous_series !== true) {
            return;
        }
        this.seriesSurrenderIgnoreUntil = Math.max(this.seriesSurrenderIgnoreUntil, Date.now() + durationMs);
    }
    isSeriesSurrenderSuppressed() {
        return (this.provisioning?.continuous_series === true &&
            Date.now() < this.seriesSurrenderIgnoreUntil);
    }
    beginSeriesTransition() {
        if (this.provisioning?.continuous_series === true) {
            this.seriesTransitioning = true;
        }
    }
    isSeriesTransitionInProgress() {
        return this.seriesTransitioning;
    }
    hasConnectedSeriesPlayers() {
        const expectedPlayers = this.team0 + this.team1;
        return (this._players.length === expectedPlayers &&
            this._players.every((player) => !player.socket.closed));
    }
    assertSeriesPlayersConnected() {
        if (this.provisioning?.continuous_series === true &&
            !this.hasConnectedSeriesPlayers()) {
            throw new Error("player_disconnected_between_games");
        }
    }
    async waitForSeriesPlayersConnected(timeoutMs) {
        const deadline = Date.now() + timeoutMs;
        while (Date.now() < deadline) {
            if (this.hasConnectedSeriesPlayers()) {
                return;
            }
            await new Promise((resolve) => setTimeout(resolve, 100));
        }
        throw new Error("player_disconnected_between_games");
    }
    finishSeriesTransition() {
        this.assertSeriesPlayersConnected();
        this._players.forEach((player) => player.clearReconnecting());
        this.seriesTransitioning = false;
    }
    async waitForNextSeriesGame() {
        const deadline = Date.now() + 60000;
        while (Date.now() < deadline) {
            const provision = loadProvisioning(this.name);
            if (provision?.session_id !== this.provisioning.session_id ||
                provision.continuous_series !== true) {
                throw new Error("series provisioning changed unexpectedly");
            }
            if (provision.game_number === this.seriesGameNumber && provision.series_finished) {
                return false;
            }
            if (provision.game_number === this.seriesGameNumber + 1 &&
                !provision.series_finished) {
                this.setPositionSwapped(
                    provision.seat_plan.a !== this.provisioning.seat_plan.a
                );
                this.provisionedSeed = [...provision.seed];
                this.requiredFirstPlayer = provision.required_first_player;
                this.seriesGameNumber = provision.game_number;
                return true;
            }
            await new Promise((resolve) => setTimeout(resolve, 100));
        }
        throw new Error("timed out waiting for next series game");
    }
    get isPositionSwapped() {
        return this._isPositionSwapped;
    }
    get hostInfo() {
        return {
            ...this._hostInfo,
            mode: this._hostInfo.mode,
            lflist: this.banListHash,
        };
    }
    get playersCount() {
        return this._players.length;
    }
    get isPlayersFull() {
        return ((this._hostInfo.mode === ygopro_msg_encode_1.GameMode.SINGLE || this._hostInfo.mode === ygopro_msg_encode_1.GameMode.MATCH) &&
            this.playersCount === 2);
    }
    /**
     * Minimal read-only routing hint for YGOProRoomList.findJoinableByName.
     * This is a LOCK-FREE read (does not take the room mutex) — it is only ever
     * used to decide whether a room is worth attempting to join. Real admission
     * still runs exclusively under AdmitToRoom / calculatePlace (which DOES take
     * the mutex), so a seat that looks free here can still lose the race by the
     * time admission actually runs — the caller must be able to tolerate that.
     */
    hasFreeSeat() {
        return this.calculatePlaceUnsafe() !== null;
    }
    get seed() {
        return this._currentDuelRecord.seed;
    }
    get currentDuelRecord() {
        return this._currentDuelRecord;
    }
    get duelRecords() {
        return this._duelRecords;
    }
    get edoBanListHash() {
        return this._edoBanListHash;
    }
    get banListName() {
        return YGOProBanListMemoryRepository_1.default.findByHash(this.banListHash)?.name ?? null;
    }
    waiting() {
        // Keep the DuelState label (_state, read by toRoomListDTO and
        // DisconnectHandler) and the actual state object (_roomState, whose
        // handleJoin() decides player-vs-spectator) moving together — every
        // other transition (rps/choosingOrder/dueling/sideDecking) sets _state
        // alongside swapping _roomState, so waiting() must too.
        this._state = YgoRoom_1.DuelState.WAITING;
        this._roomState?.removeAllListener();
        // A room can re-enter waiting after an aborted duel (setDuelFinished,
        // ocgcore error) with both players still seated, isStart="start", and
        // stale isReady flags from the crashed duel. Reset both here so a bare
        // TRY_START can't silently re-arm a new duel — everyone must re-ready
        // deliberately. No-op on first entry (no players seated yet, isStart is
        // already "waiting" from the constructor) and on every other normal
        // waiting() call site (room creation), so this is safe to run
        // unconditionally.
        this.isStart = "waiting";
        this._players.forEach((player) => player.notReady());
        const userProfileRepo = new UserProfilePostgresRepository_1.UserProfilePostgresRepository();
        const admitToRoom = new AdmitToRoom_1.AdmitToRoom(new CredentialResolver_1.CredentialResolver(userProfileRepo, new UserAuth_1.UserAuth(userProfileRepo), this._logger), new RoomAdmission_1.RoomAdmission(), this._logger);
        this._roomState = new YGOProWaitingState_1.YGOProWaitingState(admitToRoom, this.emitter, this._logger, new YGOProDeckCreator_1.YGOProDeckCreator(this._cardRepository, this._deckRules, this._logger), this.createDeckValidator());
    }
    /**
     * Resolves the account identity behind a mid-duel JOIN, with the same
     * resolver wiring the WAITING admission uses (waiting(), above). Ranked
     * seats are bound to an account id, so the non-waiting states must know who
     * is knocking BEFORE the reconnect decision — a client-chosen display name
     * alone never identifies anyone. Returns null for a joiner that resolves to
     * no account (guest).
     */
    async resolveJoinerIdentity(socket, playerInfo) {
        const userProfileRepo = new UserProfilePostgresRepository_1.UserProfilePostgresRepository();
        const credentialResolver = new CredentialResolver_1.CredentialResolver(userProfileRepo, new UserAuth_1.UserAuth(userProfileRepo), this._logger);
        const credential = await credentialResolver.resolve(socket, playerInfo);
        return credential.kind === "guest" ? null : credential.userId;
    }
    rps() {
        this._state = YgoRoom_1.DuelState.RPS;
        this._roomState?.removeAllListener();
        this._roomState = new YGOProRockPaperScissorState_1.YGOProRockPaperScissorState(this.emitter, this._logger);
    }
    choosingOrder() {
        this._state = YgoRoom_1.DuelState.CHOOSING_ORDER;
        this._roomState?.removeAllListener();
        this._roomState = new YGOProChoosingOrderState_1.YGOProChoosingOrderState(this.emitter, this._logger);
    }
    dueling() {
        this.assertSeriesPlayersConnected();
        // Create the Duel object to track turn count for Tag rotation
        const banList = YGOProBanListMemoryRepository_1.default.findByHash(this.banListHash);
        this.createDuel(banList?.name ?? null);
        this._state = YgoRoom_1.DuelState.DUELING;
        this.isStart = "start";
        this._roomState?.removeAllListener();
        this._roomState = new YGOProDuelingState_1.YGOProDuelingState(this, this.emitter, this._logger);
    }
    sideDecking() {
        this._state = YgoRoom_1.DuelState.SIDE_DECKING;
        this._roomState?.removeAllListener();
        this._roomState = new YGOProSideDeckingState_1.YGOProSideDeckingState(this.emitter, this._logger, new YGOProDeckCreator_1.YGOProDeckCreator(this._cardRepository, this._deckRules, this._logger), this.createDeckValidator(), this);
    }
    createSpectatorUnsafe(socket, name) {
        const position = ygopro_msg_encode_1.NetPlayerType.OBSERVER;
        const client = new YGOProClient_1.YGOProClient({
            name,
            socket,
            logger: this._logger,
            position,
            host: false,
            id: null,
            team: Team_1.Team.SPECTATOR,
            room: this,
        });
        return client;
    }
    plannedPlace(playerName) {
        if (this.provisioning?.series_finished === true) {
            return null;
        }
        if (!this.provisioning?.players) {
            return undefined;
        }
        const side = ["a", "b"].find((candidate) => this.provisioning.players[candidate] === playerName);
        if (!side) {
            return null;
        }
        const plannedPosition = this.provisioning.seat_plan[side];
        if (plannedPosition === 0) {
            this.restoreTemporarySeatOneOccupantUnsafe();
        }
        // MDPro3 cannot reliably render a waiting lobby whose only player is
        // already in physical seat 1. Let that player create the room in seat 0,
        // then move them to their planned seat immediately before the real
        // seat-0 player is admitted. Duel-time positions remain deterministic.
        const position = (
            plannedPosition === 1 &&
            this.players.length === 0
        ) ? 0 : plannedPosition;
        if (this.players.some((player) => player.position === position)) {
            return null;
        }
        const team = this.t0Positions.includes(position) ? 0 : 1;
        return {
            position,
            team,
        };
    }
    restoreTemporarySeatOneOccupantUnsafe() {
        if (!this.provisioning?.players || !this.provisioning?.seat_plan) {
            return;
        }
        if (this.players.some((player) => player.position === 1)) {
            return;
        }
        const displaced = this.players.find((player) => {
            const side = ["a", "b"].find(
                (candidate) => this.provisioning.players[candidate] === player.name
            );
            return (
                player.position === 0 &&
                side &&
                this.provisioning.seat_plan[side] === 1
            );
        });
        if (!displaced) {
            return;
        }
        const oldPosition = displaced.position;
        this._logger.info(
            `Restoring planned seat 1 for ${displaced.name} before seat 0 joins`
        );
        // This is a server-managed compatibility move, not a player-requested
        // seat change. The already validated fixed deck remains attached to the
        // same player object, so preserve readiness instead of requiring a
        // second UPDATE_DECK that MDPro3 does not send after TYPE_CHANGE.
        const wasReady = displaced.isReady;
        this.broadcastToAll(
            this._messageRepository.playerChangeMessage(oldPosition, 1)
        );
        const team = this.t0Positions.includes(1) ? 0 : 1;
        displaced.playerPosition(1, team);
        this.broadcastToAll(
            this._messageRepository.playerChangeMessage(
                displaced.position,
                wasReady
                    ? ygopro_msg_encode_1.PlayerChangeState.READY
                    : ygopro_msg_encode_1.PlayerChangeState.NOTREADY,
            )
        );
        displaced.sendMessageToClient(
            this._messageRepository.typeChangeMessage(
                displaced.position,
                displaced.host,
            )
        );
    }
    // Adapter that lets AdmitToRoom apply its decision on this room without
    // knowing about sockets or wire messages. Captures the connecting socket and
    // player info so the use case only deals in domain values.
    admissionTarget(socket, playerInfo) {
        return {
            league: this.league,
            freeSeat: () => {
                // A watch joiner ("w,<roomId>") asked for the stands: offering it no
                // seat routes the unchanged admission policy to spectator. Only the
                // seat offer is affected — ranked-guest rejection, league checks and
                // the upstream reservation gate all still apply. The stamp is set
                // server-side from the parsed command and scoped to this room's id.
                if (socket.watchForRoomId === this.id) {
                    return null;
                }
                const plannedPlace = this.plannedPlace(playerInfo.name);
                const place = plannedPlace === undefined
                    ? this.calculatePlaceUnsafe()
                    : plannedPlace;
                return place ? new Seat_1.Seat(place.position, place.team) : null;
            },
            seatPlayer: async (credential, seat) => {
                const userId = credential.kind === "guest" ? null : credential.userId;
                const player = this.buildPlayer(socket, playerInfo.name, userId, seat.position, seat.team);
                player.setCredential(credential);
                this.addPlayerUnsafe(player);
            },
            admitSpectator: async (credential) => {
                const spectator = this.createSpectatorUnsafe(socket, playerInfo.name);
                spectator.setCredential(credential);
                this.addSpectatorUnsafe(spectator);
            },
            rejectAdmission: () => {
                // A client that supplied a PIN but was still turned away authenticated
                // as a guest — its PIN did not resolve to a valid account (wrong PIN,
                // unknown user, or banned). Tell the player WHY before the generic join
                // error, so a mistyped password isn't an opaque failure. Text is sent as
                // a STOC_CHAT (the ygopro path's mechanism, matching the duel/side states).
                if (playerInfo.password) {
                    const chat = new ygopro_msg_encode_1.YGOProStocChat().fromPartial({
                        player_type: ygopro_msg_encode_1.ChatColor.RED,
                        msg: "Invalid username or password.",
                    });
                    socket.send(Buffer.from(chat.toFullPayload()));
                }
                socket.send(this.messageSender.errorMessage(ygopro_msg_encode_1.ErrorMessageType.JOINERROR, 0));
                socket.close();
            },
        };
    }
    /**
     * Reservation gate for the JOIN door, evaluated in EVERY room state. A
     * reserved room rejects third parties from SEATS, but admits watch-stamped
     * spectators: seat security is independent from watchability. The join
     * string alone is deliberately not enough to take a seat in a reserved
     * room: it travels through client config files and process lists, so a
     * seat demands a ticket-resolved identity stamped in the reservation set.
     * A watch-stamped socket ("w,<roomId>") can only ever spectate — the
     * waiting door offers it no seat and the mid-duel states force its
     * spectator branch — and spectators see only the opponent view (no hands),
     * so admitting it grants the stands and nothing more. Internal (bot)
     * sockets are pre-authorized by their consumed room-bound join token — a
     * bot carries no user identity to compare. Everything else (non-watch,
     * non-reserved, non-bot) is still rejected outright. Rooms without
     * reservations admit everyone, exactly as before.
     */
    reservationAdmits(socket) {
        if (!this.reservedUserIds || this.reservedUserIds.length === 0) {
            return true;
        }
        if (socket.internalForRoomId === this.id) {
            return true;
        }
        // A watch stamp is spectate-only capability, never seat capability:
        // seat-taking paths (spectator -> player promotion) must consult
        // reservationPermitsSeat instead of this gate.
        if (socket.watchForRoomId === this.id) {
            return true;
        }
        if (!socket.resolvedUserId || !this.reservedUserIds.includes(socket.resolvedUserId)) {
            return false;
        }
        return !this.holdsLiveSeat(socket.resolvedUserId);
    }
    /**
     * Seat-taking form of the reservation check, for paths that hand an
     * ALREADY-ADMITTED client a seat (spectator -> player promotion via
     * TO_DUEL). Deliberately distinct from reservationAdmits: the JOIN gate
     * also admits watch-stamped sockets, and a watch stamp grants the stands
     * only — reusing it here would let a non-reserved watcher escalate into a
     * reserved seat during the pre-duel window. Only a ticket-resolved
     * identity in the reservation set may sit, and — same one-seat-per-identity
     * rule as the JOIN gate — not while that identity already holds a live
     * seat: a seated player could otherwise re-enter through the watch door on
     * a second connection and TO_DUEL into the opponent's still-free seat.
     * Rooms without reservations permit everyone, exactly as before.
     */
    reservationPermitsSeat(socket) {
        if (!this.reservedUserIds || this.reservedUserIds.length === 0) {
            return true;
        }
        if (!socket.resolvedUserId || !this.reservedUserIds.includes(socket.resolvedUserId)) {
            return false;
        }
        return !this.holdsLiveSeat(socket.resolvedUserId);
    }
    /**
     * One seat per reserved identity, shared by BOTH reservation gates (JOIN
     * admission and seat-taking promotion): a user whose seat is still held by
     * a live socket may not take another, or one player could fill both seats
     * with two connections and squeeze the opponent out. Seat liveness reads
     * socket.closed — the same signal the reconnect paths use — so a crashed
     * player (their seat's socket already closed) can still come back in.
     */
    holdsLiveSeat(userId) {
        return this._players.some((player) => player.id === userId && !player.socket.closed);
    }
    /**
     * Turn a non-reserved joiner away: a red STOC_CHAT explaining why, the real
     * JOINERROR, then a graceful close() so both frames flush before teardown —
     * the same ygopro-flavored reject sequence as admissionTarget's
     * rejectAdmission and the waiting state's name-taken path.
     */
    rejectReservedJoin(socket) {
        const chat = new ygopro_msg_encode_1.YGOProStocChat().fromPartial({
            player_type: ygopro_msg_encode_1.ChatColor.RED,
            msg: "This room is reserved for its matched players.",
        });
        socket.send(Buffer.from(chat.toFullPayload()));
        socket.send(this.messageSender.errorMessage(ygopro_msg_encode_1.ErrorMessageType.JOINERROR, 0));
        socket.close();
    }
    createPlayerUnsafe(socket, name, userId) {
        const plannedPlace = this.plannedPlace(name);
        const place = plannedPlace === undefined
            ? this.calculatePlaceUnsafe()
            : plannedPlace;
        if (!place) {
            return null;
        }
        return this.buildPlayer(socket, name, userId, place.position, place.team);
    }
    buildPlayer(socket, name, userId, position, team) {
        const isHost = !this._players.some((client) => client.host);
        return new YGOProClient_1.YGOProClient({
            name,
            socket,
            logger: this._logger,
            position,
            host: isHost,
            id: userId,
            team,
            room: this,
        });
    }
    addPlayerUnsafe(player) {
        player.sendMessageToClient(this._messageRepository.joinGameMessage(this.hostInfo, this.banListHash));
        this._players.push(player);
        player.socket.roomId = this.id;
        player.sendMessageToClient(this._messageRepository.typeChangeMessage(player.position, player.host));
        this.clients.forEach((_client) => {
            const playerEnterMessageBuffer = this._messageRepository.playerEnterMessage(_client.name, _client.position);
            player.sendMessageToClient(playerEnterMessageBuffer);
            if (_client.deck) {
                const state = _client.isReady ? ygopro_msg_encode_1.PlayerChangeState.READY : ygopro_msg_encode_1.PlayerChangeState.NOTREADY;
                player.sendMessageToClient(this._messageRepository.playerChangeMessage(_client.position, state));
            }
        });
        const playerEnterMessage = this._messageRepository.playerEnterMessage(player.name, player.position);
        this.clients.forEach((_client) => {
            if (_client !== player) {
                _client.sendMessageToClient(playerEnterMessage);
            }
        });
        this.sendSpectatorCount({ enqueue: false });
    }
    addSpectatorUnsafe(spectator) {
        spectator.sendMessageToClient(this._messageRepository.joinGameMessage(this.hostInfo, this.banListHash));
        this._spectators.push(spectator);
        spectator.sendMessageToClient(this._messageRepository.typeChangeMessage(spectator.position, spectator.host));
        this.clients.forEach((_client) => {
            const playerEnterMessageBuffer = this._messageRepository.playerEnterMessage(_client.name, _client.position);
            spectator.sendMessageToClient(playerEnterMessageBuffer);
            if (_client.deck) {
                const state = _client.isReady ? ygopro_msg_encode_1.PlayerChangeState.READY : ygopro_msg_encode_1.PlayerChangeState.NOTREADY;
                spectator.sendMessageToClient(this._messageRepository.playerChangeMessage(_client.position, state));
            }
        });
        this.sendSpectatorCount({ enqueue: false });
    }
    playerToSpectatorUnsafe(player) {
        this.removePlayerUnsafe(player);
        this._spectators.push(player);
        const playerChangeMessageBuffer = this._messageRepository.playerChangeMessage(player.position, ygopro_msg_encode_1.PlayerChangeState.OBSERVE);
        this.broadcastToAll(playerChangeMessageBuffer);
        player.spectatorPosition(ygopro_msg_encode_1.NetPlayerType.OBSERVER);
        player.notReady();
        player.sendMessageToClient(this._messageRepository.typeChangeMessage(player.position, player.host));
        this.sendSpectatorCount({ enqueue: false });
    }
    spectatorToPlayerUnsafe(player) {
        const plannedPlace = this.plannedPlace(player.name);
        const place = plannedPlace === undefined
            ? this.calculatePlaceUnsafe()
            : plannedPlace;
        if (!place) {
            return;
        }
        this.removeSpectatorUnsafe(player);
        this._players.push(player);
        player.playerPosition(place.position, place.team);
        player.notReady();
        const playerEnterMessageBuffer = this._messageRepository.playerEnterMessage(player.name, player.position);
        this.broadcastToAll(playerEnterMessageBuffer);
        player.sendMessageToClient(this._messageRepository.typeChangeMessage(player.position, player.host));
        this.sendSpectatorCount({ enqueue: false });
    }
    movePlayerToAnotherCellUnsafe(player) {
        if (this.provisioning?.players) {
            return;
        }
        const nextPlace = this.nextAvailablePosition(player.position);
        if (!nextPlace) {
            return;
        }
        const oldPosition = player.position;
        player.notReady();
        this.broadcastToAll(this._messageRepository.playerChangeMessage(oldPosition, nextPlace.position));
        player.playerPosition(nextPlace.position, nextPlace.team);
        this.broadcastToAll(this._messageRepository.playerChangeMessage(player.position, ygopro_msg_encode_1.PlayerChangeState.NOTREADY));
        player.sendMessageToClient(this._messageRepository.typeChangeMessage(player.position, player.host));
    }
    setDecksToPlayer(position, deck) {
        this.mutex.runExclusive(() => {
            this.setDecksToPlayerUnsafe(position, deck);
        });
    }
    setDecksToPlayerUnsafe(position, deck) {
        const client = this._players.find((client) => client.position === position);
        if (!client || !(client instanceof YGOProClient_1.YGOProClient)) {
            return;
        }
        client.ready();
        client.setDeck(deck);
        const message = this._messageRepository.playerChangeMessage(client.position, ygopro_msg_encode_1.PlayerChangeState.READY);
        this.broadcastToAll(message);
    }
    notReadyUnsafe(player) {
        if (player.position === ygopro_msg_encode_1.NetPlayerType.OBSERVER) {
            return;
        }
        player.notReady();
        player.sendMessageToClient(this._messageRepository.playerChangeMessage(player.position, ygopro_msg_encode_1.PlayerChangeState.NOTREADY));
    }
    sendPreviousDuelsHistoricalMessages(spectator) {
        for (const record of this._duelRecords.slice(0, -1)) {
            for (const message of record.toPlayback((message) => this.spectatorView(message))) {
                spectator.sendMessageToClient(Buffer.from(message.toFullPayload()));
            }
        }
    }
    sendCurrentDuelHistoricalMessages(spectator) {
        for (const message of this._currentDuelRecord?.toPlayback((message) => this.spectatorView(message)) || []) {
            spectator.sendMessageToClient(Buffer.from(message.toFullPayload()));
        }
    }
    async playSeriesReplay(spectator) {
        if (!this.seriesReplay ||
            this.provisioning?.series_finished !== true ||
            spectator.seriesReplayStarted) {
            return;
        }
        spectator.seriesReplayStarted = true;
        try {
            for (const [position, name] of this.seriesReplay.players.entries()) {
                spectator.sendMessageToClient(this._messageRepository.playerEnterMessage(
                    name,
                    position,
                ));
            }
            spectator.sendMessageToClient(this._messageRepository.duelStartMessage());
            for (let index = 0; index < this.seriesReplay.duels.length; index++) {
                if (spectator.socket.closed) {
                    return;
                }
                const duel = this.seriesReplay.duels[index];
                for (const frame of duel.spectatorFrames) {
                    spectator.sendMessageToClient(frame);
                }
                if (index + 1 < this.seriesReplay.duels.length) {
                    spectator.sendMessageToClient(
                        this._messageRepository.waitingSideMessage(),
                    );
                    spectator.sendMessageToClient(
                        this._messageRepository.duelStartMessage(),
                    );
                    await new Promise((resolve) => setImmediate(resolve));
                }
            }
            spectator.sendMessageToClient(Buffer.from(
                new ygopro_msg_encode_1.YGOProStocDuelEnd().toFullPayload(),
            ));
            spectator.disconnectGracefully(60000);
        }
        catch (error) {
            this._logger.error(`series replay playback failed: ${String(error)}`);
            spectator.disconnectGracefully();
        }
    }
    spectatorView(message) {
        return SpectatorView_1.spectatorView(
            message,
            this.provisioning?.full_spectator === true,
        );
    }
    currentDuelReplayData() {
        if (!this._currentDuelRecord)
            return null;
        return this._currentDuelRecord.toYrp(this);
    }
    sendDeckCountMessage(client) {
        const toDeckCount = (deck) => {
            const message = new ygopro_msg_encode_1.YGOProStocDeckCount_DeckInfo();
            if (!deck) {
                message.main = 0;
                message.extra = 0;
                message.side = 0;
            }
            else {
                message.main = deck.main.length;
                message.extra = deck.extra.length;
                message.side = deck.side.length;
            }
            return message;
        };
        const displayCountDecks = [0, 1].map((team) => {
            const player = this.getTeamPlayers(team)[0];
            return player.deck;
        });
        const team = client.team;
        const deck = displayCountDecks[team];
        const otherDeck = displayCountDecks[1 - team];
        const message = new ygopro_msg_encode_1.YGOProStocDeckCount().fromPartial({
            player0DeckCount: toDeckCount(deck),
            player1DeckCount: toDeckCount(otherDeck),
        });
        client.sendMessageToClient(Buffer.from(message.toFullPayload()));
    }
    addDuelRecord(duelRecord) {
        this._duelRecords.push(duelRecord);
        this._currentDuelRecord = duelRecord;
    }
    saveMessageToDuelRecord(message) {
        this._currentDuelRecord.messages.push(message);
    }
    reconnect(player, socket) {
        player.socket.removeAllListeners();
        player.setSocket(socket);
        player.reconnecting();
        player.sendMessageToClient(this._messageRepository.joinGameMessage(this.hostInfo, this.banListHash));
        const type = player.host ? player.position | 0x10 : player.position;
        player.sendMessageToClient(this._messageRepository.typeChangeMessageFromType(type));
        this._players.forEach((_player) => {
            const playerEnterMessageBuffer = this._messageRepository.playerEnterMessage(_player.name, _player.position);
            player.sendMessageToClient(playerEnterMessageBuffer);
        });
    }
    createDeckValidator() {
        const banList = YGOProBanListMemoryRepository_1.default.findByHash(this.banListHash);
        return new YGOProDeckValidator_1.YGOProDeckValidator(this._deckRules, banList ?? new YGOProBanList_1.YGOProBanList());
    }
    toPresentation() {
        const lobbyInfo = (0, LobbyDuelFlags_1.getLobbyDuelInfo)(this._hostInfo.duel_rule);
        return {
            roomid: this.id,
            roomname: this.name,
            roomnotes: this.ranked ? "(Mercury-Ranked)" : "(Mercury)",
            roommode: this._hostInfo.mode,
            needpass: this.password.length > 0,
            team1: this.team0,
            team2: this.team1,
            best_of: this.bestOf,
            duel_flag: lobbyInfo.duelFlag,
            forbidden_types: lobbyInfo.forbiddenTypes,
            extra_rules: 0,
            start_lp: this._hostInfo.start_lp,
            start_hand: this._hostInfo.start_hand,
            draw_count: this._hostInfo.draw_count,
            time_limit: this._hostInfo.time_limit,
            rule: this._hostInfo.rule,
            no_check: Boolean(this._hostInfo.no_check_deck),
            no_shuffle: Boolean(this._hostInfo.no_shuffle_deck),
            banlist_hash: this._edoBanListHash ?? this.banListHash,
            istart: this.isStart,
            main_min: 40,
            main_max: 60,
            extra_min: 0,
            extra_max: 15,
            side_min: 0,
            side_max: 15,
            users: this._players.map((player) => ({
                name: player.name.replace(/\0/g, "").trim(),
                pos: player.position,
            })),
        };
    }
    toRoomListDTO() {
        const RULE_LABELS = {
            0: "OCG",
            1: "TCG",
            2: "OCG/TCG",
            3: "Pre-release",
            4: "Anything Goes",
            5: "Anything Goes",
        };
        const started = this.duelState !== YgoRoom_1.DuelState.WAITING;
        const maxPlayers = this.team0 + this.team1;
        const banList = YGOProBanListMemoryRepository_1.default.findByHash(this.banListHash);
        return {
            id: this.id,
            command: this.name,
            status: this.duelState,
            started,
            private: this.password.length > 0,
            canPlay: !started && this._players.length < maxPlayers,
            canWatch: true,
            banlist: banList?.name ?? "No banlist",
            rule: RULE_LABELS[this._hostInfo.rule] ?? "Anything Goes",
            mode: this._hostInfo.mode,
            bestOf: this.bestOf,
            duelRule: this._hostInfo.duel_rule,
            startLp: this._hostInfo.start_lp,
            timeLimit: this._hostInfo.time_limit,
            players: this._players.map((player) => ({
                name: player.name.replace(/\0/g, "").trim(),
                position: player.position,
                team: player.team,
            })),
            maxPlayers,
            spectators: this._spectators.length,
            ranked: this.ranked,
            league: this.league.type,
        };
    }
    // Adds the room's league to the real-time broadcast so the client can place
    // it in the right lobby section (verified / external / casual).
    toRealTimePresentation() {
        return {
            ...super.toRealTimePresentation(),
            league: this.league.type,
        };
    }
    destroy() {
        this.emitter.removeAllListeners();
        this._roomState?.removeAllListener();
        this._players.forEach((client) => {
            client.destroy();
        });
    }
    removeSpectator(spectator) {
        this._spectators = this._spectators.filter((item) => item.socket.id !== spectator.socket.id);
    }
    playerLeave(player) {
        this.removePlayer(player);
        const message = this.messageSender.playerChangeMessage(player.position, ygopro_msg_encode_1.PlayerChangeState.LEAVE);
        this.broadcastToAll(message);
    }
    spectatorLeave(spectator) {
        this.removeSpectator(spectator);
        this.sendSpectatorCount({ enqueue: false });
    }
    setDuelFinished() {
        // The ocgcore-error path lands here with the OCGCore still alive inside
        // the (about to be discarded) dueling state. Every other exit from
        // dueling disposes the core before transitioning away (see
        // YGOProDuelingState.finalizeWithReplays / transitionToSideDecking), so
        // this path must too, or the core leaks. Guarded because
        // setDuelFinished can only meaningfully dispose when the CURRENT state
        // actually is a dueling state.
        if (this._roomState instanceof YGOProDuelingState_1.YGOProDuelingState) {
            this._roomState.disposeCore();
        }
        // Delegate to waiting() instead of flipping the _state label alone.
        // Flipping only the label would leave _roomState pointing at the stale
        // dueling state, so a room coming out of a broken duel (ocgcore error in
        // YGOProDuelingState.handleResponse) would look "waiting" in
        // toRoomListDTO while its live JOIN handler still spectated
        // unconditionally. waiting() tears down the old state's listeners AND
        // rearms real player admission (and resets isStart/isReady).
        this.waiting();
    }
    get messageSender() {
        return this._messageRepository;
    }
    sendSpectatorCount({ enqueue = false }) {
        const message = this._messageRepository.watchChangeMessage(this._spectators.length);
        if (!enqueue) {
            this.broadcastToAll(message);
            return;
        }
        this.mutex.runExclusive(() => {
            this.broadcastToAll(message);
        });
    }
    broadcastToAll(message) {
        this._players.forEach((client) => {
            client.sendMessageToClient(message);
        });
        this._spectators.forEach((client) => {
            client.sendMessageToClient(message);
        });
    }
}
exports.YGOProRoom = YGOProRoom;
//# sourceMappingURL=YGOProRoom.js.map
