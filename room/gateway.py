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


def create_app(invites, api_port=7916, duel_port=7915, spectator_port=7917,
               control_key=None, control_port=7918, pools=None):
    sessions = {}
    recent = {}
    backends = {"": (api_port, duel_port, spectator_port)}
    for name, ports in (pools or {}).items():
        if (name != "mdpro3-diy" or not isinstance(ports, (list, tuple)) or len(ports) != 3
                or any(type(port) is not int or not 1024 <= port <= 65535 for port in ports)):
            raise ValueError("Invalid whitelisted card pool backend")
        backends[name] = tuple(ports)

    def backend(request):
        name = request.match_info.get("pool", "")
        if name not in backends:
            raise web.HTTPNotFound()
        route = "/" + request.match_info["route"] if name else request.path
        return name, backends[name], route

    @web.middleware
    async def authorize(request, handler):
        if request.headers.get("Origin"):
            raise web.HTTPForbidden(text="Use the desktop connector")
        privileged = request.path.startswith("/control/")
        try:
            if privileged:
                identity = ("owner-control" if control_key and hmac.compare_digest(
                    request.headers.get("Authorization", ""), "Bearer " + control_key) else None)
            else:
                identity = invites.authenticate(request.headers.get("Authorization", ""))
        except (OSError, ValueError, KeyError, TypeError):
            raise web.HTTPServiceUnavailable(text="Invitation service unavailable")
        if identity is None:
            raise web.HTTPUnauthorized(text="Invitation required")
        now = time.monotonic()
        history = recent.setdefault(identity, deque())
        while history and history[0] < now - 60:
            history.popleft()
        if len(history) >= (240 if privileged else 120):
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
        _, ports, route = backend(request)
        body = None
        if request.method == "POST":
            if request.content_type != "application/json":
                raise web.HTTPUnsupportedMediaType()
            body = await request.read()
        try:
            async with sessions["client"].request(
                request.method, f"http://127.0.0.1:{ports[0]}" + route, data=body,
                headers={"Content-Type": "application/json"}, allow_redirects=False,
            ) as response:
                payload = await response.json()
                if route == "/health":
                    # Only expose game state, not process IDs, paths or debug errors.
                    allowed = {"state", "checkpoint_steps", "checkpoint_sha256",
                               "model_label", "default_model_deck", "client_profiles",
                               "deck_selection_api", "policy_mode", "startup_api",
                               "human_seat", "model_deck", "startup_seconds",
                               "spectator_api", "spectator_capacity", "spectators",
                               "spectator_available", "spectator_view",
                               "occupied", "accepting", "card_pool_id", "card_pool_sha256"}
                    payload = {key: value for key, value in payload.items() if key in allowed}
                    payload.update(backend="remote_cpu", transport="wss", room_capacity=1)
                return web.json_response(payload, status=response.status)
        except (OSError, asyncio.TimeoutError, ValueError, ClientError):
            raise web.HTTPServiceUnavailable(text="Model room is not ready")

    active = {name: set() for name in backends}
    spectators = {name: set() for name in backends}

    async def duel(request):
        name, ports, route = backend(request)
        watching = route == "/watch"
        connections = spectators[name] if watching else active[name]
        if len(connections) >= (8 if watching else 1):
            raise web.HTTPConflict(text="Connection capacity reached")
        websocket = web.WebSocketResponse(
            heartbeat=20, receive_timeout=900, max_msg_size=65536, compress=False)
        if not websocket.can_prepare(request).ok:
            raise web.HTTPBadRequest(text="WebSocket connection required")
        connections.add(websocket)
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", ports[2] if watching else ports[1]),
                timeout=5)
            await websocket.prepare(request)
            await relay_stream(websocket, reader, writer)
        except (OSError, asyncio.TimeoutError, ConnectionError):
            if not websocket.prepared:
                raise web.HTTPServiceUnavailable(text="Model room is not ready")
        finally:
            if writer is not None:
                await close_stream(writer)
            connections.discard(websocket)
        return websocket

    async def shutdown(application):
        sockets = set().union(*active.values(), *spectators.values())
        await asyncio.gather(*(ws.close(code=1001) for ws in sockets),
                             return_exceptions=True)
    async def control(request):
        name = request.match_info["name"]
        if not name or any(part in ("", ".", "..") for part in name.split("/")):
            raise web.HTTPNotFound()
        try:
            async with sessions["client"].request(
                    request.method, f"http://127.0.0.1:{control_port}/" + name,
                    params=request.query, data=await request.read(),
                    headers={"Authorization": "Bearer " + control_key,
                             "Content-Type": request.headers.get("Content-Type", "application/json")},
                    allow_redirects=False) as response:
                data = await response.content.read(16 * 1024 * 1024 + 1)
                if len(data) > 16 * 1024 * 1024:
                    raise web.HTTPBadGateway(text="Management response too large")
                # read() may return a partial body; bounded accumulation is required.
                while not response.content.at_eof():
                    data += await response.content.read(65536)
                    if len(data) > 16 * 1024 * 1024:
                        raise web.HTTPBadGateway(text="Management response too large")
                return web.Response(body=data, status=response.status, headers={
                    key: response.headers[key] for key in ("Content-Type", "Content-Disposition")
                    if key in response.headers})
        except (OSError, asyncio.TimeoutError, ClientError):
            raise web.HTTPServiceUnavailable(text="Management agent unavailable")
    app.on_shutdown.append(shutdown)
    app.router.add_get("/health", api)
    app.router.add_get("/catalog", api)
    app.router.add_post("/prepare", api)
    app.router.add_get("/duel", duel)
    app.router.add_get("/watch", duel)
    for route in ("health", "catalog"):
        app.router.add_get("/pools/{pool}/{route:" + route + "}", api)
    app.router.add_post("/pools/{pool}/{route:prepare}", api)
    for route in ("duel", "watch"):
        app.router.add_get("/pools/{pool}/{route:" + route + "}", duel)
    if control_key:
        app.router.add_route("GET", "/control/{name:.+}", control)
        app.router.add_route("POST", "/control/{name:.+}", control)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--invites", type=Path, required=True)
    parser.add_argument("--port", type=int, default=6006)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--control-key", type=Path, default=Path(__file__).resolve().parent / "admin/control.key")
    parser.add_argument("--control-port", type=int, default=7918)
    parser.add_argument("--api-port", type=int, default=7916)
    parser.add_argument("--duel-port", type=int, default=7915)
    parser.add_argument("--spectator-port", type=int, default=7917)
    parser.add_argument("--pool-config", type=Path,
                        default=Path(__file__).resolve().parent / "card-pools.json")
    args = parser.parse_args()
    web.run_app(create_app(Invitations(args.invites), args.api_port, args.duel_port,
                          args.spectator_port,
                          args.control_key.read_text().strip() if args.control_key.exists() else None,
                          args.control_port,
                          json.loads(args.pool_config.read_text()) if args.pool_config.exists() else None),
                host=args.host, port=args.port, access_log=None)
