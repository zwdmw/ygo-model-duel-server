"""Unprivileged owner web console. Remote operations are explicitly allowlisted."""
import argparse
import asyncio
import base64
import hashlib
import hmac
import io
import json
from pathlib import Path
import re
import secrets
import signal
import ssl
import sys
import tempfile
import time
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web
import pyotp
import qrcode

from auth import RateLimit, Sessions, password_hash, password_matches, verify_second_factor
from common import Audit, Metrics, timestamp
from server_profiles import RevisionConflict, ServerProfiles

SESSION_KEY = web.RequestKey("admin_session", dict)
EXPORT_LIMIT = 256 * 1024 * 1024


class Console:
    def __init__(self, config):
        self.config = config
        self.static = Path(__file__).parent / "static"
        self.user_file = Path(config["user_file"])
        self.user = json.loads(self.user_file.read_text())
        self.data = Path(config["data"])
        self.origin = config["origin"].rstrip("/")
        parsed = urlsplit(self.origin)
        self.secure = parsed.scheme == "https"
        if not self.secure and parsed.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("Public administration requires HTTPS")
        self.sessions, self.limits = Sessions(), RateLimit()
        self.auth_lock = asyncio.Lock()
        self.mutations = asyncio.Lock()
        self.export_lock = asyncio.Lock()
        self.audit = Audit(self.data / "audit.jsonl")
        self.metrics = Metrics(self.data)
        self.client = None
        sys.path.insert(0, str(Path(config["helpers"])))
        from replay_store import atomic_json
        self.save = atomic_json
        self.server_profiles = ServerProfiles(self.data / "outbound/servers.json", self.save)
        self.server_profiles_lock = asyncio.Lock()

    def save_user(self):
        self.save(self.user_file, self.user)

    def cookie(self, response, token):
        response.set_cookie("ygo_admin", token, httponly=True, secure=self.secure,
                            samesite="Strict", path="/", max_age=8 * 3600)

    async def remote(self, target, name, request=None, payload=None):
        if target == "agent":
            base, key = self.config["agent_url"], self.config["agent_key"]
        else:
            base, key = self.config["bridge_url"], self.config["bridge_key"]
        async with self.client.request("POST" if payload is not None else "GET",
                base.rstrip("/") + "/" + name, json=payload,
                params=request.query if request is not None else None,
                headers={"Authorization": "Bearer " + key}, allow_redirects=False) as response:
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > 16 * 1024 * 1024:
                    raise web.HTTPBadGateway(text="Upstream response too large")
                chunks.append(chunk)
            raw = b"".join(chunks)
            return raw, response.status, {k: response.headers[k] for k in
                    ("Content-Type", "Content-Disposition") if k in response.headers}

    async def json_remote(self, target, name):
        try:
            raw, status, _ = await self.remote(target, name)
            if status != 200:
                return {"online": False}
            return {"online": True, **json.loads(raw)}
        except (ClientError, OSError, asyncio.TimeoutError, ValueError):
            return {"online": False}

    def app(self):
        @web.middleware
        async def security(request, handler):
            try:
                if request.host != urlsplit(self.origin).netloc:
                    raise web.HTTPForbidden(text="Invalid host")
                if request.path.startswith("/api/"):
                    if request.method != "GET":
                        if (request.headers.get("Origin") != self.origin or
                                request.headers.get("Sec-Fetch-Site") == "cross-site" or
                                request.content_type != "application/json"):
                            raise web.HTTPForbidden(text="Invalid request origin")
                    if request.path != "/api/login":
                        session = self.sessions.get(request.cookies.get("ygo_admin"))
                        if session is None:
                            raise web.HTTPUnauthorized()
                        request[SESSION_KEY] = session
                        if request.method != "GET" and not hmac.compare_digest(
                                session["csrf"], request.headers.get("X-CSRF-Token", "")):
                            raise web.HTTPForbidden(text="Invalid CSRF token")
                response = await handler(request)
            except web.HTTPException as error:
                if request.path.startswith("/api/"):
                    response = web.json_response({"error": error.reason}, status=error.status)
                else:
                    response = web.Response(status=error.status, text=error.reason)
            except RevisionConflict as error:
                response = web.json_response({"error": str(error)}, status=409)
            except (ValueError, TypeError, KeyError) as error:
                response = web.json_response({"error": str(error)}, status=400)
            except (ClientError, OSError, asyncio.TimeoutError):
                response = web.json_response({"error": "Management service unavailable"}, status=503)
            return response

        async def response_security(request, response):
            response.headers.update({
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                    "img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; "
                    "form-action 'self'; frame-ancestors 'none'; object-src 'none'",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            })
            if self.secure:
                response.headers["Strict-Transport-Security"] = "max-age=31536000"

        app = web.Application(middlewares=[security], client_max_size=128 * 1024)
        app.on_response_prepare.append(response_security)
        async def context(application):
            async with ClientSession(timeout=ClientTimeout(total=12, connect=4), trust_env=False) as client:
                self.client = client
                yield
        app.cleanup_ctx.append(context)

        async def index(request):
            return web.FileResponse(self.static / "index.html")

        async def asset(request):
            name = request.match_info["name"]
            if name not in {"app.js", "app.css", "deck-editor.js", "outbound.js", "replay.js",
                           "lucide.min.js", "logo.png"}:
                raise web.HTTPNotFound()
            return web.FileResponse(self.static / name)

        async def login(request):
            if not self.limits.allow(request.remote or "unknown"):
                raise web.HTTPTooManyRequests()
            body = await request.json()
            async with self.auth_lock:
                valid = await asyncio.to_thread(password_matches, body.get("password"), self.user["password"])
                valid = valid and hmac.compare_digest(str(body.get("username", "")), self.user["username"])
                if not valid or not verify_second_factor(self.user, body.get("otp", "")):
                    self.audit.add("login", False)
                    raise web.HTTPUnauthorized()
                self.save_user()
                token, session = self.sessions.create()
            self.audit.add("login")
            response = web.json_response({"ok": True, "csrf": session["csrf"]})
            self.cookie(response, token)
            return response

        async def me(request):
            return web.json_response({"username": self.user["username"], "csrf": request[SESSION_KEY]["csrf"],
                                      "totp_enabled": bool(self.user.get("totp_secret"))})

        async def logout(request):
            self.sessions.remove(request.cookies.get("ygo_admin"))
            response = web.json_response({"ok": True})
            response.del_cookie("ygo_admin", path="/", secure=self.secure, httponly=True, samesite="Strict")
            return response

        async def status(request):
            agent, bridge = await asyncio.gather(self.json_remote("agent", "status"),
                                                 self.json_remote("bridge", "status"))
            return web.json_response({"sampled_at": timestamp(), "agent": agent, "bridge": bridge,
                                     "relay": self.metrics.read(), "host": self.config.get("game_host"),
                                     "game_port": self.config.get("game_port", 6008)})

        async def proxy(request):
            name = request.match_info["name"]
            if request.method == "GET":
                allowed = (name in ("replays", "decks", "cards", "logs", "models", "statistics") or
                           re.fullmatch(r"outbound/\d{8}-\d{6}-\d{4}/(replay|trace|playback|opponent-deck)", name) or
                           re.fullmatch(r"replays/\d{8}-\d{6}-\d{4}/download", name) or
                           re.fullmatch(r"decks/(builtin|[0-9a-f]{16})", name) or
                           re.fullmatch(r"decks/(builtin|[0-9a-f]{16})/download", name))
                if not allowed:
                    raise web.HTTPNotFound()
                raw, code, headers = await self.remote("agent", name, request)
            else:
                outbound_stop = re.fullmatch(
                    r"outbound/(\d{8}-\d{6}-\d{4})/stop", name
                )
                if not (name in ("room", "decks", "password", "kick") or
                        outbound_stop or
                        re.fullmatch(r"models/[0-9a-f]{64}/select", name) or
                        re.fullmatch(r"decks/(builtin|[0-9a-f]{16})/(default|delete|edit)", name)):
                    raise web.HTTPNotFound()
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError("Expected an object")
                async with self.mutations:
                    raw, code, headers = await self.remote(
                        "bridge" if name in ("password", "kick") else "agent", name, payload=body)
                    target = (outbound_stop.group(1) if outbound_stop else
                              body.get("action") if name == "room" else
                              body.get("room_id", body.get("id")))
                    self.audit.add(name, code == 200, target=target)
            return web.Response(body=raw, status=code, headers=headers)

        async def audit(request):
            return web.json_response({"items": self.audit.recent()})

        async def server_profiles(request):
            async with self.server_profiles_lock:
                if request.method == "GET":
                    return web.json_response(await asyncio.to_thread(self.server_profiles.listing))
                body = await request.json()
                result = await asyncio.to_thread(self.server_profiles.change, body)
                self.audit.add("server." + body["action"], target=(
                    result["selected"] if body["action"] == "save" else body.get("id")))
                return web.json_response(result)

        async def outbound(request):
            if request.method == "GET":
                raw, code, headers = await self.remote("agent", "outbound")
                return web.Response(body=raw, status=code, headers=headers)
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected an object")
            if body == {"action": "stop"}:
                payload = body
            elif body.get("action") in ("start", "start_many", "start_continuous"):
                action = body["action"]
                allowed = {"action", "id", "revision", "preference", "nickname", "version"}
                if action == "start_many":
                    allowed.update(("passwords", "rooms"))
                else:
                    allowed.update(("password", "model_id"))
                if body["action"] == "start_continuous":
                    allowed.add("target")
                    target = body.get("target")
                    if type(target) is not int or (target != -1 and not 1 <= target <= 2147483647):
                        raise ValueError("Target must be a positive integer or -1")
                if set(body) - allowed:
                    raise ValueError("Unknown outbound setting")
                async with self.server_profiles_lock:
                    payload = await asyncio.to_thread(self.server_profiles.connection,
                                                       body.get("id"), body.get("revision"))
                payload.update(action=body["action"], preference=body.get("preference", "auto"),
                               nickname=body.get("nickname", "ModelBot"),
                               version=body.get("version", 0x1362))
                from server_profiles import SHA, valid_password
                if action != "start_many" and "model_id" in body:
                    model_id = body["model_id"]
                    if not isinstance(model_id, str) or not SHA.fullmatch(model_id):
                        raise ValueError("Invalid model identity")
                    payload["model_id"] = model_id
                if body["action"] == "start_continuous":
                    payload["target"] = body["target"]
                if body["action"] == "start_many":
                    if "rooms" in body and "passwords" in body:
                        raise ValueError("Use either rooms or passwords, not both")
                    if "rooms" in body:
                        rooms = body["rooms"]
                        if not isinstance(rooms, list) or not 2 <= len(rooms) <= 6:
                            raise ValueError("Batch start requires 2-6 rooms")
                        normalized = []
                        for room in rooms:
                            if not isinstance(room, dict) or set(room) - {"password", "model_id"}:
                                raise ValueError("Invalid batch room setting")
                            password = valid_password(room.get("password"))
                            model_id = room.get("model_id", payload["model_id"])
                            if not password:
                                raise ValueError("Batch room passwords cannot be empty")
                            if not isinstance(model_id, str) or not SHA.fullmatch(model_id):
                                raise ValueError("Invalid model identity")
                            normalized.append({"password": password, "model_id": model_id})
                        passwords = [room["password"] for room in normalized]
                        payload["rooms"] = normalized
                    else:
                        passwords = body.get("passwords")
                        if (not isinstance(passwords, list)
                                or not 2 <= len(passwords) <= 6):
                            raise ValueError("Batch start requires 2-6 room passwords")
                        passwords = [valid_password(value) for value in passwords]
                        if any(not value for value in passwords):
                            raise ValueError("Batch room passwords cannot be empty")
                        payload["passwords"] = passwords
                    if len(set(passwords)) != len(passwords):
                        raise ValueError("Batch room passwords must be unique")
                    payload.pop("password", None)
                elif "password" in body:
                    payload["password"] = valid_password(body["password"])
            else:
                raise ValueError("Unknown outbound action")
            raw, code, headers = await self.remote("agent", "outbound", payload=payload)
            target = body.get("id")
            if body.get("action") == "start_many":
                target = f"{target}:{len(body.get('rooms', body.get('passwords', [])))}"
            self.audit.add("outbound." + body["action"], code == 200, target=target)
            return web.Response(body=raw, status=code, headers=headers)

        async def outbound_export(request):
            if request.query:
                raise ValueError("Unknown export option")
            if self.export_lock.locked():
                return web.json_response({"error": "\u6b63\u5728\u6253\u5305\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5"},
                                         status=409)
            kind = request.match_info["kind"]
            async with self.export_lock:
                # Stage the whole upstream archive on disk before returning headers,
                # so upstream errors cannot become a silently truncated download.
                with tempfile.TemporaryFile() as stream:
                    async with self.client.get(
                            self.config["agent_url"].rstrip("/") + "/outbound/export/" + kind,
                            headers={"Authorization": "Bearer " + self.config["agent_key"]},
                            timeout=ClientTimeout(total=180, connect=4, sock_read=150),
                            allow_redirects=False) as upstream:
                        if upstream.status != 200:
                            raw = await upstream.content.read(8192)
                            return web.Response(body=raw, status=upstream.status, content_type="application/json")
                        expected = upstream.content_length
                        if (upstream.content_type != "application/zip" or expected is None
                                or not 1 <= expected <= EXPORT_LIMIT):
                            raise web.HTTPBadGateway()
                        size = 0
                        async for chunk in upstream.content.iter_chunked(65536):
                            size += len(chunk)
                            if size > EXPORT_LIMIT:
                                raise web.HTTPBadGateway()
                            await asyncio.to_thread(stream.write, chunk)
                        if size != expected:
                            raise web.HTTPBadGateway()
                    await asyncio.to_thread(stream.seek, 0)
                    self.audit.add("outbound.export." + kind)
                    response = web.StreamResponse(headers={
                        "Content-Type": "application/zip", "Content-Length": str(size),
                        "Content-Disposition": f'attachment; filename="outbound_{kind}_{time.strftime("%Y%m%d-%H%M%S")}.zip"'})
                    await response.prepare(request)
                    while chunk := await asyncio.to_thread(stream.read, 65536):
                        await response.write(chunk)
                    await response.write_eof()
                    return response

        async def security_action(request):
            if not self.limits.allow("security:" + (request.remote or "unknown")):
                raise web.HTTPTooManyRequests()
            body = await request.json()
            async with self.auth_lock:
                if not await asyncio.to_thread(password_matches, body.get("password"), self.user["password"]):
                    raise web.HTTPUnauthorized()
                action = body.get("action")
                if action == "totp_begin":
                    if self.user.get("totp_secret"):
                        raise ValueError("Second factor is already enabled")
                    secret = pyotp.random_base32()
                    request[SESSION_KEY]["pending_totp"] = (secret, time.monotonic())
                    uri = pyotp.TOTP(secret).provisioning_uri(self.user["username"], issuer_name="YGO Room")
                    image = qrcode.make(uri)
                    stream = io.BytesIO()
                    image.save(stream, format="PNG")
                    return web.json_response({"secret": secret,
                        "qr": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()})
                if action == "totp_confirm":
                    secret, started = request[SESSION_KEY].get("pending_totp", ("", 0))
                    if (not secret or time.monotonic() - started > 600 or
                            not pyotp.TOTP(secret).verify(str(body.get("otp", "")), valid_window=1)):
                        raise ValueError("Invalid or expired verification code")
                    codes = [secrets.token_urlsafe(12) for _ in range(8)]
                    self.user.update(totp_secret=secret, last_totp_counter=int(time.time()) // 30,
                                     recovery=[hashlib.sha256(c.encode()).hexdigest() for c in codes])
                    self.save_user()
                    self.sessions.items.clear()
                    token, session = self.sessions.create()
                    self.audit.add("security.totp_enable")
                    response = web.json_response({"ok": True, "recovery_codes": codes, "csrf": session["csrf"]})
                    self.cookie(response, token)
                    return response
                if action not in ("password", "totp_disable"):
                    raise ValueError("Unknown security action")
                if not verify_second_factor(self.user, body.get("otp", "")):
                    raise web.HTTPUnauthorized()
                if action == "password":
                    password = body.get("new_password")
                    if not isinstance(password, str) or not 16 <= len(password) <= 128:
                        raise ValueError("Admin password must have 16-128 characters")
                    self.user["password"] = await asyncio.to_thread(password_hash, password)
                else:
                    self.user.pop("totp_secret", None)
                    self.user.pop("recovery", None)
                    self.user.pop("last_totp_counter", None)
                self.save_user()
                self.sessions.items.clear()
                token, session = self.sessions.create()
                self.audit.add("security." + action)
                response = web.json_response({"ok": True, "csrf": session["csrf"]})
                self.cookie(response, token)
                return response

        app.router.add_get("/", index)
        app.router.add_get("/static/{name}", asset)
        app.router.add_post("/api/login", login)
        app.router.add_get("/api/me", me)
        app.router.add_post("/api/logout", logout)
        app.router.add_get("/api/status", status)
        app.router.add_get("/api/audit", audit)
        app.router.add_get("/api/servers", server_profiles)
        app.router.add_post("/api/servers", server_profiles)
        app.router.add_get("/api/outbound", outbound)
        app.router.add_post("/api/outbound", outbound)
        app.router.add_get("/api/outbound/export/{kind:replays|decks|loss-decks}", outbound_export)
        app.router.add_post("/api/security", security_action)
        app.router.add_get("/api/{name:.+}", proxy)
        app.router.add_post("/api/{name:.+}", proxy)
        return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    context = None
    if config.get("cert_file"):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(config["cert_file"], config["key_file"])
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, lambda *_: context.load_cert_chain(
                config["cert_file"], config["key_file"]))
    web.run_app(Console(config).app(), host=config.get("host", "127.0.0.1"),
                port=config.get("port", 8090), ssl_context=context, access_log=None)
