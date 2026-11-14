"""One GPU-resident policy shared by isolated outbound room workers."""
from contextlib import suppress
import json
import os
from pathlib import Path
import signal
import socket
import sys
import threading
import time
from types import SimpleNamespace

import numpy as np


class PolicySession:
    """Connection-local recurrent state over one shared inference function."""

    def __init__(self, init_state, infer, block_until_ready, asarray, lock,
                 count_unmapped=None, lookup=None):
        self.init_state = init_state
        self.infer = infer
        self.block_until_ready = block_until_ready
        self.asarray = asarray
        self.lock = lock
        self.count_unmapped = count_unmapped
        self.lookup = lookup
        self.reset()

    def reset(self):
        self.states = [self.init_state(), self.init_state()]

    def predict(self, actor, count, observation):
        queued = time.monotonic()
        with self.lock:
            queue_seconds = time.monotonic() - queued
            tick = time.monotonic()
            self.states[actor], action, logits = self.infer(
                self.states[actor], observation
            )
            self.block_until_ready(action)
            elapsed = time.monotonic() - tick
            index = int(self.asarray(action)[0])
            finite = bool(np.isfinite(self.asarray(logits)).all())
            unmapped = (
                int(self.asarray(self.count_unmapped(observation, self.lookup)))
                if self.lookup is not None
                else 0
            )
        if not 0 <= index < count or not finite:
            raise RuntimeError(
                "Invalid policy output; no heuristic fallback is used"
            )
        return {
            "action": index,
            "inference_ms": elapsed * 1000,
            "queue_ms": queue_seconds * 1000,
            "unmapped_count": unmapped,
        }


def execute(config):
    app = Path(config["app"]).resolve()
    address = Path(config["socket"]).resolve()
    output = Path(config["output"]).resolve()
    max_clients = config.get("max_clients", 4)
    if type(max_clients) is not int or not 1 <= max_clients <= 64:
        raise ValueError("Invalid policy client limit")
    sys.path[:0] = [str(app), str(app / "project")]
    os.environ.update(
        AGENT195M_ROOT=str(app),
        AGENT_MODEL_SNAPSHOT=json.dumps(config["model"]),
        XLA_PYTHON_CLIENT_PREALLOCATE="false",
    )

    import jax
    import worker
    from .policy_ipc import (RESET_FRAME, decode_observation, receive_frame,
                             send_frame, validate_observation)
    from .pool_assets import PoolAssets
    from .pool_policy import load_policy

    if jax.default_backend() != "gpu":
        raise RuntimeError(
            "Outbound shared policy requires a GPU backend; got "
            + jax.default_backend()
        )

    output.mkdir(parents=True, exist_ok=True)
    args = SimpleNamespace(
        seed=config["seed"],
        output=output,
        model_deck=worker.MODEL_DECK,
        human_deck=worker.MODEL_DECK,
    )
    pool_config = config.get("pool")
    assets = (
        PoolAssets(pool_config["path"], app, pool_config["sha256"])
        if pool_config is not None
        else None
    )
    sidecar = worker.CHECKPOINT.with_name(worker.CHECKPOINT.name + ".json")
    architecture = None
    if sidecar.is_file() and not sidecar.is_symlink():
        with suppress(OSError, ValueError):
            architecture = json.loads(sidecar.read_text()).get("architecture")
    if architecture == "official":
        from .official_policy import count_unmapped, load_policy as load_official
        from .official_policy import lookup_for

        agent, infer, schema, policy_metadata = load_official(
            args, app, worker.CHECKPOINT, assets
        )
        lookup = lookup_for(policy_metadata)
    else:
        agent, infer, schema, policy_metadata = load_policy(args, assets)
        count_unmapped = lookup = None

    identity = {
        **worker.IDENTITY,
        "backend": jax.default_backend(),
        "card_pool": policy_metadata,
        "architecture": architecture or "structured",
        "pid": os.getpid(),
    }
    inference_lock = threading.Lock()
    slots = threading.BoundedSemaphore(max_clients)
    stop = threading.Event()
    threads = set()
    threads_lock = threading.Lock()
    session_lock = threading.Lock()
    next_session = 0

    def serve(connection, session):
        policy = PolicySession(
            lambda: jax.device_put(agent.init_rnn_state(1)),
            infer,
            jax.block_until_ready,
            np.asarray,
            inference_lock,
            count_unmapped,
            lookup,
        )
        try:
            connection.settimeout(900)
            send_frame(connection, json.dumps({
                **identity, "policy_session": session,
            }).encode("ascii"))
            while not stop.is_set():
                payload = receive_frame(connection)
                if payload == RESET_FRAME:
                    policy.reset()
                    send_frame(connection, b'{"ok":true}')
                    continue
                actor, count, observation = decode_observation(payload)
                validate_observation(observation, schema)
                send_frame(
                    connection,
                    json.dumps(
                        policy.predict(actor, count, observation),
                        allow_nan=False,
                    ).encode("ascii"),
                )
        except (EOFError, ConnectionError, OSError, socket.timeout):
            pass
        except Exception as error:
            with suppress(OSError):
                send_frame(
                    connection,
                    json.dumps({"error": str(error)[:500]}).encode("ascii"),
                )
        finally:
            with suppress(OSError):
                connection.close()
            slots.release()
            with threads_lock:
                threads.discard(threading.current_thread())

    def request_stop(*_):
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    address.parent.mkdir(parents=True, exist_ok=True)
    if address.exists():
        if address.is_symlink():
            raise ValueError("Policy socket path is a symlink")
        address.unlink()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(address))
            os.chmod(address, 0o600)
            listener.listen(max_clients)
            listener.settimeout(1)
            print(json.dumps({
                "event": "outbound_policy_ready",
                **identity,
                "socket": str(address),
                "max_clients": max_clients,
            }, sort_keys=True), flush=True)
            while not stop.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                if not slots.acquire(blocking=False):
                    connection.close()
                    continue
                with session_lock:
                    next_session += 1
                    session = next_session
                thread = threading.Thread(
                    target=serve,
                    args=(connection, session),
                    name=f"outbound-policy-{session}",
                    daemon=True,
                )
                with threads_lock:
                    threads.add(thread)
                thread.start()
    finally:
        stop.set()
        address.unlink(missing_ok=True)
        with threads_lock:
            active = list(threads)
        for thread in active:
            thread.join(timeout=2)


if __name__ == "__main__":
    execute(json.loads(sys.stdin.buffer.read(128 * 1024)))
