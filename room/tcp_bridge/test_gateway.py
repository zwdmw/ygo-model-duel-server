import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from connector import fingerprint, validate_config
from gateway import Invitations, create_app


TOKEN = "a" * 43
AUTH = {"Authorization": "Bearer " + TOKEN}


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Path(self.directory.name) / "invitations.json"
        self.store.write_text(json.dumps({
            "guest": {"sha256": hashlib.sha256(TOKEN.encode()).hexdigest(), "enabled": True}
        }))
        self.connections = 0
        self.closed = asyncio.Event()
        async def echo(reader, writer):
            self.connections += 1
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
                self.closed.set()
        self.tcp = await asyncio.start_server(echo, "127.0.0.1", 0)
        self.tcp_port = self.tcp.sockets[0].getsockname()[1]
        backend = web.Application()
        async def health(request):
            return web.json_response({"state": "ready", "checkpoint_steps": 195035136,
                                      "policy_pid": 42, "session": "private",
                                      "last_error": "/private/path", "deck_selection_api": 1})
        async def prepare(request):
            return web.json_response(await request.json())
        backend.router.add_get("/health", health)
        backend.router.add_get("/catalog", health)
        backend.router.add_post("/prepare", prepare)
        self.backend = TestServer(backend)
        await self.backend.start_server()
        self.client = TestClient(TestServer(create_app(
            Invitations(self.store), self.backend.port, self.tcp_port)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.backend.close()
        self.tcp.close()
        await self.tcp.wait_closed()
        self.directory.cleanup()

    async def test_all_routes_require_invitation(self):
        for route in ("/health", "/catalog", "/duel", "/model/weights", "/admin"):
            response = await self.client.get(route)
            self.assertEqual(response.status, 401)
        response = await self.client.post("/prepare", json={})
        self.assertEqual(response.status, 401)
        self.assertEqual(self.connections, 0)

    async def test_browser_origins_rejected_even_with_token(self):
        response = await self.client.get("/health", headers={**AUTH, "Origin": "https://evil.invalid"})
        self.assertEqual(response.status, 403)

    async def test_health_filters_private_debug_information(self):
        response = await self.client.get("/health", headers=AUTH)
        result = await response.json()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["backend"], "remote_cpu")
        self.assertEqual(result["room_capacity"], 1)
        for key in ("policy_pid", "session", "last_error"):
            self.assertNotIn(key, result)

    async def test_no_file_or_admin_endpoints(self):
        for route in ("/model/weights", "/admin", "/sessions", "/../../etc/passwd"):
            response = await self.client.get(route, headers=AUTH)
            self.assertEqual(response.status, 404)

    async def test_prepare_forwards_only_json(self):
        response = await self.client.post("/prepare", headers=AUTH, json={"model_name": "Chosen"})
        self.assertEqual(await response.json(), {"model_name": "Chosen"})
        response = await self.client.post("/prepare", headers=AUTH, data="not JSON")
        self.assertEqual(response.status, 415)

    async def test_request_size_bounded(self):
        response = await self.client.post(
            "/prepare", headers={**AUTH, "Content-Type": "application/json"},
            data=b"x" * (128 * 1024 + 1))
        self.assertEqual(response.status, 413)

    async def test_token_revocation_without_service_restart(self):
        response = await self.client.get("/health", headers=AUTH)
        self.assertEqual(response.status, 200)
        await asyncio.sleep(0.02)
        self.store.write_text(json.dumps({"guest": {
            "sha256": hashlib.sha256(TOKEN.encode()).hexdigest(), "enabled": False}}))
        response = await self.client.get("/health", headers=AUTH)
        self.assertEqual(response.status, 401)

    async def test_single_room_and_binary_roundtrip(self):
        websocket = await self.client.ws_connect("/duel", headers=AUTH)
        payload = bytes(range(256)) * 4
        await websocket.send_bytes(payload)
        message = await websocket.receive(timeout=3)
        self.assertEqual(message.type, WSMsgType.BINARY)
        self.assertEqual(message.data, payload)
        response = await self.client.get("/duel", headers=AUTH)
        self.assertEqual(response.status, 409)
        await websocket.close()
        await asyncio.wait_for(self.closed.wait(), timeout=3)
        await asyncio.sleep(0.05)
        second = await self.client.ws_connect("/duel", headers=AUTH)
        await second.close()

    async def test_text_is_not_forwarded_to_engine(self):
        websocket = await self.client.ws_connect("/duel", headers=AUTH)
        await websocket.send_str("not a binary game packet")
        message = await websocket.receive(timeout=3)
        self.assertEqual(message.type, WSMsgType.CLOSE)
        self.assertEqual(message.data, 1003)
        await websocket.close()

    async def test_oversized_websocket_message_is_rejected(self):
        websocket = await self.client.ws_connect("/duel", headers=AUTH)
        await websocket.send_bytes(b"x" * 65537)
        message = await websocket.receive(timeout=3)
        self.assertEqual(message.type, WSMsgType.CLOSE)
        self.assertEqual(message.data, 1009)
        await websocket.close()


class ConnectorConfigTests(unittest.TestCase):
    def test_tls_required_and_secrets_not_in_address(self):
        for address in ("http://public.invalid", "https://user:secret@public.invalid",
                        "https://public.invalid/?token=bad", "https://public.invalid/#bad",
                        "https://public.invalid/path", "https://"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                validate_config({"endpoint": address, "token": TOKEN})

    def test_no_short_or_non_ascii_invitation(self):
        for token in ("", "123", "\u4e00" * 40):
            with self.assertRaises(ValueError):
                validate_config({"endpoint": "https://example.invalid", "token": token})

    def test_fingerprint_distinguishes_invites_without_exposing_them(self):
        config = {"endpoint": "https://example.invalid:8443", "token": TOKEN}
        result = fingerprint(config)
        self.assertEqual(len(result), 24)
        self.assertNotIn(TOKEN, result)
        self.assertNotEqual(result, fingerprint({**config, "token": "b" * 43}))


if __name__ == "__main__":
    unittest.main()
