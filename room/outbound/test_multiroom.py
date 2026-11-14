import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import numpy as np

from .manager import OutboundManager
from .policy_server import PolicySession


class Audit:
    def add(self, *args, **kwargs):
        pass


class Agent:
    def __init__(self, root, max_rooms=2):
        self.data = root / "data"
        self.data.mkdir()
        self.app_root = root / "app"
        self.outbound_app = self.app_root
        (self.app_root / "project").mkdir(parents=True)
        self.config = {"max_concurrent_outbound_rooms": max_rooms}
        self.audit = Audit()
        self.external_pool = None
        self.pool_error = ""

    @staticmethod
    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")


class MultiRoomManagerTests(unittest.TestCase):
    def test_active_jobs_are_independent_and_capacity_is_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = OutboundManager(Agent(Path(temporary)))
            manager.opponent_decks = SimpleNamespace(
                inspect=lambda *_args, **_kwargs: {"available": False}
            )
            manager._alive = lambda job: bool(job.get("alive"))
            sessions = ("20261004-220000-0001", "20261004-220000-0002")
            for session in sessions:
                folder = manager.sessions / session
                folder.mkdir()
                manager.agent.save(
                    manager._job_path(session),
                    {"session": session, "alive": True},
                )
                manager.agent.save(
                    folder / "status.json",
                    {"state": "dueling", "session": session},
                )

            active = manager.active_jobs()
            self.assertEqual(
                [row["session"] for row in active],
                list(reversed(sessions)),
            )
            self.assertEqual(manager.current()["session"], sessions[1])
            with self.assertRaisesRegex(ValueError, "capacity reached"):
                manager.ensure_capacity()

            manager.agent.save(
                manager.sessions / sessions[0] / "status.json",
                {"state": "finished", "session": sessions[0]},
            )
            manager.agent.save(
                manager.sessions / sessions[1] / "status.json",
                {"state": "reconnecting", "session": sessions[1]},
            )
            manager.ensure_capacity()
            self.assertEqual(
                [row["session"] for row in manager.active_jobs()],
                [sessions[1]],
            )


class MultiRoomBatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_prepares_once_and_launches_six_passwords(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = OutboundManager(Agent(Path(temporary), max_rooms=6))
            manager.prepare = AsyncMock(return_value={
                "ydk": "main",
                "config": {"password": "first", "model": {}},
            })
            observed = []

            async def launch(prepared, session):
                observed.append((prepared["config"]["password"], session))
                return {"session": session, "state": "waiting"}

            manager.launch = launch
            passwords = [f"room-{index}" for index in range(6)]
            result = await manager.start_many({
                "action": "start_many",
                "passwords": passwords,
            })

            self.assertEqual(result["started"], 6)
            self.assertEqual([row[0] for row in observed], passwords)
            self.assertEqual(len({row[1] for row in observed}), 6)
            manager.prepare.assert_awaited_once()
            prepared_body = manager.prepare.await_args.args[0]
            self.assertEqual(prepared_body["action"], "start")
            self.assertEqual(prepared_body["password"], "room-0")
            self.assertNotIn("passwords", prepared_body)

    async def test_batch_rejects_duplicates_and_capacity_overflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = OutboundManager(Agent(Path(temporary)))
            with self.assertRaisesRegex(ValueError, "unique"):
                await manager.start_many({
                    "action": "start_many",
                    "passwords": ["same", "same"],
                })
            manager.active_jobs = lambda: [{"session": "already-active"}]
            with self.assertRaisesRegex(ValueError, "capacity reached"):
                await manager.start_many({
                    "action": "start_many",
                    "passwords": ["one", "two"],
                })

    async def test_batch_groups_rooms_by_model_and_preserves_room_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = OutboundManager(Agent(Path(temporary), max_rooms=6))
            first, second = "a" * 64, "b" * 64

            async def prepare(body):
                return {
                    "ydk": "main",
                    "config": {
                        "password": body["password"],
                        "model": {
                            "model_label": body["model_id"][:4],
                            "checkpoint_sha256": body["model_id"],
                        },
                    },
                }

            manager.prepare = AsyncMock(side_effect=prepare)
            observed = []

            async def launch(prepared, session):
                observed.append((
                    prepared["config"]["password"],
                    prepared["config"]["model"]["checkpoint_sha256"],
                ))
                return {"session": session, "state": "waiting"}

            manager.launch = launch
            result = await manager.start_many({
                "action": "start_many",
                "rooms": [
                    {"password": "room-a", "model_id": first},
                    {"password": "room-b", "model_id": second},
                    {"password": "room-c", "model_id": first},
                ],
            })

            self.assertEqual(manager.prepare.await_count, 2)
            self.assertEqual(observed, [
                ("room-a", first),
                ("room-b", second),
                ("room-c", first),
            ])
            self.assertEqual(
                [row["room_index"] for row in result["items"]],
                [0, 1, 2],
            )
            self.assertEqual(
                [row["model_id"] for row in result["items"]],
                [first, second, first],
            )

    async def test_batch_rolls_back_started_rooms_when_a_later_launch_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = OutboundManager(Agent(Path(temporary), max_rooms=6))
            model_id = "c" * 64
            manager.prepare = AsyncMock(return_value={
                "ydk": "main",
                "config": {
                    "password": "first",
                    "model": {
                        "model_label": "model",
                        "checkpoint_sha256": model_id,
                    },
                },
            })
            launches = 0

            async def launch(_prepared, session):
                nonlocal launches
                launches += 1
                if launches == 3:
                    raise RuntimeError("launch failed")
                return {"session": session, "state": "waiting"}

            manager.launch = launch
            manager._stop_job_locked = AsyncMock()
            with self.assertRaisesRegex(RuntimeError, "launch failed"):
                await manager.start_many({
                    "action": "start_many",
                    "rooms": [
                        {"password": "room-a", "model_id": model_id},
                        {"password": "room-b", "model_id": model_id},
                        {"password": "room-c", "model_id": model_id},
                    ],
                })

            self.assertEqual(manager._stop_job_locked.await_count, 2)

    def test_policy_locations_are_stable_per_model_and_distinct_between_models(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = OutboundManager(Agent(Path(temporary), max_rooms=6))
            first = {"model_id": "a" * 64, "pool_sha256": "1" * 64}
            second = {"model_id": "b" * 64, "pool_sha256": "1" * 64}

            self.assertEqual(
                manager._policy_location(first),
                manager._policy_location(dict(first)),
            )
            self.assertNotEqual(
                manager._policy_location(first)["socket"],
                manager._policy_location(second)["socket"],
            )


class PolicySessionTests(unittest.TestCase):
    def test_connections_keep_independent_recurrent_state_and_reset_locally(self):
        lock = threading.Lock()

        def init_state():
            return 0

        def infer(state, observation):
            next_state = state + int(observation["step"])
            return next_state, np.asarray([next_state]), np.asarray([0.0, 1.0])

        def session():
            return PolicySession(
                init_state,
                infer,
                lambda _value: None,
                np.asarray,
                lock,
            )

        first, second = session(), session()
        self.assertEqual(first.predict(0, 10, {"step": 1})["action"], 1)
        self.assertEqual(first.predict(0, 10, {"step": 1})["action"], 2)
        self.assertEqual(second.predict(0, 10, {"step": 1})["action"], 1)

        first.reset()
        self.assertEqual(first.predict(0, 10, {"step": 1})["action"], 1)
        self.assertEqual(second.predict(0, 10, {"step": 1})["action"], 2)


if __name__ == "__main__":
    unittest.main()
