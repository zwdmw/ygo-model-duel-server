"""One isolated outbound job, with bounded owner controls and durable recovery."""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import re
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import time


TERMINAL = {"finished", "failed", "stopped"}
ACTIVE = {"warming", "connecting", "reconnecting", "joining", "authenticating", "waiting", "siding",
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
        self.jobs = self.root / "jobs"
        self.sessions.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.jobs.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.app_root = Path(getattr(agent, "outbound_app", agent.app_root)).resolve()
        if not self.app_root.is_dir() or self.app_root.is_symlink():
            raise ValueError("Outbound runtime app is unavailable")
        self.max_concurrent = agent.config.get("max_concurrent_outbound_rooms", 6)
        if type(self.max_concurrent) is not int or not 1 <= self.max_concurrent <= 32:
            raise ValueError("Invalid outbound concurrency limit")
        self.policy_python = Path(
            agent.config.get("outbound_policy_python", sys.executable)
        ).resolve()
        if not self.policy_python.is_file() or not os.access(self.policy_python, os.X_OK):
            raise ValueError("Outbound GPU policy Python is unavailable")
        self.policy_memory_fraction = agent.config.get(
            "outbound_gpu_memory_fraction", 0.4
        )
        if (not isinstance(self.policy_memory_fraction, (int, float))
                or not 0.05 <= float(self.policy_memory_fraction) <= 0.9):
            raise ValueError("Invalid outbound GPU memory fraction")
        self.lock = asyncio.Lock()
        self.processes = {}
        self.policy_processes = {}
        self.policy_path = self.root / "policy.json"
        self.policy_socket = self.root / "policy.sock"
        self.policy_root = self.root / "policies"
        self.policy_root.mkdir(parents=True, exist_ok=True, mode=0o700)
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
        session = job.get("session")
        process = self.processes.get(session)
        if process is not None and process.poll() is not None:
            self.processes.pop(session, None)
        return process_stamp(stamp.get("pid", -1)) == stamp

    def _job_path(self, session):
        if not isinstance(session, str) or not SESSION.fullmatch(session):
            raise ValueError("Invalid external session metadata")
        return self.jobs / (session + ".json")

    def _job(self, session):
        job = self._read(self._job_path(session))
        if not job:
            legacy = self._read(self.root / "current.json")
            if legacy.get("session") == session:
                job = legacy
        if job and job.get("session") != session:
            raise ValueError("External job identity changed")
        return job

    def _job_documents(self):
        jobs = {}
        for path in self.jobs.glob("*.json"):
            if path.is_symlink() or not SESSION.fullmatch(path.stem):
                continue
            job = self._read(path)
            if job.get("session") != path.stem:
                raise ValueError("External job identity changed")
            jobs[path.stem] = job
        legacy = self._read(self.root / "current.json")
        session = legacy.get("session")
        if isinstance(session, str) and SESSION.fullmatch(session):
            jobs.setdefault(session, legacy)
        return jobs

    def job_status(self, session, job=None):
        job = self._job(session) if job is None else job
        if not job:
            raise ValueError("Unknown external session")
        folder = self.sessions / session
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

    def active_jobs(self):
        rows = []
        for session, job in self._job_documents().items():
            status = self.job_status(session, job)
            if status.get("state") in ACTIVE and self._alive(job):
                rows.append(status)
        return sorted(rows, key=lambda row: row["session"], reverse=True)

    def current(self):
        active = self.active_jobs()
        return active[0] if active else {"state": "idle"}

    @staticmethod
    def _policy_token(key):
        raw = json.dumps(key, sort_keys=True, separators=(",", ":")).encode("ascii")
        return hashlib.sha256(raw).hexdigest()[:20]

    def _policy_location(self, key):
        token = self._policy_token(key)
        return {
            "token": token,
            "metadata": self.policy_root / (token + ".json"),
            "socket": self.policy_root / (token + ".sock"),
            "runtime": self.policy_root / (token + "-runtime"),
            "log": self.policy_root / (token + ".log"),
        }

    def _policy_records(self):
        records = []
        legacy = self._read(self.policy_path)
        if legacy:
            records.append({
                "token": "legacy",
                "metadata": self.policy_path,
                "socket": self.policy_socket,
                "value": legacy,
            })
        for path in self.policy_root.glob("*.json"):
            if path.is_symlink() or not re.fullmatch(r"[0-9a-f]{20}", path.stem):
                continue
            records.append({
                "token": path.stem,
                "metadata": path,
                "socket": path.with_suffix(".sock"),
                "value": self._read(path),
            })
        return records

    @staticmethod
    def _policy_process_alive(metadata):
        from replay_store import process_stamp
        stamp = metadata.get("worker")
        return (
            isinstance(stamp, dict)
            and process_stamp(stamp.get("pid", -1)) == stamp
        )

    def _policy_alive(self, metadata, policy_socket):
        return self._policy_process_alive(metadata) and policy_socket.is_socket()

    def policy_status(self):
        items = []
        for record in self._policy_records():
            metadata = record["value"]
            process_alive = self._policy_process_alive(metadata)
            socket_ready = process_alive and record["socket"].is_socket()
            if not process_alive:
                continue
            state = "ready" if socket_ready and metadata.get("state") == "ready" else "warming"
            items.append({
                key: value for key, value in {
                    **metadata,
                    "state": state,
                    "pid": metadata.get("pid", metadata.get("worker", {}).get("pid")),
                }.items()
                if key not in ("worker", "socket", "key")
            })
        if not items:
            return {"state": "offline"}
        ready = [row for row in items if row["state"] == "ready" and row.get("backend") == "gpu"]
        result = {
            "state": "ready" if len(ready) == len(items) else "warming",
            "backend": "gpu" if ready else items[0].get("backend"),
            "instances": len(items),
            "models": len({row.get("model_id") for row in items}),
            "items": items,
        }
        if len(items) == 1:
            result.update({key: value for key, value in items[0].items() if key != "state"})
        return result

    def listing(self):
        active = self.active_jobs()
        current = active[0] if active else {"state": "idle"}
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
        count = len(active)
        return {"current": current, "active": active,
                "capacity": {"active": count, "limit": self.max_concurrent,
                             "available": self.max_concurrent - count},
                "policy": self.policy_status(),
                "continuous": self.continuous.public(),
                "items": rows[:25], "statistics": counts, "single_duel_only": False,
                "card_pool": pool.public() if pool is not None else None,
                "pool_error": self.agent.pool_error}

    def ensure_capacity(self, requested=1):
        if type(requested) is not int or requested < 1:
            raise ValueError("Invalid external room capacity request")
        active = self.active_jobs()
        if len(active) + requested > self.max_concurrent:
            raise ValueError(
                f"External room capacity reached "
                f"({len(active)} active, {requested} requested, {self.max_concurrent} maximum)"
            )

    @staticmethod
    def _policy_key(config):
        pool = config.get("pool") or {}
        return {
            "model_id": config["model"]["checkpoint_sha256"],
            "pool_sha256": pool.get("sha256"),
        }

    def _stop_policy(self, metadata, policy_socket, token):
        if not metadata:
            policy_socket.unlink(missing_ok=True)
            return
        from replay_store import process_stamp
        stamp = metadata.get("worker")
        if not isinstance(stamp, dict) or process_stamp(stamp.get("pid", -1)) != stamp:
            policy_socket.unlink(missing_ok=True)
            return
        pid = stamp["pid"]
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if b"outbound.policy_server" not in command:
            raise ValueError("Outbound policy process identity changed")
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            if process_stamp(pid) != stamp:
                break
            time.sleep(0.1)
        if process_stamp(pid) == stamp:
            os.kill(pid, signal.SIGKILL)
        process = self.policy_processes.pop(token, None)
        if process is not None:
            with suppress(Exception):
                process.wait(timeout=5)
        policy_socket.unlink(missing_ok=True)

    def _probe_policy(self, policy_socket, expected):
        from model_snapshot import identity
        from .policy_ipc import PolicyClient
        probe = PolicyClient(policy_socket, expected=identity(expected["model"]))
        try:
            if probe.identity.get("backend") != "gpu":
                raise RuntimeError("Outbound policy did not start on the GPU")
            pool = probe.identity.get("card_pool")
            pool_sha = expected["key"].get("pool_sha256")
            if pool_sha is not None and (
                    not isinstance(pool, dict) or pool.get("sha256") != pool_sha):
                raise RuntimeError("Outbound policy card pool differs")
            return probe.identity
        finally:
            probe.close()

    def ensure_policy(self, config):
        from replay_store import process_stamp
        key = self._policy_key(config)
        expected = {"model": config["model"], "key": key}
        location = self._policy_location(key)
        candidates = [location]
        legacy = self._read(self.policy_path)
        if legacy.get("key") == key:
            candidates.append({
                "token": "legacy",
                "metadata": self.policy_path,
                "socket": self.policy_socket,
                "runtime": self.root / "policy-runtime",
                "log": self.root / "policy.log",
            })
        for candidate in candidates:
            metadata = self._read(candidate["metadata"])
            if self._policy_alive(metadata, candidate["socket"]):
                identity = self._probe_policy(candidate["socket"], expected)
                return str(candidate["socket"]), identity

        metadata = self._read(location["metadata"])
        if metadata and metadata.get("key") != key:
            raise ValueError("Outbound policy metadata identity changed")
        if self._policy_process_alive(metadata) or location["socket"].exists():
            self._stop_policy(metadata, location["socket"], location["token"])

        policy_config = {
            "app": str(self.app_root),
            "model": config["model"],
            "pool": config.get("pool"),
            "socket": str(location["socket"]),
            "output": str(location["runtime"]),
            "seed": secrets.randbelow(2**31),
            "max_clients": self.max_concurrent + 2,
        }
        remote = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        env.update(
            PYTHONPATH=os.pathsep.join((
                str(remote), str(self.app_root), str(self.app_root / "project")
            )),
            JAX_PLATFORMS="cuda",
            XLA_PYTHON_CLIENT_PREALLOCATE="false",
            XLA_PYTHON_CLIENT_MEM_FRACTION=str(self.policy_memory_fraction),
            CUDA_VISIBLE_DEVICES=str(
                self.agent.config.get("outbound_gpu_device", "0")
            ),
            OMP_NUM_THREADS="2",
            OPENBLAS_NUM_THREADS="2",
        )
        with location["log"].open("ab") as log:
            process = subprocess.Popen(
                [str(self.policy_python), "-u", "-m", "outbound.policy_server"],
                cwd=self.app_root / "project",
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        self.policy_processes[location["token"]] = process
        metadata = {
            "state": "warming",
            "backend": "gpu",
            "model_id": key["model_id"],
            "pool_sha256": key["pool_sha256"],
            "key": key,
            "socket": str(location["socket"]),
            "worker": process_stamp(process.pid),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        self.agent.save(location["metadata"], metadata)
        try:
            process.stdin.write(
                json.dumps(policy_config).encode("utf-8")
            )
            process.stdin.close()
            deadline = time.monotonic() + 300
            identity = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(
                        f"Outbound GPU policy failed; see {location['log'].name}"
                    )
                if location["socket"].is_socket():
                    try:
                        identity = self._probe_policy(location["socket"], expected)
                        break
                    except (ConnectionError, OSError, TimeoutError):
                        pass
                time.sleep(0.2)
            if identity is None:
                raise TimeoutError("Shared outbound GPU policy startup timed out")
        except BaseException:
            with suppress(OSError):
                process.stdin.close()
            with suppress(ProcessLookupError):
                process.terminate()
            with suppress(Exception):
                process.wait(timeout=10)
            location["socket"].unlink(missing_ok=True)
            metadata.update(
                state="failed",
                updated_at=datetime.now(timezone.utc).isoformat(),
            )
            self.agent.save(location["metadata"], metadata)
            self.policy_processes.pop(location["token"], None)
            raise
        metadata.update(
            state="ready",
            backend=identity["backend"],
            pid=identity["pid"],
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.agent.save(location["metadata"], metadata)
        self.agent.audit.add("outbound.policy.start", target=key["model_id"])
        return str(location["socket"]), identity

    def new_session(self):
        for _ in range(100):
            session = datetime.now().strftime("%Y%m%d-%H%M%S-") + f"{secrets.randbelow(10000):04}"
            if not (self.sessions / session).exists():
                return session
        raise RuntimeError("Cannot allocate an external session")

    @staticmethod
    def _sync_file(source, destination):
        source = Path(source)
        destination = Path(destination)
        if (not source.is_file() or source.is_symlink()
                or destination.is_symlink()):
            raise ValueError("Outbound runtime model asset is invalid")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
        try:
            shutil.copyfile(source, temporary)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _sync_model(self, model):
        relative = Path(model["checkpoint_file"])
        source = (self.agent.app_root / relative).resolve()
        destination = (self.app_root / relative).resolve()
        if (not source.is_relative_to(self.agent.app_root.resolve())
                or not destination.is_relative_to(self.app_root)):
            raise ValueError("Outbound runtime model path escaped its app")
        self._sync_file(source, destination)
        source_sidecar = source.with_name(source.name + ".json")
        destination_sidecar = destination.with_name(destination.name + ".json")
        if source_sidecar.is_file():
            self._sync_file(source_sidecar, destination_sidecar)
        elif destination_sidecar.exists():
            if destination_sidecar.is_symlink():
                raise ValueError("Outbound runtime model sidecar is invalid")
            destination_sidecar.unlink()

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
            await asyncio.to_thread(self._sync_model, model)
        return {"ydk": deck["ydk"], "config": {
            "app": str(self.app_root), "model": model,
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
        policy_socket, policy_identity = await asyncio.to_thread(
            self.ensure_policy, config
        )
        config["policy_socket"] = policy_socket
        self.agent.save(folder / "session.json", {
            **config["model"], "session": session, "source": "external",
            "host": config["display_host"], "port": config["port"], "model_deck_name": config["deck_name"],
            "deck_source": config.get("deck_source", "local"),
            "started_at": datetime.now(timezone.utc).isoformat(), "selftest": False,
            "policy": {"backend": policy_identity["backend"],
                       "pid": policy_identity["pid"], "shared": True}})
        config["address"] = await asyncio.wait_for(
            asyncio.to_thread(destination, config["display_host"], config["port"]), 8)
        remote = Path(__file__).resolve().parent.parent
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join((str(remote), str(self.app_root),
                                            str(self.app_root / "project")))
        with (folder / "worker.log").open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-u", "-m", "outbound.run"], cwd=self.app_root / "project",
                stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT, env=env,
                start_new_session=True)
        self.processes[session] = process
        try:
            job = {"session": session, "state": "warming", "model": config["model"]["model_label"],
                   "model_id": config["model"]["checkpoint_sha256"],
                   "host": config["display_host"], "port": config["port"], "deck": config["deck_name"],
                   "preference": config["preference"], "worker": process_stamp(process.pid),
                   "continuous_id": continuous_id,
                   "preferred_first": (None if prepared["ydk"] is None and config["preference"] == "auto"
                                       else prefer_first(self.agent.parse(prepared["ydk"])["main"]
                                                         if prepared["ydk"] is not None else [],
                                                         config["preference"]))}
            # Commit PID ownership before stdin allows the worker to connect.
            self.agent.save(self._job_path(session), job)
            self.agent.save(self.root / "current.json", job)
            process.stdin.write(json.dumps(config).encode("utf-8"))
            process.stdin.close()
        except BaseException:
            with suppress(OSError):
                process.stdin.close()
            with suppress(ProcessLookupError):
                process.terminate()
            process.wait(timeout=10)
            self.processes.pop(session, None)
            raise
        self.agent.audit.add("outbound.start", target=session)
        return self.job_status(session, job)

    async def start(self, body):
        async with self.lock:
            self.ensure_capacity()
            prepared = await self.prepare(body)
            self.ensure_capacity()
            return await self.launch(prepared, self.new_session())

    async def start_many(self, body):
        from server_profiles import valid_password
        if "rooms" in body and "passwords" in body:
            raise ValueError("Use either rooms or passwords, not both")
        raw_rooms = body.get("rooms")
        if raw_rooms is not None:
            if (not isinstance(raw_rooms, list)
                    or not 2 <= len(raw_rooms) <= self.max_concurrent):
                raise ValueError(
                    f"Batch start requires 2-{self.max_concurrent} rooms"
                )
            rooms = []
            for row in raw_rooms:
                if not isinstance(row, dict) or set(row) - {"password", "model_id"}:
                    raise ValueError("Invalid batch room setting")
                password = valid_password(row.get("password"))
                model_id = row.get("model_id", body.get("model_id"))
                if not isinstance(model_id, str) or not re.fullmatch(r"[0-9a-f]{64}", model_id):
                    raise ValueError("Invalid model identity")
                rooms.append({"password": password, "model_id": model_id})
        else:
            passwords = body.get("passwords")
            if (not isinstance(passwords, list)
                    or not 2 <= len(passwords) <= self.max_concurrent):
                raise ValueError(
                    f"Batch start requires 2-{self.max_concurrent} room passwords"
                )
            rooms = [
                {"password": valid_password(value), "model_id": body.get("model_id")}
                for value in passwords
            ]
        passwords = [row["password"] for row in rooms]
        if any(not value for value in passwords):
            raise ValueError("Batch room passwords cannot be empty")
        if len(set(passwords)) != len(passwords):
            raise ValueError("Batch room passwords must be unique")
        settings = {
            key: value for key, value in body.items()
            if key not in ("passwords", "rooms")
        }
        async with self.lock:
            self.ensure_capacity(len(rooms))
            prepared_by_model = {}
            for row in rooms:
                model_id = row["model_id"]
                if model_id not in prepared_by_model:
                    prepared_by_model[model_id] = await self.prepare({
                        **settings,
                        "action": "start",
                        "password": row["password"],
                        "model_id": model_id,
                    })
            self.ensure_capacity(len(rooms))
            launched = []
            try:
                for index, row in enumerate(rooms):
                    prepared = prepared_by_model[row["model_id"]]
                    variant = {
                        "ydk": prepared["ydk"],
                        "config": {
                            **prepared["config"],
                            "password": row["password"],
                        },
                    }
                    launched_row = await self.launch(variant, self.new_session())
                    launched_row.update(room_index=index, model_id=row["model_id"])
                    launched.append(launched_row)
            except BaseException:
                for row in reversed(launched):
                    with suppress(Exception):
                        await self._stop_job_locked(row["session"])
                raise
            self.agent.audit.add(
                "outbound.batch.start", target=str(len(launched))
            )
            return {
                "state": "started",
                "started": len(launched),
                "items": launched,
            }

    async def _stop_job_locked(self, session):
        job = self._job(session)
        if not job:
            raise ValueError("Unknown external session")
        if not self._alive(job):
            return self.job_status(session, job)
        pid = job["worker"]["pid"]
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if b"outbound.run" not in command:
            raise ValueError("Outbound process identity changed")
        job["stop_requested"] = True
        self.agent.save(self._job_path(session), job)
        if self._read(self.root / "current.json").get("session") == session:
            self.agent.save(self.root / "current.json", job)
        os.kill(pid, signal.SIGTERM)
        for _ in range(40):
            if not self._alive(job):
                break
            await asyncio.sleep(0.1)
        if self._alive(job):
            os.kill(pid, signal.SIGKILL)
        process = self.processes.pop(session, None)
        if process is not None:
            with suppress(Exception):
                await asyncio.to_thread(process.wait, timeout=5)
        self.agent.audit.add("outbound.stop", target=session)
        return self.job_status(session, job)

    async def stop(self, session=None):
        async with self.lock:
            state = self.continuous.public()
            if session is None or state.get("session") == session:
                self.continuous.request_stop()
            if session is not None:
                result = await self._stop_job_locked(session)
                await self.continuous.tick_locked()
                return result
            results = []
            for row in self.active_jobs():
                results.append(await self._stop_job_locked(row["session"]))
            await self.continuous.tick_locked()
            return {"state": "stopped", "items": results}

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
