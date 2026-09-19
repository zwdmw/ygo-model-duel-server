"""Invitation-only HTTPS/WebSocket gateway; the model APIs stay on loopback."""
import argparse
import asyncio
from collections import deque
import hashlib
import hmac
import json
from pathlib import Path
import time

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from transport import close_stream, relay_stream


class Invitations:
    def __init__(self, path):
        self.path = Path(path)
        self.modified = None
        self.entries = {}

    def authenticate(self, header):
        stamp = self.path.stat().st_mtime_ns
        if stamp != self.modified:
            entries = json.loads(self.path.read_text())
            if not isinstance(entries, dict) or not 1 <= len(entries) <= 128:
                raise ValueError("Invalid invitation store")
            self.entries, self.modified = entries, stamp
        token = header.removeprefix("Bearer ")
        if not header.startswith("Bearer ") or not 32 <= len(token) <= 128:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        for identity, entry in self.entries.items():
            if entry.get("enabled") and hmac.compare_digest(entry["sha256"], digest):
                return identity
        return None


def create_app(invites, api_port=7916, duel_port=7915):
    sessions = {}
    recent = {}
    api_url = f"http://127.0.0.1:{api_port}"

    @web.middleware
    async def authorize(request, handler):
        if request.headers.get("Origin"):
            raise web.HTTPForbidden(text="Use the desktop connector")
        try:
            identity = invites.authenticate(request.headers.get("Authorization", ""))
        except (OSError, ValueError, KeyError, TypeError):
            raise web.HTTPServiceUnavailable(text="Invitation service unavailable")
        if identity is None:
            raise web.HTTPUnauthorized(text="Invitation required")
        now = time.monotonic()
        history = recent.setdefault(identity, deque())
        while history and history[0] < now - 60:
            history.popleft()
        if len(history) >= 120:
            raise web.HTTPTooManyRequests(text="Request limit reached")
        history.append(now)
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    app = web.Application(middlewares=[authorize], client_max_size=128 * 1024)

    async def client_context(application):
        async with ClientSession(timeout=ClientTimeout(total=15), trust_env=False) as client:
            sessions["client"] = client
            yield
    app.cleanup_ctx.append(client_context)

    async def api(request):
        body = None
        if request.method == "POST":
            if request.content_type != "application/json":
                raise web.HTTPUnsupportedMediaType()
            body = await request.read()
        try:
            async with sessions["client"].request(
                request.method, api_url + request.path, data=body,
                headers={"Content-Type": "application/json"}, allow_redirects=False,
            ) as response:
                payload = await response.json()
                if request.path == "/health":
                    # Only expose game state, not process IDs, paths or debug errors.
                    allowed = {"state", "checkpoint_steps", "client_profiles",
                               "deck_selection_api", "policy_mode", "startup_api",
                               "human_seat", "model_deck", "startup_seconds"}
                    payload = {key: value for key, value in payload.items() if key in allowed}
                    payload.update(backend="remote_cpu", transport="wss", room_capacity=1)
                return web.json_response(payload, status=response.status)
        except (OSError, asyncio.TimeoutError, ValueError, ClientError):
            raise web.HTTPServiceUnavailable(text="Model room is not ready")

    active = set()

    async def duel(request):
        if active:
            raise web.HTTPConflict(text="Room occupied; try again after this duel")
        websocket = web.WebSocketResponse(
            heartbeat=20, receive_timeout=900, max_msg_size=65536, compress=False)
        if not websocket.can_prepare(request).ok:
            raise web.HTTPBadRequest(text="WebSocket connection required")
        active.add(websocket)
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", duel_port), timeout=5)
            await websocket.prepare(request)
            await relay_stream(websocket, reader, writer)
        except (OSError, asyncio.TimeoutError, ConnectionError):
            if not websocket.prepared:
                raise web.HTTPServiceUnavailable(text="Model room is not ready")
        finally:
            if writer is not None:
                await close_stream(writer)
            active.discard(websocket)
        return websocket

    async def shutdown(application):
        await asyncio.gather(*(ws.close(code=1001) for ws in list(active)),
                             return_exceptions=True)
    app.on_shutdown.append(shutdown)
    app.router.add_get("/health", api)
    app.router.add_get("/catalog", api)
    app.router.add_post("/prepare", api)
    app.router.add_get("/duel", duel)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--invites", type=Path, required=True)
    parser.add_argument("--port", type=int, default=6006)
    parser.add_argument("--api-port", type=int, default=7916)
    parser.add_argument("--duel-port", type=int, default=7915)
    args = parser.parse_args()
    web.run_app(create_app(Invitations(args.invites), args.api_port, args.duel_port),
                host="0.0.0.0", port=args.port, access_log=None)
