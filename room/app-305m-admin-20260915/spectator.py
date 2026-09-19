"""Read-only public YGOPro streams; never expose the player's raw stream."""
from collections import deque
import select
import socket
import struct
import threading
import time

from relay import packet, read_exact


PRIVATE = set(range(10, 30)) | {1, 3, 8, 80, 120, 132, 140, 141, 142, 143,
                               144, 160, 163, 164, 170}
FIXED = {5: 3, 32: 2, 34: 2, 35: 2, 37: 1, 38: 7, 40: 2, 41: 3,
         50: 17, 53: 10, 54: 9, 55: 17, 56: 5,
         60: 9, 61: 1, 62: 9, 63: 1, 64: 9, 65: 1,
         70: 17, 71: 2, 72: 2, 73: 2, 74: 1, 75: 2, 76: 2,
         91: 6, 92: 6, 93: 9, 94: 6, 95: 5, 96: 9, 97: 9,
         100: 6, 101: 8, 102: 8, 110: 9, 111: 27, 112: 1, 113: 1,
         114: 1, 133: 2, 165: 7}


def exact(data, length):
    if len(data) != length:
        raise ValueError("Invalid spectator message length")


def hidden(location, position):
    return not location & 0x90 and (bool(location & 0x03) or bool(position & 0x0a))


def split_native_messages(data):
    # Reuse the layouts verified by replay_compat.split_messages / CoreWrapper.
    # Native Forward can combine shuffle-hand with the following shuffle-deck.
    fixed = {**FIXED, 1: 1, 2: 7, 3: 1, 4: 19, 8: 1, 12: 14, 13: 6,
             18: 7, 19: 7, 24: 7, 120: 9, 132: 2, 140: 7, 141: 7, 160: 10, 170: 5}
    cursor = 0

    def skip(count):
        nonlocal cursor
        if count < 0 or cursor + count > len(data):
            raise ValueError("Truncated native message stream")
        cursor += count

    def count():
        skip(1)
        return data[cursor - 1]

    while cursor < len(data):
        start = cursor
        msg = count()
        if msg == 6 and start == 0:
            yield data  # Native field queries are separately framed.
            return
        if msg in fixed:
            skip(fixed[msg] - 1)
        elif msg == 10:
            skip(1)
            skip(count() * 11)
            skip(count() * 8 + 2)
        elif msg == 11:
            skip(1)
            for _ in range(5):
                skip(count() * 7)
            skip(count() * 11 + 3)
        elif msg in (14, 142, 143, 144):
            skip(1)
            skip(count() * 4)
        elif msg in (15, 20):
            skip(4)
            skip(count() * 8)
        elif msg == 16:
            skip(1)
            skip(10 + count() * 13)
        elif msg in (21, 25):
            skip(1)
            skip(count() * 7)
        elif msg == 22:
            skip(5)
            skip(count() * 9)
        elif msg == 23:
            skip(8)
            skip(count() * 11)
            skip(count() * 11)
        elif msg == 26:
            skip(5)
            skip(count() * 8)
            skip(count() * 8)
        elif msg in (30, 31, 42):
            skip(1)
            skip(count() * 7)
        elif msg in (33, 39, 80, 81, 90):
            skip(1)
            skip(count() * 4)
        elif msg == 36:
            skip(1)
            skip(count() * 8)
        elif msg == 83:
            skip(count() * 4)
        elif msg in (130, 131):
            skip(1)
            skip(count())
        elif msg in (163, 164):
            skip(2)
            skip(struct.unpack_from("<H", data, cursor - 2)[0] + 1)
        else:
            raise ValueError("Unsupported native message: " + str(msg))
        yield data[start:cursor]


def public_query(data):
    if len(data) < 3 or data[1] > 1 or data[2] not in (2, 4, 8, 16, 32, 64):
        raise ValueError("Invalid public zone query")
    output = bytearray(data[:3])
    cursor = 3
    while cursor < len(data):
        length, = struct.unpack_from("<I", data, cursor)
        if length < 4 or cursor + length > len(data):
            raise ValueError("Invalid public query chunk")
        chunk = data[cursor:cursor + length]
        cursor += length
        if length == 4:
            output.extend(chunk)
            continue
        flags, = struct.unpack_from("<I", chunk, 4)
        position_offset = 8 + (4 if flags & 1 else 0)
        if not flags & 2 or length < position_offset + 4:
            raise ValueError("Public query lacks a position")
        position, = struct.unpack_from("<I", chunk, position_offset)
        facing = position >> 24
        private = (not facing & 5) if data[2] == 2 else bool(facing & 10)
        if private and data[2] != 16:
            # Clear identity, including all variable query fields, not just code.
            output.extend(struct.pack("<IIII", 16, 3, 0, position))
        else:
            output.extend(chunk)
    return bytes(output)


def public_message(data):
    """Apply the native SingleDuel observer visibility rules, fail closed."""
    if not data:
        raise ValueError("Empty game message")
    msg = data[0]
    if msg in PRIVATE:
        return None
    if msg == 6:
        return public_query(data)
    result = bytearray(data)
    if msg == 4:
        exact(data, 19)
        result[1] = 0x10
    elif msg == 2:
        exact(data, 7)
        if data[1] not in (4, 6, 7, 8, 9, 10, 11):
            return None
    elif msg in (30, 31, 42):
        if len(data) < 3:
            raise ValueError("Truncated card confirmation")
        exact(data, 3 + data[2] * 7)
        if msg == 31 and any(data[offset + 5] == 1 for offset in range(3, len(data), 7)):
            return None
    elif msg in (33, 39, 90):
        if len(data) < 3:
            raise ValueError("Truncated draw/shuffle")
        exact(data, 3 + data[2] * 4)
        for offset in range(3, len(data), 4):
            if msg != 90 or not data[offset + 3] & 0x80:
                result[offset:offset + 4] = bytes(4)
    elif msg in (36, 81, 83, 130, 131):
        start, unit = {36: (3, 8), 81: (3, 4), 83: (2, 4),
                       130: (3, 1), 131: (3, 1)}[msg]
        if len(data) < start:
            raise ValueError("Truncated public list")
        exact(data, start + data[start - 1] * unit)
    elif msg in FIXED:
        exact(data, FIXED[msg])
        if msg == 50 and hidden(data[10], data[12]):
            result[1:5] = bytes(4)
        elif msg == 53 and data[8] & 10 and data[9] & 10:
            result[1:5] = bytes(4)
        elif msg == 54:
            result[1:5] = bytes(4)
        elif msg == 55:
            for offset in (1, 9):
                if hidden(data[offset + 5], data[offset + 7]):
                    result[offset:offset + 4] = bytes(4)
        elif msg in (60, 62, 64) and hidden(data[6], data[8]):
            result[1:5] = bytes(4)
    else:
        raise ValueError("Unsupported public game message: " + str(msg))
    return bytes(result)


class Subscription:
    def __init__(self, limit):
        self.pending = deque()
        self.bytes = 0
        self.limit = limit
        self.closed = False
        self.condition = threading.Condition()

    def put(self, item):
        with self.condition:
            if self.closed:
                return
            if self.bytes + len(item[1]) + 3 > self.limit:
                self.closed = True
                self.pending.clear()
                self.bytes = 0
            else:
                self.pending.append(item)
                self.bytes += len(item[1]) + 3
            self.condition.notify_all()

    def get(self):
        with self.condition:
            if not self.pending and not self.closed:
                self.condition.wait(0.2)
            if self.pending:
                item = self.pending.popleft()
                self.bytes -= len(item[1]) + 3
                return item
            return None

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()


class SpectatorHub:
    def __init__(self, capacity=8, history_limit=16 * 1024 * 1024,
                 queue_limit=1024 * 1024):
        self.capacity = capacity
        self.history_limit = history_limit
        self.queue_limit = queue_limit
        self.lock = threading.RLock()
        self.clients = set()
        self.history = []
        self.history_bytes = 0
        self.session = None
        self.active = False
        self.started = False
        self.error = None
        self.enabled = True

    def configure(self, enabled, capacity):
        with self.lock:
            self.enabled, self.capacity = enabled, capacity
            if not enabled:
                for client in self.clients:
                    client.close()

    def status(self):
        with self.lock:
            return {"spectator_api": 1, "spectator_capacity": self.capacity,
                    "spectators": sum(not item.closed for item in self.clients),
                    "spectator_available": self.enabled and self.active and self.started,
                    "spectator_view": "public"}

    def begin(self, session):
        with self.lock:
            self.end()
            self.history = []
            self.history_bytes = 0
            self.session = session
            self.active = True
            self.started = False
            self.error = None

    def publish(self, protocol, payload=b""):
        with self.lock:
            if not self.active:
                return
            try:
                if protocol == 1:
                    items = [(1, filtered) for message in split_native_messages(payload)
                             if (filtered := public_message(message)) is not None]
                elif protocol == 9:
                    exact(payload, 12)
                    return  # MSG_START has absolute-seat deck counts.
                elif protocol in (0x15, 0x16):
                    exact(payload, 0)
                    items = [(protocol, bytes(payload))]
                else:
                    return  # Never forward chat, responses or full-deck YRP.
                if self.history_bytes + sum(len(p) + 3 for _, p in items) > self.history_limit:
                    raise ValueError("Public history limit reached")
            except (ValueError, struct.error) as error:
                self.error = str(error)
                self.end()
                return
            for item in items:
                self.history.append(item)
                self.history_bytes += len(item[1]) + 3
                if item[0] == 1 and item[1][0] == 4:
                    self.started = True
                for client in self.clients:
                    client.put(item)
            if protocol == 0x16:
                self.end()

    def end(self):
        with self.lock:
            if self.active and (not self.history or self.history[-1][0] != 0x16):
                for client in self.clients:
                    client.put((0x16, b""))
            self.active = False
            for client in self.clients:
                client.close()

    def subscribe(self):
        with self.lock:
            if not self.enabled or not self.active or not self.started:
                raise ValueError("No watchable duel")
            if len(self.clients) >= self.capacity:
                raise ValueError("Spectator room is full")
            client = Subscription(self.queue_limit)
            self.clients.add(client)
            return client, tuple(self.history)

    def unsubscribe(self, client):
        with self.lock:
            self.clients.discard(client)
            client.close()


def bounded_receive(client):
    length, = struct.unpack("<H", read_exact(client, 2))
    if not 1 <= length <= 1024:
        raise ValueError("Invalid spectator input size")
    body = read_exact(client, length)
    return body[0], body[1:]


def serve_spectator(client, hub, host_info, convert, model_label):
    subscription = None
    try:
        client.settimeout(3)
        deadline = time.monotonic() + 8
        for _ in range(8):
            protocol, payload = bounded_receive(client)
            if time.monotonic() > deadline:
                raise TimeoutError("Spectator handshake timeout")
            if protocol == 0x12:
                if len(payload) < 48:
                    raise ValueError("Truncated spectator join")
                password = payload[8:48].decode("utf-16-le").split("\0", 1)[0]
                profiles = {"S:YGOPro2": "YGOPro2", "S:legacy": "legacy"}
                if password not in profiles:
                    raise ValueError("Invalid spectator profile")
                profile = profiles[password]
                break
            if protocol not in (0x10, 0x17):
                raise ValueError("Invalid spectator handshake")
        else:
            raise ValueError("Missing spectator join")
        subscription, history = hub.subscribe()
        frames = packet(0x12, host_info()) + packet(0x13, b"\x07")
        for seat, name in enumerate(("Human", "Agent " + model_label)):
            frames += packet(0x20, name.encode("utf-16-le")[:38].ljust(40, b"\0") + bytes([seat]))
            frames += packet(0x21, bytes([(seat << 4) | 9]))
        frames += packet(0x22, struct.pack("<H", hub.status()["spectators"]))
        client.sendall(frames)

        def send(item):
            protocol, payload = item
            if protocol == 1:
                payload = convert(payload, profile)
            client.sendall(packet(protocol, payload))

        def consume():
            if select.select([client], [], [], 0)[0]:
                protocol, _ = bounded_receive(client)
                # Time acknowledgements and staying an observer have no authority.
                if protocol not in (0x15, 0x21):
                    raise ConnectionError("Read-only spectator")

        for item in history:
            consume()
            send(item)
        while True:
            consume()
            item = subscription.get()
            if item is not None:
                send(item)
            elif subscription.closed:
                break
    except (OSError, EOFError, ValueError, UnicodeError, struct.error):
        pass
    finally:
        if subscription is not None:
            hub.unsubscribe(subscription)
        client.close()


def listen_spectators(listener, hub, host_info, convert, model_label):
    # Keep handshake slots independent of a persisted, lower viewer limit.
    slots = threading.BoundedSemaphore(12)

    def serve(client):
        try:
            serve_spectator(client, hub, host_info, convert, model_label)
        finally:
            slots.release()

    while True:
        client, _ = listener.accept()
        if not slots.acquire(blocking=False):
            client.close()
            continue
        threading.Thread(target=serve, args=(client,), daemon=True).start()
