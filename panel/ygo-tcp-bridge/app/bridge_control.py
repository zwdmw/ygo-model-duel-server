"""Private bridge administration, with persistent password hashes and bounded commands."""
import hashlib
import hmac
import json
from pathlib import Path
import re
import time

from aiohttp import web
from replay_store import atomic_json


class BridgeControl:
    def __init__(self, bridge, config):
        self.bridge = bridge
        self.config = config
        self.key = Path(config["key_file"]).read_text().strip()
        if len(self.key) < 32:
            raise ValueError("Invalid bridge management key")
        self.path = Path(config["state_file"])
        self.hashes = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.apply()
        self.runner = None

    def apply(self):
        known = {room["id"] for room in self.bridge.rooms}
        if set(self.hashes) - known or any(
                not isinstance(v, str) or not re.fullmatch("[0-9a-f]{64}", v)
                for v in self.hashes.values()):
            raise ValueError("Invalid password overlay")
        for room in self.bridge.rooms:
            if room["id"] in self.hashes:
                room["password_sha256"] = self.hashes[room["id"]]
        values = [room["password_sha256"] for room in self.bridge.rooms]
        if len(set(values)) != len(values):
            raise ValueError("Room passwords must be distinct")

    def app(self):
        @web.middleware
        async def authenticate(request, handler):
            if (request.headers.get("Origin") or not hmac.compare_digest(
                    request.headers.get("Authorization", ""), "Bearer " + self.key)):
                raise web.HTTPUnauthorized()
            try:
                return await handler(request)
            except (ValueError, TypeError, KeyError) as error:
                return web.json_response({"error": str(error)}, status=400)

        app = web.Application(middlewares=[authenticate], client_max_size=8192)

        async def status(request):
            return web.json_response({
                "occupied": self.bridge.occupied, "watching": self.bridge.watching,
                "per_ip_limit": self.bridge.per_ip_connections,
                "rooms": [{key: room.get(key) for key in ("id", "profile", "role")}
                          for room in self.bridge.rooms],
                "connections": [{**record["public"], "seconds": int(time.monotonic() - record["since"])}
                                for record in self.bridge.connections.values()],
            })

        async def password(request):
            body = await request.json()
            room = next((r for r in self.bridge.rooms if r["id"] == body.get("room_id")), None)
            value = body.get("password", "")
            if not room or not isinstance(value, str) or not re.fullmatch("[a-zA-Z0-9_-]{8,19}", value):
                raise ValueError("Use 8-19 ASCII letters, digits, underscore or hyphen")
            digest = hashlib.sha256(value.encode()).hexdigest()
            if any(r["id"] != room["id"] and r["password_sha256"] == digest for r in self.bridge.rooms):
                raise ValueError("Each entrance must have a different password")
            proposed = {**self.hashes, room["id"]: digest}
            atomic_json(self.path, proposed)
            self.hashes = proposed
            self.apply()
            return web.json_response({"ok": True, "effective": "next_connection"})

        async def kick(request):
            body = await request.json()
            record = self.bridge.connections.get(body.get("id"))
            if record is None or record["public"].get("role") != "spectator":
                raise ValueError("Only an active spectator connection can be disconnected")
            record["task"].cancel()
            return web.json_response({"ok": True})

        app.router.add_get("/status", status)
        app.router.add_post("/password", password)
        app.router.add_post("/kick", kick)
        return app

    async def start(self):
        self.runner = web.AppRunner(self.app(), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", self.config.get("port", 6010))
        await site.start()

    async def close(self):
        if self.runner:
            await self.runner.cleanup()
