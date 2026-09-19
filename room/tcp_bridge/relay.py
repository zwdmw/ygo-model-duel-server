"""Relay the existing player-filtered native bridge to the local human lobby."""
import os
import selectors
import socket
import struct
import sys


def packet(protocol, payload=b""):
    if len(payload) > 65534:
        raise ValueError("packet too large")
    return struct.pack("<HB", len(payload) + 1, protocol) + payload


def read_exact(sock, length):
    chunks = bytearray()
    while len(chunks) < length:
        part = sock.recv(length - len(chunks))
        if not part:
            raise EOFError("connection closed")
        chunks.extend(part)
    return bytes(chunks)


def receive(sock):
    length, = struct.unpack("<H", read_exact(sock, 2))
    if length == 0:
        raise ValueError("empty protocol frame")
    body = read_exact(sock, length)
    return body[0], body[1:]


def read_deck(path):
    main, extra, side = [], [], []
    current = main
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if line == "#extra":
            current = extra
        elif line == "!side":
            current = side
        elif line.isdigit():
            current.append(int(line))
    return main, extra, side


def deck_payload(main, extra, side):
    codes = main + extra + side
    return struct.pack("<II", len(main) + len(extra), len(side)) + struct.pack(
        f"<{len(codes)}I", *codes
    )


def main():
    args = dict(
        item.split("=", 1) for item in sys.argv[1:] if "=" in item
    )
    native = socket.create_connection(("127.0.0.1", int(args["Port"])), timeout=30)
    public = socket.create_connection(
        ("127.0.0.1", int(os.environ["MDPRO_RELAY_PORT"])), timeout=30
    )
    native.settimeout(900)
    public.settimeout(900)
    native.sendall(packet(0x10, "Human".encode("utf-16-le").ljust(40, b"\0")))
    native.sendall(packet(0x12, struct.pack("<H", 4961) + bytes(46)))
    native.sendall(packet(0x02, deck_payload(*read_deck(args["DeckFile"]))))
    native.sendall(packet(0x22))
    selector = selectors.DefaultSelector()
    selector.register(native, selectors.EVENT_READ)
    selector.register(public, selectors.EVENT_READ)
    try:
        while True:
            for key, _ in selector.select(timeout=900):
                source = key.fileobj
                protocol, payload = receive(source)
                if source is native:
                    if protocol in {0x12, 0x13, 0x20, 0x21}:
                        continue
                    public.sendall(packet(protocol, payload))
                    if protocol == 0x16:
                        return
                else:
                    if protocol in {0x13, 0x14}:
                        return
                    # Chat and time acknowledgements are not engine decisions.
                    if protocol == 0x01:
                        native.sendall(packet(protocol, payload))
    except (EOFError, ConnectionError):
        pass
    finally:
        selector.close()
        native.close()
        public.close()


if __name__ == "__main__":
    main()
