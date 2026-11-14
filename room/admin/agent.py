"""Rental-side file and room API. No shell, arbitrary paths or process commands."""
import argparse
import asyncio
from contextlib import suppress
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sys
import tempfile
import threading
import time

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from common import Audit, Metrics, safe_path, tail
from duel_stats import DuelStatistics
from deck_editor import DeckCards, canonical_ydk, deck_revision


SESSION = re.compile(r"\d{8}-\d{6}-\d{4}")
DECK_ID = re.compile(r"[0-9a-f]{16}")


class Agent:
    def __init__(self, config):
        self.config = config
        self.root, self.data = Path(config["root"]), Path(config["data"])
        self.app_root = Path(config["app"])
        self.outbound_app = Path(config.get("outbound_app", config["app"])).resolve()
        sys.path.insert(0, str(self.app_root))
        from deck_config import parse_ydk, selection_name, validate_selection
        from replay_store import atomic_json
        self.parse, self.name, self.validate, self.save = parse_ydk, selection_name, validate_selection, atomic_json
        self.key = Path(config["key_file"]).read_text().strip()
        if len(self.key) < 32:
            raise ValueError("Invalid control key")
        self.storage = self.data / "admin/decks"
        self.storage.mkdir(parents=True, exist_ok=True)
        self.audit = Audit(self.data / "admin/agent-audit.jsonl")
        self.metrics = Metrics(self.data)
        self.statistics = DuelStatistics(self.data)
        self.catalog = None
        self.client = None
        self.replay_cache = None
        self.cache_time = 0
        self.model_lock = asyncio.Lock()
        self.deck_lock = asyncio.Lock()
        self.export_lock = asyncio.Lock()
        self.cards = DeckCards(self.app_root / "project/assets/locale/en/cards.cdb",
                               self.root / "admin/card-names.json")
        self.models = None
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from outbound.manager import OutboundManager
        from outbound.pool_assets import PoolCache
        self.pools = PoolCache(self.root, self.app_root, on_change=self.use_pool)
        self.pools.resolve(strict=False)
        self.outbound = OutboundManager(self)
        if config.get("model_catalog"):
            from registered_catalog import RegisteredCatalog
            self.models = RegisteredCatalog(config["model_catalog"], self.app_root)
        elif config.get("model_run"):
            from model_catalog import ModelCatalog
            self.models = ModelCatalog(config["model_run"], self.app_root,
                                       paired_project=config.get("model_project"))

    def use_pool(self, pool):
        """Card names and deck types follow the active external pool."""
        self.cards = DeckCards(pool.path / "cards.cdb" if pool is not None else
                               self.app_root / "project/assets/locale/en/cards.cdb",
                               self.root / "admin/card-names.json")

    @property
    def external_pool(self):
        """The active pool, re-resolved whenever the pointer or manifest changes."""
        return self.pools.resolve(strict=False)

    def require_pool(self):
        """Strict resolution: a damaged pool must stop a duel before it starts."""
        return self.pools.resolve(strict=True)

    @property
    def pool_error(self):
        return self.pools.error

    async def room(self, path, data=None):
        async with self.client.request("POST" if data is not None else "GET",
                self.config.get("room_url", "http://127.0.0.1:7916") + path,
                json=data, headers={"Authorization": "Bearer " + self.key}) as response:
            value = await response.json()
            if response.status != 200:
                raise web.HTTPBadRequest(text=json.dumps({"error": value.get("error", "Room unavailable")}),
                                         content_type="application/json")
            return value

    async def types(self):
        if self.catalog is None:
            catalog = await self.room("/catalog")
            self.catalog = {int(k): v for k, v in catalog["card_types"].items()}
        return self.catalog

    async def deck_types(self):
        return self.external_pool.types if self.external_pool is not None else await self.types()

    def deck(self, identity):
        if identity == "builtin":
            raw = (self.app_root / "project/assets/deck/BestowedDragon.ydk").read_text(encoding="utf-8-sig")
            return {"id": "builtin", "name": "BestowedDragon", "ydk": raw, "builtin": True}
        if not DECK_ID.fullmatch(identity):
            raise web.HTTPNotFound()
        path = safe_path(self.storage, identity + ".json")
        if not path.is_file():
            raise web.HTTPNotFound()
        return json.loads(path.read_text(encoding="utf-8"))

    def decks(self):
        rows = [self.deck("builtin")]
        for path in sorted(self.storage.glob("*.json"))[:128]:
            if DECK_ID.fullmatch(path.stem):
                rows.append(self.deck(path.stem))
        for row in rows:
            row["counts"] = {key: len(cards) for key, cards in self.parse(row.pop("ydk")).items()}
        return rows

    def deck_detail(self, identity):
        row = self.deck(identity)
        return {**row, "revision": deck_revision(row),
                "counts": {key: len(cards) for key, cards in self.parse(row["ydk"]).items()}}

    async def deck_content(self, body):
        name = self.name(body.get("name"))
        raw = body.get("ydk")
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 65536:
            raise ValueError("Invalid YDK file")
        deck = self.validate(self.parse(raw), await self.deck_types())
        return {"name": name, "ydk": canonical_ydk(deck)}

    def recordings(self):
        if self.replay_cache is not None and time.monotonic() - self.cache_time < 3:
            return self.replay_cache
        rows = []
        for folder in sorted((self.data / "sessions").glob("*"), reverse=True):
            if not SESSION.fullmatch(folder.name) or folder.is_symlink():
                continue
            try:
                metadata = json.loads((folder / "session.json").read_text())
                status = json.loads((folder / "replay-status.json").read_text())
            except (OSError, ValueError):
                continue
            entries = status.get("replays", [])
            rows.append({"session": folder.name, "status": status.get("status", "recording"),
                         "started_at": status.get("started_at"), "finalized_at": status.get("finalized_at"),
                         "reason": status.get("reason"), "model": metadata.get("model_label"),
                         "human_deck": metadata.get("human_deck_name", ""),
                         "model_deck": metadata.get("model_deck_name", ""),
                         "available": bool(status.get("recording_available")),
                         "bytes": sum(item.get("bytes", 0) for item in entries)})
            if len(rows) >= 20000:
                break
        self.replay_cache, self.cache_time = rows, time.monotonic()
        return rows

    def app(self):
        @web.middleware
        async def auth(request, handler):
            if request.headers.get("Origin") or not hmac.compare_digest(
                    request.headers.get("Authorization", ""), "Bearer " + self.key):
                raise web.HTTPUnauthorized()
            try:
                response = await handler(request)
            except (ValueError, TypeError, KeyError) as error:
                return web.json_response({"error": str(error)}, status=400)
            except (OSError, ClientError, asyncio.TimeoutError):
                return web.json_response({"error": "Room or storage unavailable"}, status=503)
            return response

        app = web.Application(middlewares=[auth], client_max_size=128 * 1024)
        async def context(application):
            async with ClientSession(timeout=ClientTimeout(total=10), trust_env=False) as client:
                self.client = client
                await self.outbound.continuous.open()
                try:
                    yield
                finally:
                    await self.outbound.continuous.close()
        app.cleanup_ctx.append(context)

        async def status(request):
            try:
                room = await self.room("/health")
            except (ClientError, OSError, asyncio.TimeoutError, web.HTTPException):
                room = {"state": "offline"}
            rows = await asyncio.to_thread(self.recordings)
            return web.json_response({"room": room, "resources": self.metrics.read(),
                                     "replays": {key: sum(r["status"] == key for r in rows)
                                                 for key in ("complete", "incomplete", "recording")},
                                     "replay_bytes": sum(r["bytes"] for r in rows)})

        async def room_action(request):
            data = await request.json()
            if data.get("action") == "switch_model":
                raise ValueError("Use the verified model list to select a model")
            response = await self.room("/manage", data)
            self.audit.add("room." + str(data.get("action")))
            return web.json_response(response)

        async def list_replays(request):
            rows = await asyncio.to_thread(self.recordings)
            text = request.query.get("q", "").casefold()[:120]
            state = request.query.get("status", "")
            date = request.query.get("date", "").replace("-", "")
            selected = [row for row in rows if (not state or row["status"] == state)
                        and (not date or row["session"].startswith(date))
                        and (not text or text in (row["session"] + row["human_deck"] + row["model_deck"]).casefold())]
            page = max(1, min(int(request.query.get("page", "1")), 10000))
            return web.json_response({"items": selected[(page - 1) * 25:page * 25],
                                     "total": len(selected), "page": page, "page_size": 25})

        async def replay(request):
            identity = request.match_info["id"]
            if not SESSION.fullmatch(identity):
                raise web.HTTPNotFound()
            status_path = safe_path(self.data / "sessions", identity + "/replay-status.json")
            if not status_path.is_file():
                raise web.HTTPNotFound()
            saved = json.loads(status_path.read_text())
            if saved.get("status") not in ("complete", "incomplete") or not saved.get("recording_available"):
                raise web.HTTPConflict(text="Recording is not finalized or unavailable")
            entries = saved.get("replays", [])
            item = next((item for item in entries if item.get("archive")), None)
            if item is None:
                raise web.HTTPNotFound()
            path = safe_path(self.data, item["archive"])
            if not path.is_relative_to((self.data / "recordings").resolve()) or path.suffix != ".yrp":
                raise web.HTTPNotFound()
            if path.stat().st_size > 16 * 1024 * 1024:
                raise web.HTTPRequestEntityTooLarge(max_size=16 * 1024 * 1024, actual_size=path.stat().st_size)
            raw = await asyncio.to_thread(path.read_bytes)
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise web.HTTPConflict(text="Recording integrity check failed")
            self.audit.add("replay.download", target=identity)
            return web.Response(body=raw, content_type="application/octet-stream", headers={
                "Content-Disposition": f'attachment; filename="{identity}_{saved["status"]}.yrp"'})

        async def list_decks(request):
            return web.json_response({"items": self.decks()})

        async def deck_detail(request):
            return web.json_response(self.deck_detail(request.match_info["id"]))

        async def cards(request):
            if set(request.query) - {"q", "section", "ids"}:
                raise ValueError("Unknown card search filter")
            ids = None
            if "ids" in request.query:
                raw = request.query["ids"]
                if "q" in request.query or not re.fullmatch(r"[0-9]+(?:,[0-9]+){0,199}", raw):
                    raise ValueError("Invalid card identities")
                ids = list(dict.fromkeys(map(int, raw.split(","))))
                if any(not 0 < code <= 0xFFFFFFFF for code in ids):
                    raise ValueError("Invalid card identities")
            types = await self.deck_types()
            result = await asyncio.to_thread(self.cards.search, types, request.query.get("q", ""),
                                             request.query.get("section", "side"), ids)
            if self.external_pool is not None:
                hosted = await self.types()
                for row in result["items"]:
                    row["hosted_supported"] = row["id"] in hosted
                    row["external_supported"] = (row["supported"] and
                                                row["id"] not in self.external_pool.pending)
                    row["pending_script"] = row["id"] in self.external_pool.pending
                    row["supported"] = row["external_supported"]
            return web.json_response(result)

        async def statistics(request):
            if set(request.query) - {"model", "days", "bucket"}:
                raise ValueError("Unknown statistics filter")
            result = await asyncio.to_thread(self.statistics.report,
                request.query.get("model", "all"), request.query.get("days", "all"),
                request.query.get("bucket", "duration"))
            return web.json_response(result)

        async def outbound(request):
            if request.method == "GET":
                return web.json_response(await asyncio.to_thread(self.outbound.listing))
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected an object")
            if body.get("action") == "start":
                result = await self.outbound.start(body)
            elif body.get("action") == "start_many":
                result = await self.outbound.start_many(body)
            elif body.get("action") == "start_continuous":
                result = await self.outbound.continuous.start(body)
            elif body.get("action") == "stop" and set(body) <= {"action", "session"}:
                result = await self.outbound.stop(body.get("session"))
            else:
                raise ValueError("Invalid outbound action")
            return web.json_response(result)

        async def outbound_stop(request):
            if request.query:
                raise ValueError("Unknown outbound stop option")
            return web.json_response(
                await self.outbound.stop(request.match_info["id"])
            )

        async def outbound_playback(request):
            identity = request.match_info["id"]
            document = await asyncio.to_thread(self.outbound.playback, identity)
            return web.json_response(document)

        async def outbound_download(request):
            if request.match_info["kind"] == "opponent-deck":
                identity = request.match_info["id"]
                if not SESSION.fullmatch(identity):
                    raise web.HTTPNotFound()
                info = await asyncio.to_thread(self.outbound.opponent_decks.inspect, identity, True)
                if not info["available"]:
                    raise ValueError("No verified opponent deck in the completed server replay")
                self.audit.add("outbound.opponent_deck.download", target=identity)
                if len(info.get("items", [])) > 1:
                    import io
                    import zipfile
                    output = io.BytesIO()
                    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                        for item in info["items"]:
                            archive.writestr(f'{identity}_game{item["game"]:02}_opponent.ydk', item["ydk"])
                    return web.Response(body=output.getvalue(), content_type="application/zip", headers={
                        "Content-Disposition": f'attachment; filename="{identity}_opponent_decks.zip"'})
                return web.Response(text=info["ydk"], content_type="text/plain", headers={
                    "Content-Disposition": f'attachment; filename="{identity}_opponent_main-extra.ydk"'})
            if request.match_info["kind"] == "replay":
                fmt = request.query.get("format")
                if fmt not in (None, "yrp1"):
                    raise ValueError("Unknown replay format")
                if fmt == "yrp1":
                    identity = request.match_info["id"]
                    name, raw = await asyncio.to_thread(self.outbound.playable, identity)
                    self.audit.add("outbound.replay.playable", target=identity)
                    return web.Response(body=raw, content_type="application/octet-stream",
                                        headers={"Content-Disposition":
                                                 f'attachment; filename="{name}"'})
            path = await asyncio.to_thread(self.outbound.download,
                                           request.match_info["id"], request.match_info["kind"])
            return web.FileResponse(path, headers={
                "Content-Disposition": f'attachment; filename="{path.name}"'})

        async def outbound_export(request):
            from outbound.exports import build_archive
            if request.query:
                raise ValueError("Unknown export option")
            if self.export_lock.locked():
                raise web.HTTPConflict(text='{"error":"\u6b63\u5728\u6253\u5305\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5"}',
                                       content_type="application/json")
            kind = request.match_info["kind"]
            async with self.export_lock:
                with tempfile.TemporaryFile(dir=self.outbound.root) as stream:
                    cancelled = threading.Event()
                    task = asyncio.create_task(asyncio.to_thread(
                        build_archive, self.outbound, kind, stream, cancelled))
                    try:
                        info = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        cancelled.set()
                        with suppress(Exception):
                            await task
                        raise
                    self.audit.add("outbound.export." + kind, target=str(info["files"]))
                    response = web.StreamResponse(headers={
                        "Content-Type": "application/zip", "Content-Length": str(info["bytes"]),
                        "Content-Disposition": f'attachment; filename="outbound_{kind}_{time.strftime("%Y%m%d-%H%M%S")}.zip"',
                        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
                    await response.prepare(request)
                    while chunk := await asyncio.to_thread(stream.read, 65536):
                        await response.write(chunk)
                    await response.write_eof()
                    return response

        async def list_models(request):
            if self.models is None:
                return web.json_response({"items": [], "available": False})
            result = await asyncio.to_thread(self.models.listing)
            try:
                room = await self.room("/health")
            except (ClientError, OSError, asyncio.TimeoutError, web.HTTPException):
                room = {"state": "offline"}
            result.update(available=True, current={key: room.get(key) for key in
                          ("model_label", "checkpoint_steps", "checkpoint_sha256")},
                          switch=room.get("model_switch", {}),
                          enabled=room.get("model_selection_api") == 1,
                          occupied=room.get("occupied", False),
                          restart_requested=room.get("restart_requested", False))
            return web.json_response(result)

        async def select_model(request):
            body = await request.json()
            if (self.models is None or not isinstance(body, dict)
                    or set(body) != {"confirmation"} or body["confirmation"] != "SWITCH_MODEL"):
                raise ValueError("Model selection confirmation required")
            async with self.model_lock:
                room = await self.room("/health")
                if room.get("restart_requested") or room.get("model_switch", {}).get("state") == "loading":
                    raise ValueError("Another maintenance operation is pending")
                selected = request.match_info["id"]
                if room.get("checkpoint_sha256") == selected:
                    return web.json_response({"ok": True, "unchanged": True})
                model = await asyncio.to_thread(self.models.stage, selected)
                response = await self.room("/manage", {"action": "switch_model",
                    "confirmation": "SWITCH_MODEL", "model": model})
                self.audit.add("model.select", target=selected)
                return web.json_response(response)

        async def upload(request):
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"name", "ydk"}:
                raise ValueError("Expected deck name and YDK content")
            content = await self.deck_content(body)
            async with self.deck_lock:
                if len(list(self.storage.glob("*.json"))) >= 128:
                    raise ValueError("Deck storage limit reached")
                identity = secrets.token_hex(8)
                self.save(self.storage / (identity + ".json"),
                          {"id": identity, **content, "builtin": False})
            self.audit.add("deck.upload", target=identity)
            return web.json_response({"ok": True, "id": identity,
                                      "revision": deck_revision(content)})

        async def edit_deck(request):
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"name", "ydk", "revision"}:
                raise ValueError("Expected deck name, YDK content and revision")
            content = await self.deck_content(body)
            async with self.deck_lock:
                previous = self.deck(request.match_info["id"])
                if previous.get("builtin"):
                    raise ValueError("Save a copy of the built-in deck instead")
                if body["revision"] != deck_revision(previous):
                    return web.json_response({"error": "Deck changed in another session; reopen it before saving"},
                                             status=409)
                self.save(self.storage / (previous["id"] + ".json"),
                          {**previous, **content})
            self.audit.add("deck.edit", target=previous["id"])
            return web.json_response({"ok": True, "id": previous["id"],
                                      "revision": deck_revision(content),
                                      "room_default_unchanged": True})

        async def default(request):
            deck = self.deck(request.match_info["id"])
            response = await self.room("/manage", {"action": "default_deck", "name": deck["name"], "ydk": deck["ydk"]}
                                       if not deck.get("builtin") else {"action": "reset_default"})
            self.audit.add("deck.default", target=deck["id"])
            return web.json_response(response)

        async def download_deck(request):
            deck = self.deck(request.match_info["id"])
            return web.Response(text=deck["ydk"], content_type="text/plain", headers={
                "Content-Disposition": f'attachment; filename="{deck["id"]}.ydk"'})

        async def delete_deck(request):
            async with self.deck_lock:
                deck = self.deck(request.match_info["id"])
                current = await self.room("/manage")
                if deck.get("builtin") or current["default_name"] == deck["name"]:
                    raise ValueError("The built-in or current default deck cannot be deleted")
                safe_path(self.storage, deck["id"] + ".json").unlink()
            self.audit.add("deck.delete", target=deck["id"])
            return web.json_response({"ok": True})

        async def logs(request):
            allowed = {"room": self.root / "room.log", "gateway": self.root / "gateway.log",
                       "service": self.root / "service.log", "warmup": self.data / "warmup.log"}
            name = request.query.get("kind", "room")
            if name not in allowed:
                raise web.HTTPNotFound()
            return web.json_response({"text": tail(allowed[name])})

        app.router.add_get("/status", status)
        app.router.add_post("/room", room_action)
        app.router.add_get("/replays", list_replays)
        app.router.add_get("/replays/{id}/download", replay)
        app.router.add_get("/decks", list_decks)
        app.router.add_get("/decks/{id}", deck_detail)
        app.router.add_get("/cards", cards)
        app.router.add_get("/models", list_models)
        app.router.add_get("/statistics", statistics)
        app.router.add_get("/outbound", outbound)
        app.router.add_post("/outbound", outbound)
        app.router.add_post("/outbound/{id}/stop", outbound_stop)
        app.router.add_get("/outbound/export/{kind:replays|decks|loss-decks}", outbound_export)
        app.router.add_get("/outbound/{id}/{kind:replay|trace|opponent-deck}", outbound_download)
        app.router.add_get("/outbound/{id}/playback", outbound_playback)
        app.router.add_post("/models/{id}/select", select_model)
        app.router.add_post("/decks", upload)
        app.router.add_post("/decks/{id}/edit", edit_deck)
        app.router.add_post("/decks/{id}/default", default)
        app.router.add_post("/decks/{id}/delete", delete_deck)
        app.router.add_get("/decks/{id}/download", download_deck)
        app.router.add_get("/logs", logs)
        return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    web.run_app(Agent(config).app(), host="127.0.0.1", port=config.get("port", 7918), access_log=None)
