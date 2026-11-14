"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.EvrpSerializer = void 0;
const node_zlib_1 = require("node:zlib");
const evrp_constants_1 = require("./evrp-constants");
class EvrpSerializer {
    static serialize(room, records) {
        const now = new Date().toISOString();
        const envelope = {
            version: evrp_constants_1.EVRP_VERSION,
            meta: {
                players: room.players.map((player) => ({ name: player.name })),
                hostInfo: room.hostInfo,
                startTime: records[0]?.startTime.toISOString() ?? now,
                endTime: records[records.length - 1]?.endTime?.toISOString() ?? now,
            },
            duels: records.map((record) => ({
                gameNumber: record.gameNumber,
                seed: record.seed,
                players: record.toSwappedPlayers().map((player) => ({
                    name: player.name,
                })),
                startTime: record.startTime.toISOString(),
                endTime: record.endTime?.toISOString(),
                winPosition: record.winPosition,
                winReason: record.winReason,
                frames: record.toEvrpFrames(),
                spectatorFrames: Array.from(
                    record.toPlayback((message) => room.spectatorView(message)),
                    (message) => Buffer.from(message.toFullPayload()).toString("base64"),
                ),
            })),
        };
        return (0, node_zlib_1.gzipSync)(
            Buffer.from(JSON.stringify(envelope), "utf8"),
        );
    }
    static toFrames(gz) {
        const count = Math.ceil(gz.length / evrp_constants_1.EVRP_CHUNK_BYTES) || 1;
        const frames = [];
        for (let index = 0; index < count; index++) {
            const chunk = gz.subarray(
                index * evrp_constants_1.EVRP_CHUNK_BYTES,
                (index + 1) * evrp_constants_1.EVRP_CHUNK_BYTES,
            );
            const bodyLen = 1 + 1 + 2 + 2 + chunk.length;
            const frame = Buffer.allocUnsafe(2 + bodyLen);
            frame.writeUInt16LE(bodyLen, 0);
            frame[2] = evrp_constants_1.STOC_EVRP_EXPORT;
            frame[3] = evrp_constants_1.EVRP_VERSION;
            frame.writeUInt16LE(index, 4);
            frame.writeUInt16LE(count, 6);
            chunk.copy(frame, 8);
            frames.push(frame);
        }
        return frames;
    }
}
exports.EvrpSerializer = EvrpSerializer;
//# sourceMappingURL=EvrpSerializer.js.map
