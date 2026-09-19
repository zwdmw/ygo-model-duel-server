"""Loopback-published YGOPro room backed by the immutable 195M snapshot."""
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


ROOT = Path(os.environ.get("AGENT195M_ROOT", "/app"))
DATA = Path(os.environ.get("AGENT195M_DATA", "/data"))
STATUS = {"state": "warming_up", "checkpoint_steps": 195035136, "backend": "local_cpu",
          "client_profiles": ["legacy", "YGOPro2"], "deck_selection_api": 1,
          "policy_mode": "resident", "startup_api": 1}
LOCK = threading.Lock()
PENDING = PendingDecks()
EXTRA_TYPES = 0x40 | 0x2000 | 0x800000 | 0x4000000


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


def await_deck(client, human_seat, card_types, known):
    deck = None
    while True:
        protocol, payload = receive(client)
        if protocol == 0x02:
            try:
                deck = validate_deck(payload, card_types, known)
            except ValueError as error:
                deck = None
                chat(client, str(error))
        elif protocol in {0x22, 0x25}:
            if deck is not None:
                return deck
            client.sendall(packet(0x21, bytes(((human_seat << 4) | 10,))))
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
                protocol, _ = receive(client)
                if protocol in {0x13, 0x14}:
                    raise EOFError("Client left while the duel was starting")
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
            self.send_json(STATUS, 200 if STATUS["state"] != "failed" else 503)
        elif self.path == "/catalog" and self.card_types is not None:
            self.send_json({"checkpoint_steps": 195035136, "card_types": self.card_types})
        else:
            self.send_json({"error": "Not found or service still initializing"}, 404)

    def do_POST(self):
        if self.path != "/prepare":
            self.send_json({"error": "Not found"}, 404)
            return
        if self.headers.get("Origin") or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.send_json({"error": "Use the local deck picker"}, 403)
            return
        if self.card_types is None or STATUS["state"] in ("warming_up", "failed"):
            self.send_json({"error": "Model service is not ready"}, 503)
            return
        if LOCK.locked():
            self.send_json({"error": "Room occupied; finish or leave the current duel first"}, 409)
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 128 * 1024:
                raise ValueError("Invalid request length")
            self.connection.settimeout(10)
            request = json.loads(self.rfile.read(length))
            self.send_json(PENDING.prepare(request, self.card_types))
        except (ValueError, UnicodeError) as error:
            self.send_json({"error": str(error)}, 400)

    def log_message(self, *_):
        pass


def duel(client, args, card_types, known):
    if not LOCK.acquire(blocking=False):
        chat(client, "Agent195M is already playing another duel.")
        client.close()
        return
    process = None
    bridge = None
    output = None
    worker_log = None
    wire_log = None
    resigned = False
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
        model_name = selection["model_name"] if selection else "BestowedDragon"
        model_cards = selection["model_deck"] if selection else validate_selection(
            read_ydk(ROOT / "project/assets/deck/BestowedDragon.ydk"),
            {code: card_types[code] for code in known},
        )
        client.sendall(packet(0x12, host_info()))
        client.sendall(packet(0x13, bytes((0x10 | human_seat,))))
        client.sendall(packet(0x20, text40("Human") + bytes((human_seat,))))
        client.sendall(packet(0x20, text40("Agent 195M") + bytes((1 - human_seat,))))
        client.sendall(packet(0x21, bytes((((1 - human_seat) << 4) | 9,))))
        chat(client, "195M / 195035136 steps / local CPU / native engine / no heuristic fallback")
        chat(client, "Model deck: " + model_name)
        STATUS.update(state="lobby", client_profile=profile, model_deck=model_name)
        deck = await_deck(client, human_seat, card_types, known)
        if deck is None:
            return
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
            "seed": seed, "human_seat": human_seat, "checkpoint_steps": 195035136,
            "client_profile": profile,
            "human_deck_name": human_name, "model_deck_name": model_name,
            "human_deck_sha256": hashlib.sha256(human_deck.read_bytes()).hexdigest(),
            "model_deck_sha256": hashlib.sha256(model_deck.read_bytes()).hexdigest(),
        }, indent=2))
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
                        if protocol == 0x14:
                            client.sendall(packet(0x01, bytes((5, 1 - human_seat, 0))))
                        finished = True
                    elif protocol == 0x01:
                        if wire_log is not None:
                            wire_log.write(json.dumps({
                                "direction": "human", "response": payload.hex(),
                            }) + "\n")
                            wire_log.flush()
                        bridge.sendall(packet(protocol, payload))
        finally:
            selector.close()
        if not resigned:
            process.wait(timeout=20)
        if resigned:
            (output / "resigned.json").write_text(json.dumps({"human_seat": human_seat}))
    except (EOFError, ConnectionError, socket.timeout):
        pass
    except Exception as error:
        STATUS["last_error"] = str(error)
        traceback.print_exc()
        try:
            chat(client, "Agent195M stopped: " + str(error))
        except OSError:
            pass
    finally:
        if bridge is not None:
            bridge.close()
        if process is not None:
            # The per-duel native engine and its relay share this private group.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            if process.poll() is None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
        if worker_log is not None:
            worker_log.close()
        if wire_log is not None:
            wire_log.close()
        if output is not None:
            (output / "client-protocol.json").write_text(json.dumps(compatibility, indent=2))
        # Let the client consume the final duel packets before EOF.
        time.sleep(1)
        client.close()
        STATUS["state"] = "ready" if args.policy_host.healthy() else "failed"
        LOCK.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=7915)
    parser.add_argument("--health-port", type=int, default=7916)
    parser.add_argument("--human-seat", type=int, choices=(0, 1), default=0)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()
    DATA.mkdir(parents=True, exist_ok=True)
    health = ThreadingHTTPServer((args.host, args.health_port), Health)
    threading.Thread(target=health.serve_forever, daemon=True).start()
    args.policy_host = PolicyHost()
    atexit.register(args.policy_host.close)
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
    STATUS.update(state="ready", human_seat=args.human_seat, model_deck="BestowedDragon")
    threading.Thread(target=args.policy_host.monitor, daemon=True).start()
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
