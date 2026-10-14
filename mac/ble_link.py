#!/usr/bin/env python3
"""Bluetooth data link for Dromac (the Mac end of the phone's BleLink).

The fallback for when neither the USB cable nor Wi-Fi can reach the phone.
Serves plain HTTP on 127.0.0.1:18823 and relays each request to the phone over
a Bluetooth LE GATT connection, so the main server just treats this as one more
phone address and every endpoint keeps working, only slower.

Runs as its own process for the same reason as ble_helper.py: CoreBluetooth can
abort the interpreter outright, and that must never take the main server down.
Connects on the first request and drops the connection after a quiet spell, so
nothing is held open while the phone is reached some other way.

Framing (matches BleLink.kt): every write/notification starts with one byte,
1 = last frame of the message. Request {k, m, p, b}; reply {s, t, b(base64)}.
"""
import asyncio
import base64
import json
import sys
from pathlib import Path

try:
    from bleak import BleakClient, BleakScanner
except Exception:
    sys.exit(1)

PORT = 18823
MANUFACTURER_ID = 0xFFFF
SERVICE_UUID = "d20a0001-7c2e-4b8a-9f3e-6a1d5c0b7e01"
REQUEST_UUID = "d20a0002-7c2e-4b8a-9f3e-6a1d5c0b7e01"
RESPONSE_UUID = "d20a0003-7c2e-4b8a-9f3e-6a1d5c0b7e01"
KEY_FILE = Path.home() / "Library/Application Support/Dromac/ble_key"
IDLE_DISCONNECT = 30   # seconds without a request before letting the link go
REPLY_TIMEOUT = 20
MAX_BODY = 512 * 1024

LOG_FILE = Path.home() / "Library/Application Support/Dromac/ble_link.log"
_client = None


def log(msg):
    import time
    try:
        with open(LOG_FILE, "a") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except Exception:
        pass
_reply_frames = bytearray()
_reply_done = None
_link_lock = asyncio.Lock()
_last_used = 0.0


def _on_notify(_, data: bytearray):
    global _reply_frames
    if not data:
        return
    _reply_frames += data[1:]
    if data[0] == 1 and _reply_done and not _reply_done.done():
        _reply_done.set_result(bytes(_reply_frames))


def _on_disconnect(_):
    global _client
    _client = None


async def _ensure_connected():
    global _client
    if _client and _client.is_connected:
        return _client
    device = await BleakScanner.find_device_by_filter(
        lambda d, ad: MANUFACTURER_ID in (ad.manufacturer_data or {})
        or SERVICE_UUID in [u.lower() for u in (ad.service_uuids or [])],
        timeout=8,
    )
    if device is None:
        log("scan: phone not found")
        raise ConnectionError("phone not in Bluetooth range")
    log(f"scan: found {device.address} ({device.name}), connecting")
    client = BleakClient(device, disconnected_callback=_on_disconnect)
    await client.connect(timeout=10)
    log(f"connected, mtu={client.mtu_size}, services={[s.uuid for s in client.services]}")
    await client.start_notify(RESPONSE_UUID, _on_notify)
    log("notifications on")
    _client = client
    return client


async def _relay(method, path, body):
    global _reply_frames, _reply_done, _last_used
    key = KEY_FILE.read_text().strip() if KEY_FILE.exists() else ""
    if not key:
        return 503, "application/json", b'{"error":"no bluetooth pairing key yet: connect the phone by USB once"}'
    message = {"k": key, "m": method, "p": path}
    if body:
        message["b"] = body.decode("utf-8", "replace")
    payload = json.dumps(message).encode("utf-8")
    async with _link_lock:
        client = await _ensure_connected()
        # ATT payload is MTU - 3, minus our one-byte frame header; attribute
        # values are capped at 512 bytes.
        chunk = min(511, max(19, (client.mtu_size or 23) - 4))
        _reply_frames = bytearray()
        _reply_done = asyncio.get_running_loop().create_future()
        for i in range(0, len(payload), chunk):
            last = i + chunk >= len(payload)
            frame = bytes([1 if last else 0]) + payload[i:i + chunk]
            await client.write_gatt_char(REQUEST_UUID, frame, response=True)
        log(f"sent {method} {path} ({len(payload)} bytes in {chunk}-byte frames)")
        raw = await asyncio.wait_for(_reply_done, REPLY_TIMEOUT)
        log(f"reply {len(raw)} bytes")
        _last_used = asyncio.get_running_loop().time()
    reply = json.loads(raw.decode("utf-8"))
    return int(reply.get("s", 502)), reply.get("t") or "application/octet-stream", base64.b64decode(reply.get("b") or "")


async def _handle(reader, writer):
    status, ctype, data = 502, "application/json", b'{"error":"bluetooth link failed"}'
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        lines = head.decode("latin-1").split("\r\n")
        method, path = lines[0].split(" ")[0:2]
        length = 0
        for line in lines[1:]:
            if line.lower().startswith("content-length:"):
                length = min(int(line.split(":", 1)[1].strip() or 0), MAX_BODY)
        body = await reader.readexactly(length) if length else b""
        status, ctype, data = await _relay(method, path, body)
    except ConnectionError as e:
        status, data = 504, json.dumps({"error": str(e)}).encode()
    except Exception as e:
        log(f"failed: {type(e).__name__}: {e}")
        status, data = 502, json.dumps({"error": f"bluetooth link failed: {type(e).__name__}: {e}"}).encode()
    try:
        writer.write(
            f"HTTP/1.1 {status} X\r\nContent-Type: {ctype}\r\nContent-Length: {len(data)}\r\n"
            f"Connection: close\r\n\r\n".encode("latin-1") + data
        )
        await writer.drain()
    finally:
        writer.close()


async def _idle_reaper():
    while True:
        await asyncio.sleep(5)
        if _client and _client.is_connected and asyncio.get_running_loop().time() - _last_used > IDLE_DISCONNECT:
            try:
                await _client.disconnect()
            except Exception:
                pass


async def main():
    server = await asyncio.start_server(_handle, "127.0.0.1", PORT)
    asyncio.get_running_loop().create_task(_idle_reaper())
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        sys.exit(1)
