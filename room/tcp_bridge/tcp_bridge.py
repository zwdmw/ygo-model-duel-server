"""Native game TCP ingress. Only authenticated joins may use the upstream invite."""
import argparse
import asyncio
from collections import deque
import hashlib
import hmac
import json
import logging
from pathlib import Path
import re
import signal
import struct
import sys
import time

from aiohttp import ClientError, ClientSession, ClientTimeout, ClientWSTimeout

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from relay import packet
from connector import validate_config
from transport import CHUNK_SIZE, close_stream, relay_stream

LOG = logging.getLogger("agent195m.tcp_bridge")


class Rejected(Exception):
    """A fixed, non-secret reason suitable for an operator log."""


def validate_rooms(rooms):
    if not isinstance(rooms, list) or not 1 <= len(rooms) <= 32:
        raise ValueError("Configure between 1 and 32 rooms")
    ids, passwords = set(), set()
    for room in rooms:
        if not isinstance(room, dict):
            raise ValueError("Invalid room")
        name, digest = room.get("id", ""), room.get("password_sha256", "")
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", name):
            raise ValueError("Invalid room ID")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Room requires a SHA-256 password hash")
        if name in ids or digest in passwords:
            raise ValueError("Duplicate room ID or password")
        ids.add(name)
        passwords.add(digest)
        if room.get("profile") not in ("YGOPro2", "legacy"):
            raise ValueError("Unsupported client profile")
        model_name = room.get("model_name")
        if (not isinstance(model_name, str) or not model_name.strip()
                or len(model_name) > 120 or any(ord(c) < 32 for c in model_name)):
            raise ValueError("Invalid model deck name")
        deck = room.get("model_deck")
        if not isinstance(deck, dict) or set(deck) != {"main", "extra", "side"}:
            raise ValueError("Invalid model deck")
        for section, lower, upper in (("main", 40, 60), ("extra", 0, 15), ("side", 0, 15)):
            cards = deck[section]
            if (not isinstance(cards, list) or not lower <= len(cards) <= upper
                    or any(type(card) is not int or not 0 < card <= 0xffffffff for card in cards)):
                raise ValueError("Invalid model deck cards or size")
    return rooms


class AttemptLimit:
    def __init__(self):
        self.global_history = deque()
        self.hosts = {}

    def allow(self, address):
        now = time.monotonic()
        while self.global_history and self.global_history[0] <= now - 60:
            self.global_history.popleft()
        if len(self.global_history) >= 120:
            return False
        # The global limit also bounds the address table under rotating-IP traffic.
        self.global_history.append(now)
        self.hosts = {host: history for host, history in self.hosts.items()
                      if history and history[-1] > now - 60}
        history = self.hosts.setdefault(address, deque())
        while history and history[0] <= now - 60:
            history.popleft()
        if len(history) >= 10:
            return False
        history.append(now)
        return True


class NativeBridge:
    def __init__(self, config):
        self.endpoint, token = validate_config(config)
        self.headers = {"Authorization": "Bearer " + token}
        self.rooms = validate_rooms(config.get("rooms"))
        self.handshake_timeout = 10
        self.setup_timeout = 25
        self.idle_timeout = 900
        self.session_timeout = 4 * 60 * 60
        self.max_connections = 32
        self.per_ip_connections = 4
        self.active = set()
        self.host_connections = {}
        self.occupied = False
        self.limits = AttemptLimit()
        self.client = None
        self.listener = None

    async def start(self, host="127.0.0.1", port=7915):
        self.client = ClientSession(timeout=ClientTimeout(total=15), trust_env=False)
        try:
            self.listener = await asyncio.start_server(
                self.accept, host, port, limit=65536, backlog=32)
        except BaseException:
            await self.client.close()
            raise
        return self

    async def close(self):
        if self.listener is not None:
            self.listener.close()
            await self.listener.wait_closed()
        tasks = list(self.active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.client is not None:
            await self.client.close()

    def accept(self, reader, writer):
        address = writer.get_extra_info("peername")[0]
        count = self.host_connections.get(address, 0)
        if (len(self.active) >= self.max_connections or count >= self.per_ip_connections
                or not self.limits.allow(address)):
            writer.close()
            return
        self.host_connections[address] = count + 1
        task = asyncio.create_task(self.handle(reader, writer))
        self.active.add(task)

        def done(completed):
            self.active.discard(completed)
            remaining = self.host_connections[address] - 1
            if remaining:
                self.host_connections[address] = remaining
            else:
                del self.host_connections[address]
            if not completed.cancelled() and completed.exception() is not None:
                LOG.error("connection_failed: internal_error")
        task.add_done_callback(done)

    async def handshake(self, reader):
        prefix = []
        total = 0
        name_seen = False
        for _ in range(8):
            header = await reader.readexactly(2)
            size = struct.unpack("<H", header)[0]
            total += size + 2
            if not 1 <= size <= 1024 or total > 4096:
                raise Rejected("malformed_join")
            body = await reader.readexactly(size)
            protocol, payload = body[0], body[1:]
            if protocol == 0x12:
                if not 48 <= len(payload) <= 128:
                    raise Rejected("malformed_join")
                try:
                    field = payload[8:48].decode("utf-16-le")
                except UnicodeDecodeError:
                    raise Rejected("malformed_join") from None
                password, terminator, padding = field.partition("\0")
                if not terminator or padding.strip("\0"):
                    raise Rejected("malformed_join")
                if not re.fullmatch(r"[a-zA-Z0-9_-]{16,19}", password):
                    raise Rejected("authentication_failed")
                digest = hashlib.sha256(password.encode("ascii")).hexdigest()
                room = None
                for candidate in self.rooms:
                    if hmac.compare_digest(candidate["password_sha256"], digest):
                        room = candidate
                if room is None:
                    raise Rejected("authentication_failed")
                return b"".join(prefix), payload, room
            if protocol == 0x10 and len(payload) == 40 and not name_seen:
                prefix.append(header + body)
                name_seen = True
            elif protocol == 0x17:
                prefix.append(header + body)
            else:
                raise Rejected("malformed_join")
        raise Rejected("malformed_join")

    async def request_json(self, path, payload=None):
        async with self.client.request(
            "GET" if payload is None else "POST", self.endpoint + path,
            json=payload, headers=self.headers, allow_redirects=False,
        ) as response:
            if response.status != 200:
                raise Rejected("upstream_unavailable")
            raw = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                raw.extend(chunk)
                if len(raw) > 65536:
                    raise Rejected("upstream_invalid_response")
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise Rejected("upstream_invalid_response") from None
            if not isinstance(value, dict):
                raise Rejected("upstream_invalid_response")
            return value

    async def open_room(self, room):
        health = await self.request_json("/health")
        if health.get("state") != "ready":
            raise Rejected("room_busy")
        # The native lobby collects the real human deck after join. The backend
        # replaces this valid placeholder with the actual CTOS_UPDATE_DECK.
        prepared = await self.request_json("/prepare", {
            "profile": room["profile"], "human_name": "Room selection",
            "model_name": room["model_name"],
            "human_deck": room["model_deck"], "model_deck": room["model_deck"],
        })
        password = prepared.get("password", "")
        if not isinstance(password, str) or not re.fullmatch(r"D:[0-9a-f]{16}", password):
            raise Rejected("upstream_invalid_response")
        websocket = await self.client.ws_connect(
            self.endpoint + "/duel", headers=self.headers, heartbeat=20,
            timeout=ClientWSTimeout(ws_close=3), max_msg_size=65537, compress=0)
        return password, websocket

    async def connect_upstream(self, room, reader):
        setup = asyncio.create_task(self.open_room(room))
        read = asyncio.create_task(reader.read(65536))
        buffered = bytearray()
        delivered = False
        try:
            while True:
                done, _ = await asyncio.wait((setup, read), return_when=asyncio.FIRST_COMPLETED)
                if read in done:
                    data = read.result()
                    if not data:
                        raise Rejected("client_disconnected")
                    buffered.extend(data)
                    if len(buffered) > 65536:
                        raise Rejected("setup_buffer_limit")
                if setup in done:
                    password, websocket = setup.result()
                    break
                if read in done:
                    read = asyncio.create_task(reader.read(65537 - len(buffered)))
            read.cancel()
            await asyncio.gather(read, return_exceptions=True)
            delivered = True
            return password, websocket, buffered
        finally:
            if not delivered:
                read.cancel()
                setup.cancel()
                results = await asyncio.gather(setup, read, return_exceptions=True)
                if isinstance(results[0], tuple):
                    await results[0][1].close()

    async def handle(self, reader, writer):
        reserved = False
        websocket = None
        room_id = "-"
        joined = False
        try:
            prefix, join, room = await asyncio.wait_for(
                self.handshake(reader), self.handshake_timeout)
            room_id = room["id"]
            if self.occupied:
                raise Rejected("room_busy")
            self.occupied = reserved = True
            password, websocket, buffered = await asyncio.wait_for(
                self.connect_upstream(room, reader), self.setup_timeout)
            join = join[:8] + password.encode("utf-16-le").ljust(40, b"\0") + join[48:]
            await asyncio.wait_for(
                websocket.send_bytes(prefix + packet(0x12, join)), 5)
            for offset in range(0, len(buffered), CHUNK_SIZE):
                await asyncio.wait_for(
                    websocket.send_bytes(buffered[offset:offset + CHUNK_SIZE]), 5)
            joined = True
            LOG.info("connected room=%s profile=%s", room_id, room["profile"])
            await asyncio.wait_for(
                relay_stream(websocket, reader, writer, idle_timeout=self.idle_timeout),
                self.session_timeout)
        except (Rejected, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError,
                ConnectionError, ClientError) as error:
            reason = str(error) if isinstance(error, Rejected) else type(error).__name__
            LOG.info("closed room=%s reason=%s", room_id, reason)
            if not joined:
                try:
                    # STOC_ERROR_MSG, ERRMSG_JOINERROR, generic join failure.
                    writer.write(packet(0x02, struct.pack("<B3xI", 1, 0)))
                    await asyncio.wait_for(writer.drain(), 1)
                except (OSError, asyncio.TimeoutError):
                    pass
        finally:
            try:
                if websocket is not None:
                    await websocket.close()
            finally:
                try:
                    await close_stream(writer)
                finally:
                    if reserved:
                        self.occupied = False


async def serve(config, host, port, duration=None):
    bridge = await NativeBridge(config).start(host, port)
    addresses = [sock.getsockname()[:2] for sock in bridge.listener.sockets]
    LOG.info("listening addresses=%s rooms=%s capacity=1", addresses,
             [room["id"] for room in bridge.rooms])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        if duration is None:
            await stop.wait()
        else:
            await asyncio.wait_for(stop.wait(), duration)
    except asyncio.TimeoutError:
        pass
    finally:
        await bridge.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7915)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--duration", type=float, help="Stop after this many seconds (smoke test)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        NativeBridge(config)
        if args.check:
            print("Configuration valid; no network connections were made.")
        else:
            asyncio.run(serve(config, args.host, args.port, args.duration))
    except (OSError, ValueError, TypeError, KeyError):
        parser.exit(1, "Bridge failed; check private configuration and listening port.\n")
    except KeyboardInterrupt:
        pass
