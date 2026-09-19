"""Loopback-published YGOPro room backed by an explicitly pinned snapshot."""
import argparse
import atexit
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import random
import selectors
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from relay import packet, receive
from deck_config import PendingDecks, SECTIONS, read_ydk, validate_selection
from model_snapshot import identity, snapshot
from replay_store import begin_session, finalize_session, recover_sessions, track_worker
from spectator import SpectatorHub, listen_spectators


ROOT = Path(os.environ.get("AGENT195M_ROOT", "/app"))
DATA = Path(os.environ.get("AGENT195M_DATA", "/data"))
MODEL = snapshot()
IDENTITY = identity(MODEL)
STATUS = {"state": "warming_up", **IDENTITY, "backend": "local_cpu",
          "default_model_deck": "BestowedDragon",
          "client_profiles": ["legacy", "YGOPro2"], "deck_selection_api": 1,
          "policy_mode": "resident", "startup_api": 1, "model_deck_upload": 1,
          "replay_recording": 2}
LOCK = threading.Lock()
WORKERS = set()
WORKER_OUTPUTS = {}
WORKERS_LOCK = threading.Lock()
SHUTDOWN = threading.Event()
PENDING = PendingDecks()
SPECTATORS = SpectatorHub()
MANAGEMENT = None
EXTRA_TYPES = 0x40 | 0x2000 | 0x800000 | 0x4000000


def stop_workers():
    SHUTDOWN.set()
    SPECTATORS.end()
    with WORKERS_LOCK:
        processes = [(process, WORKER_OUTPUTS.get(process)) for process in WORKERS]
    for process, output in processes:
        stop_duel_process(process)
        if output is not None:
            finalize_session(output, "service_shutdown", DATA)


def stop_duel_process(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


class PolicyHost:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="agent195m-policy-")
        self.address = Path(self.directory.name) / "policy.sock"
        self.process = None
        self.log = None
        self.stopping = threading.Event()

    def start(self):
        self.log = (DATA / "warmup.log").open("w")
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "worker.py"), "--serve-policy", str(self.address)],
            cwd=ROOT / "project", stdout=self.log, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 240
        while not self.address.exists():
            if self.process.poll() is not None:
                raise RuntimeError("Persistent model initialization failed; see /data/warmup.log")
            if time.monotonic() > deadline:
                raise TimeoutError("Persistent model initialization exceeded 240 seconds")
            time.sleep(0.1)
        STATUS["policy_pid"] = self.process.pid
        from policy_ipc import PolicyClient
        probe = PolicyClient(self.address, expected=IDENTITY)
        probe.close()

    def healthy(self):
        return self.process is not None and self.process.poll() is None

    def monitor(self):
        while not self.stopping.wait(1):
            if not self.healthy():
                STATUS.update(state="failed", error="Persistent model process exited; restart the service")
                return

    def close(self):
        self.stopping.set()
        if self.healthy():
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.log is not None:
            self.log.close()
        self.directory.cleanup()


def known_codes(text):
    return {int(line.split()[0]) for line in text.splitlines() if line.split()}


def text40(value):
    return value.encode("utf-16-le")[:38].ljust(40, b"\0")


def host_info():
    data = bytearray(20)
    data[6] = 5
    data[7] = 1
    struct.pack_into("<I", data, 12, 8000)
    data[16:18] = bytes((5, 1))
    struct.pack_into("<H", data, 18, 0)
    return data


def send_lobby(client, human_seat):
    client.sendall(
        packet(0x12, host_info())
        + packet(0x13, bytes((0x10 | human_seat,)))
        + packet(0x20, text40("Human") + bytes((human_seat,)))
        + packet(0x20, text40("Agent " + MODEL["model_label"]) + bytes((1 - human_seat,)))
        + packet(0x21, bytes(((human_seat << 4) | 10,)))
        + packet(0x21, bytes((((1 - human_seat) << 4) | 9,)))
    )


def chat(sock, text):
    sock.sendall(packet(0x19, struct.pack("<H", 8) + text.encode("utf-16-le") + b"\0\0"))


def join_profile(payload):
    if len(payload) < 48:
        raise ValueError("Truncated join packet")
    password = payload[8:48].decode("utf-16-le").split("\0", 1)[0]
    return "YGOPro2" if password == "YGOPro2" else "legacy"


def client_message(payload, profile):
    if profile != "YGOPro2" or not payload:
        return payload
    if payload[0] == 31:
        if len(payload) < 3 or len(payload) != 3 + payload[2] * 7:
            raise ValueError("Invalid native MSG_CONFIRM_CARDS")
        # YGOPro2 1.036.2 inserts skip_panel before count; native 1.036.1 has none.
        return payload[:2] + b"\0" + payload[2:]
    if payload[0] == 16:
        if len(payload) < 13 or len(payload) != 13 + payload[2] * 13:
            raise ValueError("Invalid native MSG_SELECT_CHAIN")
        # The old shared forced flag becomes one flag per effect in 1.036.2.
        converted = bytearray(payload[:4] + payload[5:13])
        for offset in range(13, len(payload), 13):
            converted.extend(payload[offset:offset + 1] + payload[4:5] +
                             payload[offset + 1:offset + 13])
        return bytes(converted)
    return payload


def validate_deck(payload, card_types, known):
    if len(payload) < 8:
        raise ValueError("Invalid deck packet")
    total, side_count = struct.unpack_from("<II", payload)
    if total > 75 or side_count > 15 or len(payload) != 8 + 4 * (total + side_count):
        raise ValueError("Invalid deck size")
    codes = list(struct.unpack_from(f"<{total + side_count}I", payload, 8))
    unsupported = sorted(set(codes) - known)
    if unsupported:
        raise ValueError("Cards outside this model snapshot: " + ", ".join(map(str, unsupported[:8])))
    main, extra = [], []
    for code in codes[:total]:
        (extra if card_types.get(code, 0) & EXTRA_TYPES else main).append(code)
    if not 40 <= len(main) <= 60 or len(extra) > 15:
        raise ValueError("Main deck must have 40-60 cards; extra deck at most 15")
    return main, extra, codes[total:]


def save_deck(path, deck):
    main, extra, side = deck
    path.write_text(
        "#main\n" + "".join(f"{x}\n" for x in main) +
        "#extra\n" + "".join(f"{x}\n" for x in extra) +
        "!side\n" + "".join(f"{x}\n" for x in side),
        encoding="ascii",
    )


def model_command(payload):
    if not payload or len(payload) > 512 or len(payload) % 2:
        return None
    try:
        text = payload.decode("utf-16-le")
    except UnicodeDecodeError:
        return None
    text, _, padding = text.partition("\0")
    if padding.strip("\0"):
        return None
    words = text.strip().casefold().split()
    if words and words[0] == "/human":
        return "human" if len(words) == 1 else "invalid"
    if not words or words[0] != "/model":
        return None
    if len(words) == 1:
        return "upload"
    if len(words) == 2 and words[1] in {"default", "cancel"}:
        return words[1]
    return "invalid"


def model_locked_notice(client, payload):
    if model_command(payload) is not None:
        chat(client, "\u5bf9\u5c40\u5df2\u5f00\u59cb\uff0c\u53cc\u65b9\u5361\u7ec4\u5df2\u9501\u5b9a\u3002")


def await_deck(client, human_seat, card_types, known, model_choice=None, require_start=False):
    deck = None
    selecting_model = False
    model_candidate = None
    last_model_upload = None
    explicit_human = False

    def unready(refresh=False):
        if refresh:
            # Some YGOPro clients only clear the Ready marker on NotReady;
            # their deck dropdown is re-enabled by the full lobby snapshot.
            send_lobby(client, human_seat)
        else:
            client.sendall(packet(0x21, bytes(((human_seat << 4) | 10,))))

    while True:
        protocol, payload = receive(client)
        if protocol == 0x02:
            try:
                candidate = validate_deck(payload, card_types, known)
            except ValueError as error:
                candidate = None
                chat(client, str(error))
            if selecting_model:
                model_candidate = candidate
                if candidate is not None:
                    last_model_upload = candidate
            else:
                deck = candidate
        elif protocol == 0x16 and model_choice is not None:
            command = model_command(payload)
            if command in {"upload", "cancel", "default", "human"}:
                # A dropdown change may not upload until Ready. Never reuse the
                # previous human deck as an implicit model upload.
                deck = model_candidate = None
                selecting_model = command == "upload"
                explicit_human = command == "human"
                unready(refresh=True)
                if command == "upload":
                    chat(client, "\u6a21\u578b\u5361\u7ec4\u8bbe\u7f6e\u4e2d")
                elif command == "human":
                    chat(client, "\u73a9\u5bb6\u5361\u7ec4\u8bbe\u7f6e\u4e2d\uff0c"
                         "\u6a21\u578b\u5361\u7ec4\u4fdd\u6301\u4e0d\u53d8\u3002"
                         "\u8bf7\u9009\u62e9\u73a9\u5bb6\u5361\u7ec4\u540e\u51c6\u5907\u3002")
                else:
                    if command == "default":
                        model_choice.update(name=model_choice.get("default_name", "BestowedDragon"),
                                            deck=model_choice["default_deck"], source="default")
                        STATUS["model_deck"] = model_choice["name"]
                    chat(client, ("\u5df2\u53d6\u6d88\u6a21\u578b\u5361\u7ec4\u8bbe\u7f6e\u3002"
                                  if command == "cancel" else
                                  "\u6a21\u578b\u5361\u7ec4\uff1a" + model_choice["name"]))
            elif command == "invalid":
                chat(client, "\u65e0\u6548\u7684\u6a21\u578b\u5361\u7ec4\u547d\u4ee4\u3002")
        elif protocol == 0x23:
            # MDPro3 waits for this acknowledgement before reopening its picker.
            deck = model_candidate = None
            unready()
        elif protocol in {0x22, 0x25}:
            if selecting_model:
                unready(refresh=True)
                if model_candidate is None:
                    chat(client, "\u5c1a\u672a\u6536\u5230\u6709\u6548\u7684\u6a21\u578b\u5361\u7ec4\u3002")
                    continue
                model_choice.update(
                    name="Player deck", source="lobby_upload",
                    deck={key: list(cards) for key, cards in zip(SECTIONS, model_candidate)})
                STATUS["model_deck"] = model_choice["name"]
                counts = tuple(len(cards) for cards in model_candidate)
                chat(client, "\u6a21\u578b\u5361\u7ec4\u5df2\u4fdd\u5b58\uff1a"
                     f"\u4e3b\u5361\u7ec4 {counts[0]} / \u989d\u5916 {counts[1]} / \u526f\u5361\u7ec4 {counts[2]}"
                     "\u3002\u8bf7\u9009\u62e9\u73a9\u5bb6\u5361\u7ec4\u540e\u518d\u51c6\u5907\u3002")
                selecting_model = False
                model_candidate = deck = None
                continue
            if deck is not None:
                # Ready often resends the same dropdown deck. That packet alone
                # cannot distinguish an unchanged model selection from a mirror.
                if (not explicit_human and last_model_upload is not None
                        and all(Counter(cards) == Counter(previous)
                                for cards, previous in zip(deck, last_model_upload))):
                    deck = None
                    unready(refresh=True)
                    chat(client, "\u73a9\u5bb6\u5361\u7ec4\u5c1a\u672a\u786e\u8ba4\uff1b"
                         "\u5f53\u524d\u4ecd\u662f\u521a\u624d\u7684\u6a21\u578b\u5361\u7ec4\u3002"
                         "\u8bf7\u9009\u62e9\u81ea\u5df1\u7684\u5361\u7ec4\u540e\u51c6\u5907\u3002"
                         "\u82e5\u786e\u5b9a\u4f7f\u7528\u540c\u4e00\u526f\u5361\u7ec4\uff0c"
                         "\u53d1\u9001 /human \u540e\u518d\u51c6\u5907\u3002")
                    continue
                if require_start and protocol == 0x22:
                    client.sendall(packet(0x21, bytes(((human_seat << 4) | 9,))))
                    continue
                return deck
            unready(refresh=True)
            chat(client, "Choose a supported deck before starting.")
        elif protocol in {0x13, 0x14}:
            return None


def await_bridge(client, listener, process, started):
    deadline = started + 60
    next_notice = started + 2
    with selectors.DefaultSelector() as poll:
        poll.register(client, selectors.EVENT_READ)
        poll.register(listener, selectors.EVENT_READ)
        while True:
            if process.poll() is not None:
                raise RuntimeError(f"Duel worker exited: {process.returncode}")
            now = time.monotonic()
            if now > deadline:
                raise TimeoutError("Duel engine startup exceeded 60 seconds")
            if now >= next_notice:
                chat(client, "\u6b63\u5728\u521b\u5efa\u5bf9\u5c40... "
                     f"{int(now - started)}s")
                next_notice = now + 2
            for key, _ in poll.select(timeout=0.2):
                if key.fileobj is listener:
                    return listener.accept()[0]
                protocol, payload = receive(client)
                if protocol in {0x13, 0x14}:
                    raise EOFError("Client left while the duel was starting")
                if protocol == 0x16:
                    model_locked_notice(client, payload)
                # Ready/Start repeats are lobby messages, never engine decisions.


class Health(BaseHTTPRequestHandler):
    card_types = None

    def send_json(self, data, status=200):
        data = json.dumps(data, ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self.send_json({**STATUS, **SPECTATORS.status(),
                            **(MANAGEMENT.status() if MANAGEMENT else {})},
                           200 if STATUS["state"] != "failed" else 503)
        elif self.path == "/manage" and MANAGEMENT:
            if not MANAGEMENT.authorize(self.headers.get("Authorization")):
                self.send_json({"error": "Unauthorized"}, 401)
                return
            self.send_json(MANAGEMENT.status())
        elif self.path == "/catalog" and self.card_types is not None:
            self.send_json({**IDENTITY, "card_types": self.card_types})
        else:
            self.send_json({"error": "Not found or service still initializing"}, 404)

    def do_POST(self):
        if self.path == "/manage" and MANAGEMENT:
            if (self.headers.get("Origin") or
                    not MANAGEMENT.authorize(self.headers.get("Authorization"))):
                self.send_json({"error": "Unauthorized"}, 401)
                return
            try:
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Expected JSON")
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 128 * 1024:
                    raise ValueError("Invalid request length")
                self.connection.settimeout(5)
                self.send_json(MANAGEMENT.action(json.loads(self.rfile.read(length))))
                STATUS["default_model_deck"] = MANAGEMENT.status()["default_name"]
            except (ValueError, TypeError, OSError, RuntimeError) as error:
                self.send_json({"error": str(error)}, 400)
            return
        if self.path != "/prepare":
            self.send_json({"error": "Not found"}, 404)
            return
        if self.headers.get("Origin") or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.send_json({"error": "Use the local deck picker"}, 403)
            return
        if self.card_types is None or STATUS["state"] in ("warming_up", "failed"):
            self.send_json({"error": "Model service is not ready"}, 503)
            return
        if LOCK.locked() or (MANAGEMENT and not MANAGEMENT.status()["accepting"]):
            self.send_json({"error": "Room occupied; finish or leave the current duel first"}, 409)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 128 * 1024:
                raise ValueError("Invalid request length")
            self.connection.settimeout(10)
            request = json.loads(self.rfile.read(length))
            if request.pop("use_default_model", False):
                if MANAGEMENT:
                    request["model_name"], request["model_deck"] = MANAGEMENT.default()
            self.send_json(PENDING.prepare(request, self.card_types))
        except (ValueError, UnicodeError) as error:
            self.send_json({"error": str(error)}, 400)

    def log_message(self, *_):
        pass


def duel(client, args, card_types, known):
    if not (MANAGEMENT.enter() if MANAGEMENT else LOCK.acquire(blocking=False)):
        chat(client, f"Agent {MODEL['model_label']} is already playing another duel.")
        client.close()
        return
    process = None
    bridge = None
    output = None
    worker_log = None
    wire_log = None
    resigned = False
    exit_reason = "interrupted"
    recording_failed = False
    compatibility = {}
    selection = None
    try:
        if not args.policy_host.healthy():
            raise RuntimeError("Persistent model is unavailable; restart the service")
        client.settimeout(900)
        while True:
            protocol, payload = receive(client)
            if protocol == 0x12:
                profile = join_profile(payload)
                password = payload[8:48].decode("utf-16-le").split("\0", 1)[0]
                if password.startswith("D:"):
                    selection = PENDING.consume(password[2:])
                    profile = selection["profile"]
                compatibility = {
                    "profile": profile, "join_version": struct.unpack_from("<H", payload)[0],
                    "confirm_cards_adapted": 0,
                    "select_chain_adapted": 0,
                }
                break
            if protocol not in {0x10, 0x17}:
                raise ValueError(f"Unexpected handshake {protocol}")
        human_seat = args.human_seat
        default_model_cards = validate_selection(
            read_ydk(ROOT / "project/assets/deck/BestowedDragon.ydk"),
            {code: card_types[code] for code in known},
        )
        default_name = "BestowedDragon"
        if MANAGEMENT:
            default_name, default_model_cards = MANAGEMENT.default()
        model_name = selection["model_name"] if selection else default_name
        model_choice = {
            "name": model_name,
            "deck": selection["model_deck"] if selection else default_model_cards,
            "default_deck": default_model_cards,
            "default_name": default_name,
            "source": "prepared" if selection else "default",
        }
        send_lobby(client, human_seat)
        chat(client, f"{MODEL['model_label']} / {MODEL['checkpoint_steps']} steps / CPU / native engine / no heuristic fallback")
        chat(client, "Model deck: " + model_name)
        STATUS.update(state="lobby", client_profile=profile, model_deck=model_name)
        deck = await_deck(client, human_seat, card_types, known, model_choice,
                          require_start=profile == "legacy")
        if deck is None:
            return
        model_name, model_cards = model_choice["name"], model_choice["deck"]
        started = time.monotonic()
        session = time.strftime("%Y%m%d-%H%M%S") + f"-{random.randrange(10000):04d}"
        output = DATA / "sessions" / session
        output.mkdir(parents=True)
        if profile == "YGOPro2":
            wire_log = (output / "client-wire.jsonl").open("w")
        human_deck = output / "human.ydk"
        save_deck(human_deck, deck)
        model_deck = output / "model.ydk"
        save_deck(model_deck, tuple(model_cards[key] for key in SECTIONS))
        human_name = "Room selection"
        if selection and all(Counter(cards) == Counter(selection["human_deck"][key])
                             for key, cards in zip(SECTIONS, deck)):
            human_name = selection["human_name"]
        client.sendall(packet(0x21, bytes(((human_seat << 4) | 9,))))
        chat(client, "\u6a21\u578b\u5df2\u5c31\u7eea\uff0c\u6b63\u5728\u521b\u5efa\u5bf9\u5c40...")
        STATUS.update(state="starting_duel", session=session)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(1)
        seed = random.SystemRandom().randrange(1, 2**30)
        (output / "session.json").write_text(json.dumps({
            "seed": seed, "human_seat": human_seat, **IDENTITY,
            "client_profile": profile,
            "human_deck_name": human_name, "model_deck_name": model_name,
            "model_deck_source": model_choice["source"],
            "human_deck_sha256": hashlib.sha256(human_deck.read_bytes()).hexdigest(),
            "model_deck_sha256": hashlib.sha256(model_deck.read_bytes()).hexdigest(),
        }, indent=2))
        begin_session(output)
        SPECTATORS.begin(session)
        env = dict(os.environ)
        env["MDPRO_RELAY_PORT"] = str(listener.getsockname()[1])
        env["PATH"] = str(ROOT / "bin") + os.pathsep + env["PATH"]
        worker_log = (output / "worker.log").open("w")
        process = subprocess.Popen(
            [sys.executable, str(ROOT / "worker.py"), "--human-deck", str(human_deck),
             "--model-deck", str(model_deck),
             "--policy-socket", str(args.policy_host.address),
             "--human-seat", str(human_seat), "--seed", str(seed),
             "--output", str(output)],
            cwd=ROOT / "project", env=env, stdout=worker_log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        with WORKERS_LOCK:
            WORKERS.add(process)
            WORKER_OUTPUTS[process] = output
        track_worker(output, process.pid)
        try:
            bridge = await_bridge(client, listener, process, started)
        finally:
            listener.close()
        bridge.settimeout(900)
        startup_seconds = time.monotonic() - started
        (output / "startup.json").write_text(json.dumps({
            "startup_seconds": startup_seconds, "policy_mode": "resident",
            "policy_pid": args.policy_host.process.pid,
        }, indent=2))
        STATUS.update(state="dueling", startup_seconds=startup_seconds)
        selector = selectors.DefaultSelector()
        selector.register(client, selectors.EVENT_READ)
        selector.register(bridge, selectors.EVENT_READ)
        try:
            finished = False
            while not finished:
                events = selector.select(timeout=10)
                if not events and process.poll() is not None:
                    break
                for key, _ in events:
                    source = key.fileobj
                    protocol, payload = receive(source)
                    if source is bridge:
                        SPECTATORS.publish(protocol, payload)
                        if protocol == 0x01:
                            converted = client_message(payload, profile)
                            if converted != payload:
                                counter = "confirm_cards_adapted" if payload[0] == 31 else "select_chain_adapted"
                                compatibility[counter] += 1
                            if wire_log is not None:
                                wire_log.write(json.dumps({
                                    "direction": "engine", "native": payload.hex(),
                                    "client": converted.hex(),
                                }) + "\n")
                                wire_log.flush()
                            payload = converted
                        client.sendall(packet(protocol, payload))
                        if protocol == 0x16:
                            finished = True
                    elif protocol in {0x13, 0x14}:
                        resigned = True
                        exit_reason = "surrender" if protocol == 0x14 else "left_room"
                        if protocol == 0x14:
                            client.sendall(packet(0x01, bytes((5, 1 - human_seat, 0))))
                            SPECTATORS.publish(1, bytes((5, 1 - human_seat, 0)))
                        finished = True
                    elif protocol == 0x01:
                        if wire_log is not None:
                            wire_log.write(json.dumps({
                                "direction": "human", "response": payload.hex(),
                            }) + "\n")
                            wire_log.flush()
                        bridge.sendall(packet(protocol, payload))
                    elif protocol == 0x16:
                        model_locked_notice(client, payload)
        finally:
            selector.close()
        if not resigned:
            process.wait(timeout=20)
        if resigned:
            (output / "resigned.json").write_text(json.dumps({"human_seat": human_seat}))
    except socket.timeout:
        exit_reason = "timeout"
    except (EOFError, ConnectionError):
        exit_reason = "disconnected"
    except Exception as error:
        exit_reason = "error"
        STATUS["last_error"] = str(error)
        traceback.print_exc()
        try:
            chat(client, f"Agent {MODEL['model_label']} stopped: " + str(error))
        except OSError:
            pass
    finally:
        SPECTATORS.end()
        if bridge is not None:
            bridge.close()
        if process is not None:
            stop_duel_process(process)
        if worker_log is not None:
            worker_log.close()
        if wire_log is not None:
            wire_log.close()
        if output is not None:
            try:
                (output / "client-protocol.json").write_text(json.dumps(compatibility, indent=2))
                finalize_session(output, "service_shutdown" if SHUTDOWN.is_set() else exit_reason, DATA)
            except Exception as error:
                recording_failed = True
                STATUS["last_recording_error"] = str(error)
                traceback.print_exc()
        if process is not None:
            with WORKERS_LOCK:
                WORKERS.discard(process)
                WORKER_OUTPUTS.pop(process, None)
        # Let the client consume the final duel packets before EOF.
        time.sleep(1)
        client.close()
        STATUS["state"] = "ready" if args.policy_host.healthy() and not recording_failed else "failed"
        LOCK.release()


def main():
    global MANAGEMENT
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7915)
    parser.add_argument("--health-port", type=int, default=7916)
    parser.add_argument("--spectator-port", type=int, default=7917)
    parser.add_argument("--human-seat", type=int, choices=(0, 1), default=0)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    DATA.mkdir(parents=True, exist_ok=True)
    if os.environ.get("AGENT_ADMIN_KEY_FILE"):
        from room_management import RoomManagement
        MANAGEMENT = RoomManagement(DATA / "admin", os.environ["AGENT_ADMIN_KEY_FILE"],
                                    LOCK, SPECTATORS, model_root=ROOT)
    health = ThreadingHTTPServer((args.host, args.health_port), Health)
    threading.Thread(target=health.serve_forever, daemon=True).start()
    print(json.dumps({"event": "replay_recovery", **recover_sessions(DATA)}), flush=True)
    args.policy_host = PolicyHost()
    atexit.register(args.policy_host.close)
    atexit.register(stop_workers)
    try:
        args.policy_host.start()
    except Exception as error:
        STATUS.update(state="failed", error=str(error))
        raise
    known = known_codes((ROOT / "project/scripts/code_list.txt").read_text())
    with sqlite3.connect(ROOT / "project/assets/locale/en/cards.cdb") as db:
        card_types = dict(db.execute("SELECT id, type FROM datas"))
    known.intersection_update(card_types)
    Health.card_types = {code: card_types[code] for code in known}
    if MANAGEMENT:
        MANAGEMENT.initialize(Health.card_types, read_ydk(ROOT / "project/assets/deck/BestowedDragon.ydk"))
        STATUS["default_model_deck"] = MANAGEMENT.status()["default_name"]
    STATUS.update(state="ready", human_seat=args.human_seat, model_deck="BestowedDragon")
    threading.Thread(target=args.policy_host.monitor, daemon=True).start()
    watchers = socket.socket()
    watchers.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    watchers.bind(("127.0.0.1", args.spectator_port))
    watchers.listen(16)
    atexit.register(watchers.close)
    threading.Thread(target=listen_spectators,
                     args=(watchers, SPECTATORS, host_info, client_message, MODEL["model_label"]),
                     daemon=True).start()
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((args.host, args.port))
        listener.listen(2)
        print(json.dumps(STATUS), flush=True)
        while True:
            client, _ = listener.accept()
            threading.Thread(target=duel, args=(client, args, card_types, known), daemon=True).start()


if __name__ == "__main__":
    main()
