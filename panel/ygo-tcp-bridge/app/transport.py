"""Bounded binary WebSocket/TCP transport, shared by gateway and connector."""
import asyncio

from aiohttp import WSMsgType

CHUNK_SIZE = 32768


async def close_stream(writer):
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), 3)
    except asyncio.TimeoutError:
        writer.transport.abort()
    except OSError:
        pass


async def relay_stream(websocket, reader, writer, idle_timeout=None):
    loop = asyncio.get_running_loop()
    last_activity = loop.time()

    async def to_websocket():
        nonlocal last_activity
        while True:
            # Stay below the gateway's frame-size limit, including its boundary.
            data = await reader.read(CHUNK_SIZE)
            if not data:
                return
            last_activity = loop.time()
            await websocket.send_bytes(data)

    async def to_tcp():
        nonlocal last_activity
        async for message in websocket:
            if message.type == WSMsgType.BINARY:
                last_activity = loop.time()
                writer.write(message.data)
                await writer.drain()
            elif message.type == WSMsgType.TEXT:
                await websocket.close(code=1003, message=b"Binary protocol required")
                return
            elif message.type == WSMsgType.ERROR:
                raise ConnectionError("WebSocket transport failed")

    async def idle_watchdog():
        while True:
            remaining = last_activity + idle_timeout - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError("Game stream idle timeout")
            await asyncio.sleep(remaining)

    tasks = [asyncio.create_task(to_websocket()), asyncio.create_task(to_tcp())]
    if idle_timeout is not None:
        tasks.append(asyncio.create_task(idle_watchdog()))
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await close_stream(writer)
        try:
            await websocket.close()
        except OSError:
            pass
