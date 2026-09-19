"""Durable, serial external duels. The browser is never the scheduler."""
import asyncio
from contextlib import suppress
import time
import secrets


RUNNING = {"running", "waiting", "stopping"}


def target_count(value):
    if type(value) is not int or (value != -1 and not 1 <= value <= 2147483647):
        raise ValueError("Target must be a positive integer or -1")
    return value


def authoritative_outcome(result):
    if result.get("result_scope") == "match":
        games = result.get("games", [])
        if (result.get("event") != "complete" or result.get("terminated") is not True
                or result.get("truncated") or result.get("selftest")
                or result.get("match_ended") is not True or not isinstance(games, list)
                or not 1 <= len(games) <= 5):
            return "incomplete"
        if any(type(g.get("our_player")) is not int or g["our_player"] not in (0, 1)
               or type(g.get("winner")) is not int or g["winner"] not in (0, 1, 2) for g in games):
            return "incomplete"
        counted = games[-1:] if result.get("match_kill") else games
        wins = sum(g["winner"] == g["our_player"] for g in counted)
        losses = sum(g["winner"] in (0, 1) and g["winner"] != g["our_player"] for g in counted)
        expected = "win" if wins > losses else "loss" if losses > wins else "draw"
        return expected if result.get("outcome") == expected else "incomplete"
    if (result.get("event") != "complete" or result.get("terminated") is not True
            or result.get("truncated", False) or result.get("selftest", False)
            or type(result.get("model_seat")) is not int or result["model_seat"] not in (0, 1)
            or type(result.get("winner")) is not int or result["winner"] not in (0, 1, 2)):
        return "incomplete"
    expected = ("draw" if result["winner"] == 2 else
                "win" if result["winner"] == result["model_seat"] else "loss")
    return expected if result.get("outcome") == expected else "incomplete"


class ContinuousDuels:
    def __init__(self, manager):
        self.manager = manager
        self.path = manager.root / "continuous.json"
        self.private = manager.root / "continuous-private.json"
        self.task = None

    def public(self):
        return self.manager._read(self.path) or {"state": "idle"}

    def active(self):
        return self.public()["state"] in RUNNING

    def save(self, state):
        state["updated_at"] = time.time()
        self.manager.agent.save(self.path, state)
        if state["state"] not in RUNNING:
            self.private.unlink(missing_ok=True)

    async def start(self, body):
        target = target_count(body.get("target"))
        settings = {key: value for key, value in body.items() if key != "target"}
        async with self.manager.lock:
            self.manager.ensure_idle()
            prepared = await self.manager.prepare(settings)
            identity = secrets.token_hex(8)
            self.manager.agent.save(self.private, {"id": identity, "prepared": prepared})
            self.private.chmod(0o600)
            config = prepared["config"]
            state = {"id": identity, "state": "running", "target": target, "completed": 0,
                     "win": 0, "loss": 0, "draw": 0, "incomplete": 0, "attempts": 0,
                     "failure_streak": 0, "session": None, "next_at": time.time(),
                     "last_error": "", "started_at": time.time(),
                     "host": config["display_host"], "port": config["port"],
                     "model": config["model"]["model_label"], "deck": config["deck_name"]}
            self.save(state)
            self.manager.agent.audit.add("outbound.continuous.start", target=identity)
            return state

    def request_stop(self):
        state = self.public()
        if state["state"] in RUNNING:
            state["state"] = "stopping"
            self.save(state)
            self.manager.agent.audit.add("outbound.continuous.stop", target=state["id"])

    async def tick_locked(self):
        state = self.public()
        if state["state"] not in RUNNING:
            return
        job = self.manager._read(self.manager.root / "current.json")
        session = state.get("session")
        if session:
            if job.get("session") == session and self.manager._alive(job):
                return
            # This update is the sole counter commit; clearing session makes it idempotent.
            result = self.manager._read(self.manager.sessions / session / "result.json")
            outcome = authoritative_outcome(result)
            state[outcome] += 1
            state["completed"] = state["win"] + state["loss"]
            state["session"] = None
            state["last_session"] = session
            state["last_error"] = result.get("error", "") if result else "worker_exited"
            state["failure_streak"] = state["failure_streak"] + 1 if outcome == "incomplete" else 0
            state["next_at"] = time.time() + (min(120, 5 * 2 ** min(state["failure_streak"] - 1, 5))
                                              if outcome == "incomplete" else 3)
            if state["state"] != "stopping":
                state["state"] = "waiting"
            self.save(state)
        if state["state"] == "stopping":
            state.update(state="stopped", next_at=None)
            self.save(state)
            return
        if state["target"] != -1 and state["completed"] >= state["target"]:
            state.update(state="finished", next_at=None)
            self.save(state)
            self.manager.agent.audit.add("outbound.continuous.finish", target=state["id"])
            return
        if time.time() < state["next_at"]:
            return
        if self.manager._alive(job):
            state.update(state="failed", last_error="Another external worker is active", next_at=None)
            self.save(state)
            return
        private = self.manager._read(self.private)
        if private.get("id") != state["id"]:
            state.update(state="failed", last_error="Continuous settings are unavailable", next_at=None)
            self.save(state)
            return
        # Reserve the attempt before launching; restart can account for an interrupted launch.
        session = self.manager.new_session()
        state.update(state="running", session=session, attempts=state["attempts"] + 1)
        self.save(state)
        try:
            await self.manager.launch(private["prepared"], session, continuous_id=state["id"])
        except Exception as error:
            folder = self.manager.sessions / session
            folder.mkdir(exist_ok=True)
            self.manager.agent.save(folder / "result.json", {
                "event": "incomplete", "outcome": "incomplete", "error": type(error).__name__,
                "terminated": False, "selftest": False})
            self.manager.agent.save(folder / "status.json", {
                "state": "failed", "outcome": "incomplete", "error": type(error).__name__})

    async def tick(self):
        async with self.manager.lock:
            await self.tick_locked()

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as error:
                # Storage errors must not create a tight reconnect loop or expose private settings.
                with suppress(OSError):
                    self.manager.agent.audit.add("outbound.continuous.scheduler", False,
                                                 target=type(error).__name__)
                await asyncio.sleep(5)
            await asyncio.sleep(1)

    async def open(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name="continuous-duels")

    async def close(self):
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
