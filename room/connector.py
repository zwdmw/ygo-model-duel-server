"""Local loopback endpoints for a TLS-verified, invitation-only remote room."""
import asyncio
import hashlib
import json
import time
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from transport import close_stream, relay_stream


def validate_config(config):
    endpoint = config.get("endpoint", "").rstrip("/")
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment or parsed.path):
        raise ValueError("A root HTTPS server address is required")
    token = config.get("token", "")
    if not isinstance(token, str) or not 32 <= len(token) <= 128 or not token.isascii():
        raise ValueError("Invalid invitation")
    return endpoint, token


def fingerprint(config):
    endpoint, token = validate_config(config)
    return hashlib.sha256((endpoint + "\n" + token).encode()).hexdigest()[:24]


async def serve_connector(config, game_port=27915, api_port=27916):
    endpoint, token = validate_config(config)
    identity = fingerprint(config)
    headers = {"Authorization": "Bearer " + token}
    last_used = time.monotonic()
    active = set()

    async with ClientSession(timeout=ClientTimeout(total=20), trust_env=False) as session:
        async def handle_client(reader, writer):
            nonlocal last_used
            task = asyncio.current_task()
            active.add(task)
            try:
                async with session.ws_connect(endpoint + "/duel", headers=headers,
                                              heartbeat=20, max_msg_size=65536,
                                              compress=0) as websocket:
                    await relay_stream(websocket, reader, writer)
            except (OSError, asyncio.TimeoutError, ClientError):
                from relay import packet
                message = "\u8fdc\u7aef\u8fde\u63a5\u5931\u8d25\uff1a\u8bf7\u68c0\u67e5\u623f\u95f4\u72b6\u6001\u548c\u9080\u8bf7\u7801\u3002"
                try:
                    writer.write(packet(0x19, b"\x08\0" + message.encode("utf-16-le") + b"\0\0"))
                    await writer.drain()
                except (OSError, ConnectionError):
                    pass
            finally:
                await close_stream(writer)
                active.discard(task)
                last_used = time.monotonic()

        @web.middleware
        async def local_only(request, handler):
            nonlocal last_used
            if (request.headers.get("Origin")
                    or request.host != f"127.0.0.1:{api_port}"):
                raise web.HTTPForbidden()
            last_used = time.monotonic()
            return await handler(request)

        async def info(request):
            return web.json_response({"connector": 1, "endpoint": endpoint,
                                      "fingerprint": identity, "game_port": game_port})

        async def forward(request):
            data = None
            if request.method == "POST":
                if request.content_type != "application/json":
                    raise web.HTTPUnsupportedMediaType()
                data = await request.read()
            try:
                async with session.request(
                    request.method, endpoint + request.path, data=data,
                    headers={**headers, "Content-Type": "application/json"},
                    allow_redirects=False,
                ) as response:
                    try:
                        result = await response.json()
                    except (ValueError, ClientError):
                        messages = {401: "Invitation rejected", 409: "Room occupied",
                                    429: "Too many requests"}
                        result = {"error": messages.get(response.status, "Remote service unavailable")}
                    return web.json_response(result, status=response.status)
            except (OSError, asyncio.TimeoutError, ClientError):
                return web.json_response({"error": "Cannot reach the remote room"}, status=503)

        app = web.Application(middlewares=[local_only], client_max_size=128 * 1024)
        app.router.add_get("/connector", info)
        app.router.add_get("/health", forward)
        app.router.add_get("/catalog", forward)
        app.router.add_post("/prepare", forward)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "127.0.0.1", api_port).start()
            async with await asyncio.start_server(handle_client, "127.0.0.1", game_port):
                while active or time.monotonic() - last_used < 600:
                    await asyncio.sleep(2)
        finally:
            for task in list(active):
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            await runner.cleanup()
