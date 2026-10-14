#!/usr/bin/env python3
"""Standalone BLE beacon listener for Dromac.

Runs as its OWN process, separate from the main server, because CoreBluetooth
can hard-abort the whole interpreter (SIGABRT) if the calling process isn't
recognized by macOS as an app allowed to use Bluetooth. Isolating it here
means that kind of crash only takes down this helper -- never the HTTP
server actually controlling the phone. The main server respawns this helper
automatically if it exits.

Reports phone sightings back to the main server over loopback HTTP, so no
shared Python state / IPC beyond that is needed.
"""
import asyncio
import json
import sys
import urllib.request

try:
    from bleak import BleakScanner
except Exception:
    sys.exit(1)

MANUFACTURER_ID = 0xFFFF
MAIN_SERVER_URL = "http://127.0.0.1:8811/internal/ble_hit"


def decode_payload(data):
    # ip1[, ip2, ip3] (4 bytes each, best first) then port (2 bytes); the
    # original single-address format is just the 6-byte case, and a phone with
    # no address at all (mobile data only) sends just the port.
    if len(data) < 2 or (len(data) - 2) % 4 != 0:
        return None
    ips = [".".join(str(b) for b in data[i:i + 4]) for i in range(0, len(data) - 2, 4)]
    port = (data[-2] << 8) | data[-1]
    return ips, port


def report_hit(ips, port):
    try:
        body = json.dumps({"ip": ips[0] if ips else "", "ips": ips, "port": port}).encode("utf-8")
        req = urllib.request.Request(
            MAIN_SERVER_URL, data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


def on_detection(device, advertisement_data):
    try:
        raw = (advertisement_data.manufacturer_data or {}).get(MANUFACTURER_ID)
        if not raw:
            return
        decoded = decode_payload(bytes(raw))
        if decoded:
            report_hit(*decoded)
    except Exception:
        pass


async def scan_forever():
    async with BleakScanner(detection_callback=on_detection):
        while True:
            await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(scan_forever())
    except Exception:
        sys.exit(1)
