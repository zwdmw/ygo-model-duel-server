"""One isolated outbound job, with bounded owner controls and durable recovery."""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import secrets
import re
import signal
import socket
import subprocess
import sys


TERMINAL = {"finished", "failed", "stopped"}
ACTIVE = {"warming", "connecting", "joining", "authenticating", "waiting", "siding",
          "selecting_turn", "dueling", "saving"}
SESSION = re.compile(r"\d{8}-\d{6}-\d{4}")


def destination(host, port):
    from server_profiles import host_name
    host = host_name(host)
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("Invalid outbound port")
    rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = list(dict.fromkeys(row[4][0] for row in rows))
    if not addresses:
        raise ValueError("Host did not resolve")
    for value in addresses:
        address = ipaddress.ip_address(value)
        if (not address.is_global or address.is_multicast or address.is_unspecified
                or address.is_reserved):
            raise ValueError("External host must resolve only to public unicast addresses")
    return next((value for value in addresses if ":" not in value), addresses[0])


class OutboundManager:
    def __init__(self, agent):
        self.agent = agent
        self.root = agent.data / "outbound"
        self.sessions = self.root / "sessions"
        self.sessions.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = asyncio.Lock()
        self.process = None
        from .continuous import ContinuousDuels
        from .opponent_deck import OpponentDecks
        self.continuous = ContinuousDuels(self)
        self.opponent_decks = OpponentDecks(self)

    def _read(self, path):
        if not path.is_file():
            return {}
        if path.is_symlink() or path.stat().st_size > 128 * 1024:
            raise ValueError("Invalid outbound metadata")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Invalid outbound object")
        return value

    def _alive(self, job):
        from replay_store import process_stamp
        stamp = job.get("worker")
        if not isinstance(stamp, dict):
            return False
        if self.process is not None:
            self.process.poll()
        return process_stamp(stamp.get("pid", -1)) == stamp

    def current(self):
        job = self._read(self.root / "current.json")
        if not job:
            return {"state": "idle"}
        if not isinstance(job.get("session"), str) or not SESSION.fullmatch(job["session"]):
            raise ValueError("Invalid external session metadata")
        folder = self.sessions / job["session"]
        status = self._read(folder / "status.json") or {"state": "warming"}
        if status.get("state") not in TERMINAL and not self._alive(job):
            reason = "stopped_by_owner" if job.get("stop_requested") else "worker_exited"
            status.update(state="stopped" if job.get("stop_requested") else "failed",
                          outcome="incomplete", error=reason,
                          updated_at=datetime.now(timezone.utc).isoformat())
            self.agent.save(folder / "status.json", status)
            saved = self._read(folder / "replay-status.json")
            if not saved.get("finalized_at"):
                saved.update(status="incomplete", reason=reason,
                             finalized_at=status["updated_at"], recording_available=False,
                             trace_available=(folder / "network.jsonl").exists())
                self.agent.save(folder / "replay-status.json", saved)
        return {key: value for key, value in {**job, **status}.items()
                if key != "worker"}

    def listing(self):
        current = self.current()
        rows = []
        for folder in sorted(self.sessions.iterdir(), reverse=True)[:5000]:
            if not SESSION.fullmatch(folder.name) or not folder.is_dir() or folder.is_symlink():
                continue
            metadata = self._read(folder / "session.json")
            if not metadata:
                continue
            status = self._read(folder / "status.json")
            result = self._read(folder / "result.json")
            replay = self._read(folder / "replay-status.json")
            rows.append({"session": folder.name, "model": metadata.get("model_label"),
                         "model_id": metadata.get("checkpoint_sha256"),
                         "host": metadata.get("host"), "port": metadata.get("port"),
                         "deck": metadata.get("model_deck_name"), "started_at": metadata.get("started_at"),
                         "state": status.get("state", "unknown"),
                         "outcome": result.get("outcome", status.get("outcome", "recording")),
                         "error": result.get("error", status.get("error", "")),
                         "decisions": result.get("decisions", status.get("decisions", 0)),
                         "scope": result.get("result_scope", "duel"),
                         "game_count": len(result.get("games", [])),
                         "seconds": result.get("wall_seconds"),
                         "went_first": result.get("went_first"),
                         "replay_available": replay.get("recording_available", False),
                         "trace_available": (folder / "network.jsonl").is_file(),
                         "selftest": result.get("selftest", metadata.get("selftest", False))})
        eligible = [row for row in rows if not row["selftest"]]
        counts = {name: sum(row["outcome"] == name for row in eligible)
                  for name in ("win", "loss", "draw", "incomplete")}
        settled = counts["win"] + counts["loss"] + counts["draw"]
        counts.update(settled=settled, win_rate=counts["win"] / settled if settled else None)
        for row in rows[:25]:
            opponent = self.opponent_decks.inspect(row["session"])
            row["opponent_deck"] = {key: value for key, value in opponent.items() if key not in ("ydk", "items")}
        pool = self.agent.external_pool
        return {"current": current, "continuous": self.continuous.public(),
                "items": rows[:25], "statistics": counts, "single_duel_only": False,
                "card_pool": pool.public() if pool is not None else None,
                "pool_error": self.agent.pool_error}

    def ensure_idle(self):
        if (self.continuous.active() or self.current()["state"] in ACTIVE
                or self._alive(self._read(self.root / "current.json"))):
            raise ValueError("An external session or continuous task is already active")

    def new_session(self):
        for _ in range(100):
            session = datetime.now().strftime("%Y%m%d-%H%M%S-") + f"{secrets.randbelow(10000):04}"
            if not (self.sessions / session).exists():
                return session
        raise RuntimeError("Cannot allocate an external session")

    async def prepare(self, body):
        from server_profiles import valid_password, account_password, validated_fields
        from .preference import prefer_first
        allowed = {"action", "host", "port", "password", "model_id", "deck_id",
                   "preference", "nickname", "version", "deck_source",
                   "auth_mode", "account_name", "account_password"}
        if set(body) - allowed:
            raise ValueError("Unknown outbound setting")
        password = valid_password(body.get("password", ""))
        source, auth = body.get("deck_source", "local"), body.get("auth_mode", "none")
        validated_fields({"name": "External", "host": body.get("host", "example.org"),
                          "port": body.get("port", 233), "deck_source": source,
                          "auth_mode": auth, "account_name": body.get("account_name", "")})
        secret = account_password(body.get("account_password", ""))
        if auth != "none" and not secret:
            raise ValueError("Account password is required")
        nickname = body.get("account_name") if auth != "none" else body.get("nickname", "ModelBot")
        if (not isinstance(nickname, str) or not nickname or
                len(nickname.encode("utf-16-le")) > 38 or
                any(ord(c) < 32 or ord(c) == 127 for c in nickname)):
            raise ValueError("Invalid player nickname")
        version = body.get("version", 0x1362)
        if version not in (0x1362, 0x133E):
            raise ValueError("Unsupported protocol version")
        if auth == "duelchronicle" and version != 0x1362:
            raise ValueError("DuelChronicle requires protocol 1.036.2")
        preference = body.get("preference", "auto")
        prefer_first([], preference)
        if self.agent.models is None:
            raise ValueError("Verified model catalog is unavailable")
        pool = self.agent.require_pool()
        deck = (await self._local_deck(body) if source == "local" else
                {"name": "\u670d\u52a1\u5668\u968f\u673a\u5361\u7ec4", "ydk": None})
        await asyncio.wait_for(asyncio.to_thread(destination, body.get("host"), body.get("port")), 8)
        async with self.agent.model_lock:
            model = await asyncio.to_thread(self.agent.models.stage, body.get("model_id"))
        return {"ydk": deck["ydk"], "config": {
            "app": str(self.agent.app_root), "model": model,
            "display_host": body["host"], "port": body["port"],
            "deck_name": deck["name"], "deck_source": source,
            "auth_mode": auth, "account_password": secret if auth != "none" else "",
            "preference": preference, "nickname": nickname,
            "password": password, "version": version,
            "pool": pool.descriptor() if pool is not None else None,
        }}

    async def _local_deck(self, body):
        async with self.agent.deck_lock:
            deck = self.agent.deck(body.get("deck_id"))
            await self.agent.deck_content({"name": deck["name"], "ydk": deck["ydk"]})
            parsed = self.agent.parse(deck["ydk"])
            types = await self.agent.deck_types()
            tokens = sorted({code for section in parsed.values() for code in section
                             if types.get(code, 0) & 0x4000})
            if tokens:
                names = self.agent.cards.search(types, ids=tokens)["items"]
                detail = ", ".join(f'{row["name"]} ({row["id"]})' for row in names)
                raise ValueError("\u5361\u7ec4\u542b\u4e0d\u80fd\u6295\u5165\u7684\u884d\u751f\u7269: " + detail +
                                 "\u3002\u8bf7\u9009\u62e9\u4fee\u8ba2\u5361\u7ec4\u6216\u5148\u79fb\u9664\u884d\u751f\u7269\u3002")
            pool = self.agent.external_pool
            pending = set().union(*map(set, parsed.values())) & pool.pending if pool is not None else set()
            if pending:
                raise ValueError("\u65b0\u5361\u5c1a\u65e0\u53ef\u9a8c\u8bc1\u7684\u6548\u679c\u811a\u672c: " +
                                 ", ".join(map(str, sorted(pending))))
        return deck

    async def launch(self, prepared, session, continuous_id=None):
        from replay_store import process_stamp
        from .preference import prefer_first
        folder = self.sessions / session
        folder.mkdir(mode=0o700)
        if prepared["ydk"] is not None:
            (folder / "model.ydk").write_text(prepared["ydk"], encoding="utf-8")
        config = {**prepared["config"], "output": str(folder), "seed": secrets.randbelow(2**31)}
        self.agent.save(folder / "session.json", {
            **config["model"], "session": session, "source": "external",
            "host": config["display_host"], "port": config["port"], "model_deck_name": config["deck_name"],
            "deck_source": config.get("deck_source", "local"),
            "started_at": datetime.now(timezone.utc).isoformat(), "selftest": False})
        config["address"] = await asyncio.wait_for(
            asyncio.to_thread(destination, config["display_host"], config["port"]), 8)
        remote = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join((str(remote), str(self.agent.app_root),
                                            str(self.agent.app_root / "project")))
        with (folder / "worker.log").open("wb") as log:
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-m", "outbound.run"], cwd=self.agent.app_root / "project",
                stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, env=env,
                start_new_session=True)
        try:
            job = {"session": session, "state": "warming", "model": config["model"]["model_label"],
                   "host": config["display_host"], "port": config["port"], "deck": config["deck_name"],
                   "preference": config["preference"], "worker": process_stamp(self.process.pid),
                   "continuous_id": continuous_id,
                   "preferred_first": (None if prepared["ydk"] is None and config["preference"] == "auto"
                                       else prefer_first(self.agent.parse(prepared["ydk"])["main"]
                                                         if prepared["ydk"] is not None else [],
                                                         config["preference"]))}
            # Commit PID ownership before stdin allows the worker to connect.
            self.agent.save(self.root / "current.json", job)
            self.process.stdin.write(json.dumps(config).encode("utf-8"))
            self.process.stdin.close()
        except BaseException:
            with suppress(OSError):
                self.process.stdin.close()
            with suppress(ProcessLookupError):
                self.process.terminate()
            self.process.wait(timeout=10)
            raise
        self.agent.audit.add("outbound.start", target=session)
        return self.current()

    async def start(self, body):
        async with self.lock:
            self.ensure_idle()
            return await self.launch(await self.prepare(body), self.new_session())

    async def stop(self):
        async with self.lock:
            self.continuous.request_stop()
            job = self._read(self.root / "current.json")
            if not self._alive(job):
                await self.continuous.tick_locked()
                return self.current()
            pid = job["worker"]["pid"]
            command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if b"outbound.run" not in command:
                raise ValueError("Outbound process identity changed")
            job["stop_requested"] = True
            self.agent.save(self.root / "current.json", job)
            os.kill(pid, signal.SIGTERM)
            for _ in range(40):
                if not self._alive(job):
                    break
                await asyncio.sleep(0.1)
            if self._alive(job):
                os.kill(pid, signal.SIGKILL)
                if self.process is not None:
                    await asyncio.to_thread(self.process.wait, timeout=5)
            self.agent.audit.add("outbound.stop", target=job["session"])
            await self.continuous.tick_locked()
            return self.current()

    def playback(self, identity):
        """Frames of one recorded duel, rebuilt from its own network journal."""
        from common import safe_path
        from . import playback as playback_module
        if not SESSION.fullmatch(identity):
            raise ValueError("Invalid external session")
        folder = safe_path(self.sessions, identity)
        if not folder.is_dir():
            raise ValueError("Unknown external session")
        seat = 1
        for name in ("result.json", "session.json"):
            document = self._read(folder / name)
            if document.get("model_seat") in (0, 1):
                seat = int(document["model_seat"])
                break
            if document.get("human_seat") in (0, 1):
                seat = 1 - int(document["human_seat"])
                break
        return playback_module.build(folder, seat)

    def playable(self, identity):
        """Bytes of a replay the native clients (MDPro3 / YGOPro2) can open.

        External replays arrive in the extended yrp2 container, which native
        clients misread; `replay_convert` rewrites the container into plain yrp1.
        """
        from common import safe_path
        from .exports import recorded_replays
        from . import replay_convert
        if not SESSION.fullmatch(identity):
            raise ValueError("Invalid external session")
        folder = safe_path(self.sessions, identity)
        recordings = recorded_replays(self, folder)
        if not recordings:
            raise ValueError("The external server did not provide a YRP replay")
        raw = recordings[0][1]
        if raw[:4] == b"yrp2":
            raw = replay_convert.to_playable(raw)
        return "%s_game01_playable.yrp" % identity, raw

    def download(self, identity, kind):
        from common import safe_path
        if not SESSION.fullmatch(identity):
            raise ValueError("Invalid external session")
        folder = safe_path(self.sessions, identity)
        if kind == "trace":
            path = safe_path(folder, "network.jsonl")
            limit = 9 * 1024 * 1024
        elif kind == "replay":
            from .exports import recorded_replays
            import tempfile
            import zipfile
            saved = self._read(folder / "replay-status.json")
            if not saved.get("recording_available"):
                raise ValueError("The external server did not provide a YRP replay")
            recordings = recorded_replays(self, folder)
            if len(recordings) > 1:
                target = folder / "match-replays.zip"
                with tempfile.NamedTemporaryFile(dir=folder, suffix=".zip", delete=False) as output:
                    temporary = Path(output.name)
                try:
                    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
                        for i, (_, raw) in enumerate(recordings):
                            archive.writestr(f"{identity}_game{i + 1:02}.yrp", raw)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
                return target
            path = safe_path(self.root, saved["replays"][0]["archive"])
            limit = 16 * 1024 * 1024
        else:
            raise ValueError("Unknown download type")
        if not path.is_file() or path.stat().st_size > limit:
            raise ValueError("Missing or oversized external recording")
        return path
