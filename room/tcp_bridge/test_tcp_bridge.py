import asyncio
import copy
import hashlib
import json
from pathlib import Path
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import unittest

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web
from aiohttp.test_utils import TestServer

from configure_tcp_bridge import create_config, write_private
from gateway import Invitations, create_app
from tcp_bridge import AttemptLimit, NativeBridge, packet

TOKEN = "t" * 43
PASSWORD = "RoomTest_12345678"
SECOND_PASSWORD = "OtherRoom_123456"
DECK = {"main": [123] * 40, "extra": [], "side": []}
ROOM = {"id": "ygo2-burn", "password_sha256": hashlib.sha256(PASSWORD.encode()).hexdigest(),
        "profile": "YGOPro2", "model_name": "Burn", "model_deck": DECK}
CONFIG = {"endpoint": "https://example.invalid", "token": TOKEN, "rooms": [ROOM]}
PREPARED = "D:" + "a" * 16


def join(password=PASSWORD, suffix=b""):
    field = password.encode("utf-16-le").ljust(40, b"\0")
    return packet(0x12, struct.pack("<H", 4961) + b"\0" * 6 + field + suffix)


def name():
    return packet(0x10, "Player".encode("utf-16-le").ljust(40, b"\0"))


async def wait_until(predicate):
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Condition did not become true")


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.invites = Path(self.directory.name) / "invitations.json"
        self.invites.write_text(json.dumps({"bridge": {
            "sha256": hashlib.sha256(TOKEN.encode()).hexdigest(), "enabled": True}}))
        self.api_calls = []
        self.prepared = []
        self.engine_connections = 0
        self.engine_active = set()
        self.health_state = "ready"
        self.prepare_status = 200
        self.prepare_password = PREPARED
        self.prepare_gate = asyncio.Event()
        self.prepare_gate.set()
        self.prepare_started = asyncio.Event()
        self.writers = []

        async def echo(reader, writer):
            self.engine_connections += 1
            self.engine_active.add(writer)
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            except (OSError, asyncio.CancelledError):
                pass
            finally:
                writer.close()
                await writer.wait_closed()
                self.engine_active.discard(writer)

        self.engine = await asyncio.start_server(echo, "127.0.0.1", 0)
        async def health(request):
            self.api_calls.append("health")
            return web.json_response({"state": self.health_state})
        async def prepare(request):
            self.api_calls.append("prepare")
            self.prepared.append(await request.json())
            self.prepare_started.set()
            await self.prepare_gate.wait()
            return web.json_response({"password": self.prepare_password},
                                     status=self.prepare_status)
        backend = web.Application()
        backend.router.add_get("/health", health)
        backend.router.add_post("/prepare", prepare)
        self.backend = TestServer(backend)
        await self.backend.start_server()
        self.gateway = TestServer(create_app(
            Invitations(self.invites), self.backend.port,
            self.engine.sockets[0].getsockname()[1]))
        await self.gateway.start_server()
        self.bridge = NativeBridge(copy.deepcopy(CONFIG))
        # Production config validation still requires HTTPS. Tests keep the
        # real gateway and TCP transport on loopback without an external CA.
        self.bridge.endpoint = str(self.gateway.make_url("")).rstrip("/")
        await self.bridge.start(port=0)
        self.port = self.bridge.listener.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.prepare_gate.set()
        for writer in self.writers:
            writer.close()
        await asyncio.gather(*(w.wait_closed() for w in self.writers), return_exceptions=True)
        await self.bridge.close()
        await self.gateway.close()
        await self.backend.close()
        self.engine.close()
        await self.engine.wait_closed()
        await wait_until(lambda: not self.engine_active)
        self.directory.cleanup()

    async def connect(self, data=b""):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.writers.append(writer)
        if data:
            writer.write(data)
            await writer.drain()
        return reader, writer

    async def rejection(self, reader):
        data = await asyncio.wait_for(reader.read(), 2)
        self.assertEqual(data, b"\x09\x00\x02\x01\x00\x00\x00\x00\x00\x00\x00")
        await wait_until(lambda: not self.bridge.active)

    async def establish(self):
        reader, writer = await self.connect(name() + join())
        expected = name() + join(PREPARED)
        self.assertEqual(await asyncio.wait_for(reader.readexactly(len(expected)), 3), expected)
        return reader, writer

    async def test_wrong_password_does_not_touch_upstream(self):
        with self.assertLogs("agent195m.tcp_bridge", level="INFO") as captured:
            reader, _ = await self.connect(join("WrongRoom_123456"))
            await self.rejection(reader)
        self.assertEqual(self.api_calls, [])
        self.assertEqual(self.engine_connections, 0)
        self.assertNotIn(TOKEN, str(captured.output))
        self.assertNotIn("WrongRoom_123456", str(captured.output))

    async def test_empty_or_unterminated_password_rejected(self):
        for password in ("", "x" * 20):
            reader, _ = await self.connect(join(password))
            await self.rejection(reader)
        self.assertEqual(self.api_calls, [])

    async def test_malformed_handshake_never_reaches_upstream(self):
        for data in (b"\0\0", struct.pack("<H", 1025), packet(0x12, b"short"),
                     packet(0x02, b"deck"), packet(0x10, b"short"),
                     packet(0x17, b"x" * 700) * 6,
                     packet(0x17) * 8,
                     join()[:11] + b"\0\xd8" + join()[13:]):
            reader, _ = await self.connect(data)
            await self.rejection(reader)
        self.assertEqual(self.api_calls, [])

    async def test_slow_handshake_times_out_without_upstream(self):
        self.bridge.handshake_timeout = 0.05
        reader, _ = await self.connect(b"\x31")
        await self.rejection(reader)
        self.assertEqual(self.api_calls, [])

    async def test_fragmented_join_and_pipelined_deck_are_preserved(self):
        reader, writer = await self.connect()
        suffix = b"\x01\x02\x03\x04"
        tail = packet(0x02, bytes(range(256)) * 20) + packet(0x22) * 3
        data = name() + join(suffix=suffix) + tail
        for part in (data[:1], data[1:13], data[13:57], data[57:]):
            writer.write(part)
            await writer.drain()
            await asyncio.sleep(0.005)
        expected = name() + join(PREPARED, suffix) + tail
        self.assertEqual(await asyncio.wait_for(reader.readexactly(len(expected)), 3), expected)
        self.assertEqual(self.prepared, [{
            "profile": "YGOPro2", "human_name": "Room selection", "model_name": "Burn",
            "human_deck": DECK, "model_deck": DECK}])
        self.assertEqual(self.engine_connections, 1)
        self.assertNotIn(PASSWORD.encode("utf-16-le"), expected)

    async def test_version_information_before_name_matches_native_client(self):
        version = packet(0x17, bytes(range(24)))
        reader, _ = await self.connect(version + name() + join())
        expected = version + name() + join(PREPARED)
        self.assertEqual(await asyncio.wait_for(reader.readexactly(len(expected)), 3), expected)
        self.assertEqual(self.engine_connections, 1)

    async def test_duplicate_names_do_not_reach_upstream(self):
        reader, _ = await self.connect(name() + name() + join())
        await self.rejection(reader)
        self.assertEqual(self.api_calls, [])

    async def test_preset_selects_explicit_legacy_profile_and_model(self):
        self.bridge.rooms.append({
            **ROOM, "id": "legacy-blue", "profile": "legacy", "model_name": "BlueEyes",
            "password_sha256": hashlib.sha256(SECOND_PASSWORD.encode()).hexdigest(),
            "model_deck": {**DECK, "main": [456] * 40}})
        reader, _ = await self.connect(join(SECOND_PASSWORD))
        self.assertEqual(await asyncio.wait_for(reader.readexactly(len(join(PREPARED))), 3),
                         join(PREPARED))
        self.assertEqual(self.prepared[0]["profile"], "legacy")
        self.assertEqual(self.prepared[0]["model_name"], "BlueEyes")
        self.assertEqual(self.prepared[0]["model_deck"]["main"], [456] * 40)

    async def test_large_post_join_binary_stream_is_byte_exact(self):
        reader, writer = await self.establish()
        data = bytes(range(256)) * 1024
        writer.write(data)
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(len(data)), 5), data)

    async def test_real_wss_verifies_certificate_and_transfers_binary(self):
        candidate = Path(r"C:\Program Files\Git\usr\bin\openssl.exe")
        openssl = str(candidate) if candidate.is_file() else shutil.which("openssl")
        if openssl is None:
            self.skipTest("OpenSSL required for ephemeral TLS test certificate")
        key = Path(self.directory.name) / "tls-test.key"
        cert = Path(self.directory.name) / "tls-test.pem"
        settings = Path(self.directory.name) / "openssl.cnf"
        settings.write_text("[req]\ndistinguished_name=dn\n[dn]\n")
        result = await asyncio.to_thread(subprocess.run, [
            openssl, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256",
            "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "1",
            "-config", str(settings),
            "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1"],
            timeout=10, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        await self.gateway.close()
        self.gateway = TestServer(create_app(
            Invitations(self.invites), self.backend.port,
            self.engine.sockets[0].getsockname()[1]), scheme="https")
        await self.gateway.start_server(ssl=tls)
        self.bridge.endpoint = str(self.gateway.make_url("")).rstrip("/")
        reader, _ = await self.connect(join())
        await self.rejection(reader)
        self.assertEqual(self.api_calls, [])
        await self.bridge.client.close()
        self.bridge.client = ClientSession(
            connector=TCPConnector(ssl=ssl.create_default_context(cafile=str(cert))),
            timeout=ClientTimeout(total=15), trust_env=False)
        reader, writer = await self.establish()
        data = bytes(range(256)) * 1024
        writer.write(data)
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.readexactly(len(data)), 5), data)

    async def test_full_setup_buffer_is_split_into_bounded_frames(self):
        self.prepare_gate.clear()
        reader, writer = await self.connect(join())
        await asyncio.wait_for(self.prepare_started.wait(), 2)
        data = bytes(range(256)) * 256
        writer.write(data)
        await writer.drain()
        await asyncio.sleep(0.05)
        self.prepare_gate.set()
        expected = join(PREPARED) + data
        self.assertEqual(await asyncio.wait_for(reader.readexactly(len(expected)), 5), expected)

    async def test_busy_bridge_rejects_without_second_prepare(self):
        first, writer = await self.establish()
        second, _ = await self.connect(join())
        self.assertEqual(await asyncio.wait_for(second.read(), 2),
                         b"\x09\x00\x02\x01\x00\x00\x00\x00\x00\x00\x00")
        self.assertEqual(self.api_calls, ["health", "prepare"])
        writer.write(b"still-playing")
        await writer.drain()
        self.assertEqual(await first.readexactly(13), b"still-playing")

    async def test_existing_remote_duel_is_not_interrupted(self):
        self.health_state = "playing"
        reader, _ = await self.connect(join())
        await self.rejection(reader)
        self.assertEqual(self.api_calls, ["health"])
        self.assertEqual(self.engine_connections, 0)

    async def test_disconnect_releases_room_and_allows_next_join(self):
        _, writer = await self.establish()
        writer.close()
        await writer.wait_closed()
        await wait_until(lambda: not self.bridge.occupied and not self.engine_active)
        await asyncio.sleep(0.05)
        await self.establish()
        self.assertEqual(self.engine_connections, 2)

    async def test_disconnect_during_prepare_cancels_setup(self):
        self.prepare_gate.clear()
        _, writer = await self.connect(join() + packet(0x02, b"pipelined"))
        await asyncio.wait_for(self.prepare_started.wait(), 2)
        writer.close()
        await writer.wait_closed()
        await wait_until(lambda: not self.bridge.occupied and not self.bridge.active)
        self.assertEqual(self.engine_connections, 0)

    async def test_setup_buffer_is_bounded(self):
        self.prepare_gate.clear()
        reader, writer = await self.connect(join())
        await asyncio.wait_for(self.prepare_started.wait(), 2)
        writer.write(b"x" * 65537)
        await writer.drain()
        await self.rejection(reader)
        self.assertFalse(self.bridge.occupied)
        self.assertEqual(self.engine_connections, 0)

    async def test_setup_deadline_releases_reservation(self):
        self.prepare_gate.clear()
        self.bridge.setup_timeout = 0.08
        reader, _ = await self.connect(join())
        await self.rejection(reader)
        self.assertFalse(self.bridge.occupied)

    async def test_upstream_failure_has_no_secret_in_log_or_error(self):
        self.prepare_status = 500
        self.prepare_password = TOKEN
        with self.assertLogs("agent195m.tcp_bridge", level="INFO") as captured:
            reader, _ = await self.connect(join())
            await self.rejection(reader)
        self.assertNotIn(TOKEN, str(captured.output))
        self.assertFalse(self.bridge.occupied)
        self.assertEqual(self.engine_connections, 0)

    async def test_bad_upstream_password_is_not_forwarded(self):
        self.prepare_password = TOKEN
        reader, _ = await self.connect(join())
        await self.rejection(reader)
        self.assertEqual(self.engine_connections, 0)

    async def test_idle_session_releases_engine(self):
        self.bridge.idle_timeout = 0.08
        reader, _ = await self.establish()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        await wait_until(lambda: not self.bridge.occupied and not self.engine_active)

    async def test_live_traffic_resets_idle_deadline(self):
        self.bridge.idle_timeout = 0.2
        reader, writer = await self.establish()
        for _ in range(5):
            await asyncio.sleep(0.06)
            writer.write(b"live")
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(4), 1), b"live")

    async def test_shutdown_cleans_active_tcp_and_websocket(self):
        reader, _ = await self.establish()
        await self.bridge.close()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        self.assertFalse(self.bridge.occupied)
        await wait_until(lambda: not self.engine_active)

    async def test_shutdown_during_prepare_releases_every_task(self):
        self.prepare_gate.clear()
        reader, _ = await self.connect(join())
        await asyncio.wait_for(self.prepare_started.wait(), 2)
        await self.bridge.close()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        self.assertFalse(self.bridge.occupied)
        self.assertFalse(self.bridge.active)
        self.assertEqual(self.engine_connections, 0)

    async def test_session_deadline_closes_even_with_live_transport(self):
        self.bridge.session_timeout = 0.08
        reader, _ = await self.establish()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b"")
        await wait_until(lambda: not self.bridge.occupied and not self.engine_active)

    async def test_per_ip_connections_are_bounded(self):
        for _ in range(4):
            await self.connect()
        reader, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        self.assertEqual(len(self.bridge.active), 4)
        self.assertEqual(self.api_calls, [])

    async def test_total_connection_limit(self):
        self.bridge.max_connections = 1
        await self.connect()
        reader, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        self.assertEqual(len(self.bridge.active), 1)


class BridgeConfigTests(unittest.TestCase):
    def test_cli_config_check_and_short_lived_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "bridge.json"
            write_private(config, CONFIG)
            script = Path(__file__).resolve().parent / "tcp_bridge.py"
            for flags in (["--check"], ["--host", "127.0.0.1", "--port", "0",
                                      "--duration", "0.1"]):
                result = subprocess.run(
                    [sys.executable, str(script), "--config", str(config), *flags],
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(TOKEN, result.stdout + result.stderr)
                self.assertNotIn(PASSWORD, result.stdout + result.stderr)

    def test_generated_password_fits_native_join_and_config_stores_hash_only(self):
        with tempfile.TemporaryDirectory() as directory:
            deck = Path(directory) / "Test.ydk"
            deck.write_text("#main\n" + "123\n" * 40 + "#extra\n!side\n")
            config, codes = create_config(CONFIG, [("test", "YGOPro2", deck)])
            password = codes[0]["password"]
            self.assertEqual(len(password), 16)
            self.assertNotIn(password, json.dumps(config))
            self.assertEqual(config["rooms"][0]["password_sha256"],
                             hashlib.sha256(password.encode()).hexdigest())
            self.assertEqual(len(join(password)), 51)
            path = Path(directory) / "private.json"
            write_private(path, config)
            with self.assertRaises(FileExistsError):
                write_private(path, {"replaced": True})
            self.assertEqual(json.loads(path.read_text()), config)

    def test_bad_config_is_rejected(self):
        invalid_rooms = ([], [ROOM, ROOM], [{**ROOM, "profile": "guess"}],
                         [{**ROOM, "id": "log\ninjection"}],
                         [{**ROOM, "password_sha256": "short"}],
                         [{**ROOM, "model_deck": {**DECK, "main": [123]}}],
                         [{**ROOM, "model_deck": {**DECK, "main": [True] * 40}}])
        for rooms in invalid_rooms:
            with self.subTest(rooms=rooms), self.assertRaises(ValueError):
                NativeBridge({**CONFIG, "rooms": rooms})
        for endpoint in ("http://public.invalid", "https://user:secret@public.invalid",
                         "https://public.invalid/path", "https://public.invalid/?token=secret"):
            with self.assertRaises(ValueError):
                NativeBridge({**CONFIG, "endpoint": endpoint})

    def test_rate_limit_per_ip_and_rotating_ip_table(self):
        limit = AttemptLimit()
        self.assertTrue(all(limit.allow("one") for _ in range(10)))
        self.assertFalse(limit.allow("one"))
        limit = AttemptLimit()
        self.assertTrue(all(limit.allow(str(i)) for i in range(120)))
        self.assertFalse(limit.allow("overflow"))
        self.assertLessEqual(len(limit.hosts), 120)


if __name__ == "__main__":
    unittest.main()
