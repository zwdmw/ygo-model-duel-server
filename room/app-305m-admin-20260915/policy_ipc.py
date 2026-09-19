"""Bounded, pickle-free inference IPC over a private container Unix socket."""
import io
import json
import socket
import struct
import zipfile

import numpy as np

from relay import read_exact
from model_snapshot import identity, snapshot, validate_identity


MAX_FRAME = 2 * 1024 * 1024


def send_frame(sock, payload):
    if not 0 < len(payload) <= MAX_FRAME:
        raise ValueError("Invalid inference frame size")
    sock.sendall(struct.pack("<I", len(payload)) + payload)


def receive_frame(sock):
    length, = struct.unpack("<I", read_exact(sock, 4))
    if not 0 < length <= MAX_FRAME:
        raise ValueError("Invalid inference frame size")
    return read_exact(sock, length)


def encode_observation(actor, count, observation):
    if actor not in (0, 1) or not 1 <= count <= 128:
        raise ValueError("Invalid actor or legal action count")
    arrays = {key: np.asarray(value) for key, value in observation.items()
              if value is not None}
    if any(key.startswith("__") or value.dtype.hasobject for key, value in arrays.items()):
        raise ValueError("Invalid observation array")
    arrays["__meta"] = np.asarray([actor, count], dtype=np.int32)
    arrays["__none"] = np.asarray([key for key, value in observation.items()
                                   if value is None], dtype="U64")
    buffer = io.BytesIO()
    np.savez(buffer, **arrays)
    return buffer.getvalue()


def decode_observation(payload):
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.infolist()
        if len(entries) > 32 or sum(entry.file_size for entry in entries) > MAX_FRAME:
            raise ValueError("Oversized observation archive")
        if any(entry.compress_type != zipfile.ZIP_STORED for entry in entries):
            raise ValueError("Compressed observations are not supported")
    with np.load(io.BytesIO(payload), allow_pickle=False) as data:
        meta = data["__meta"]
        if meta.shape != (2,) or meta.dtype != np.int32:
            raise ValueError("Invalid inference metadata")
        actor, count = map(int, meta)
        if actor not in (0, 1) or not 1 <= count <= 128:
            raise ValueError("Invalid actor or legal action count")
        none_keys = data["__none"]
        if none_keys.ndim != 1 or none_keys.dtype.kind != "U":
            raise ValueError("Invalid null observation fields")
        observation = {key: data[key] for key in data.files if not key.startswith("__")}
        for key in none_keys.tolist():
            if key.startswith("__") or key in observation:
                raise ValueError("Duplicate observation field")
            observation[key] = None
    return actor, count, observation


def observation_schema(observation):
    return {key: None if value is None else (value.shape, value.dtype)
            for key, value in observation.items()}


def validate_observation(observation, schema):
    if observation_schema(observation) != schema:
        raise ValueError("Observation differs from the warmed policy schema")


def serve_connection(sock, predict, schema, identity):
    send_frame(sock, json.dumps(identity).encode("ascii"))
    while True:
        actor, count, observation = decode_observation(receive_frame(sock))
        validate_observation(observation, schema)
        result = predict(actor, count, observation)
        send_frame(sock, json.dumps(result, allow_nan=False).encode("ascii"))


class PolicyClient:
    def __init__(self, address, expected=None):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(900)
        try:
            self.socket.connect(str(address))
            self.identity = json.loads(receive_frame(self.socket))
            validate_identity(self.identity, identity(snapshot()) if expected is None else expected)
        except BaseException:
            self.close()
            raise

    def infer(self, actor, count, observation):
        send_frame(self.socket, encode_observation(actor, count, observation))
        result = json.loads(receive_frame(self.socket))
        if "error" in result:
            raise RuntimeError(result["error"])
        action = result.get("action")
        elapsed = result.get("inference_ms")
        if (type(action) is not int or not 0 <= action < count
                or not isinstance(elapsed, (int, float)) or not np.isfinite(elapsed)
                or elapsed < 0):
            raise RuntimeError("Invalid policy response; no heuristic fallback is used")
        return action, elapsed / 1000

    def close(self):
        self.socket.close()
