#!/usr/bin/env python3
"""Dromac backend: talks to the Dromac companion app on the phone over plain HTTP
(no adb / USB / ADB wireless debugging involved) and serves the dashboard."""

import difflib
import hashlib
import ipaddress
import unicodedata
import json
import mimetypes
import os
import plistlib
import re
import socket
import subprocess
import sys
import threading
import time
from collections import deque
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

try:
    from zeroconf import Zeroconf, ServiceBrowser
    HAVE_ZEROCONF = True
except Exception:
    HAVE_ZEROCONF = False

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
STATE_FILE = ROOT / "state.json"
PORT = 8811

# FileDrop is a separate, standalone LAN file-sharing server (its own
# server.py, own port) -- Dromac only knows how to check whether it's up,
# spawn it as a detached background process if not, and pop its page open
# in its own app-style Chrome window, mirroring how Dromac's own dashboard
# opens as a window rather than a plain browser tab.
FILEDROP_DIR = Path.home() / "Library" / "Application Support" / "FileDrop"
FILEDROP_SERVER = FILEDROP_DIR / "server.py"
FILEDROP_PORT = 8900


def filedrop_status():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{FILEDROP_PORT}/api/server-info", timeout=1.5) as resp:
            info = json.loads(resp.read().decode("utf-8"))
        return {"running": True, "url": info.get("url")}
    except Exception:
        return {"running": False, "url": None}


def start_filedrop():
    status = filedrop_status()
    if status["running"]:
        return status
    if not FILEDROP_SERVER.exists():
        return {"running": False, "url": None, "error": "FileDrop is not installed"}
    log_path = FILEDROP_DIR / "server.log"
    with open(log_path, "a") as log_file:
        subprocess.Popen(
            [sys.executable, str(FILEDROP_SERVER)],
            stdout=log_file, stderr=log_file,
            cwd=str(FILEDROP_DIR), start_new_session=True,
        )
    for _ in range(40):  # up to ~4s for the server to come up
        time.sleep(0.1)
        status = filedrop_status()
        if status["running"]:
            break
    return status


# CodeGate is its own project (github.com/yatharth1011/codegate) with its own
# server: Dromac just checks whether it's up, starts it if not, and drives its
# admin API, which only answers loopback requests carrying X-CodeGate-Local.
CODEGATE_DIR = Path.home() / "Library" / "Application Support" / "CodeGate"
CODEGATE_SERVER = CODEGATE_DIR / "server.py"
CODEGATE_PORT = 8902


def codegate_up():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{CODEGATE_PORT}/", timeout=1.5):
            return True
    except Exception:
        return False


def start_codegate():
    if codegate_up():
        return True, None
    if not CODEGATE_SERVER.exists():
        return False, "CodeGate isn't installed (github.com/yatharth1011/codegate)"
    with open(CODEGATE_DIR / "server.log", "a") as log_file:
        subprocess.Popen(
            [sys.executable, str(CODEGATE_SERVER)],
            stdout=log_file, stderr=log_file,
            cwd=str(CODEGATE_DIR), start_new_session=True,
        )
    for _ in range(60):  # up to ~6s for it to come up
        time.sleep(0.1)
        if codegate_up():
            return True, None
    return False, "CodeGate didn't start (see server.log in its folder)"


def codegate_request(method, path, body=None, timeout=10):
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{CODEGATE_PORT}{path}", data=data, method=method,
        headers={"X-CodeGate-Local": "1", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"CodeGate returned {e.code}"}
    except Exception:
        return {"codegateRunning": False}


CHROME_BINARY = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

_FOCUS_FILEDROP_SCRIPT = '''
tell application "Google Chrome"
    repeat with w in windows
        if name of w is "FileDrop" then
            set index of w to 1
            set minimized of w to false
            activate
            return "focused"
        end if
    end repeat
    return "not_found"
end tell
'''


def _focus_filedrop_window():
    try:
        result = subprocess.run(
            ["osascript", "-e", _FOCUS_FILEDROP_SCRIPT],
            capture_output=True, text=True, timeout=3,
        )
        return result.stdout.strip() == "focused"
    except Exception:
        return False


def open_filedrop_window(url):
    # A repeat click was silently spawning ANOTHER window every time instead
    # of bringing the existing one forward -- each one opened fine, just
    # behind whatever's already in front, so clicking looked like it did
    # nothing while windows quietly piled up. Reuse and focus an existing
    # window first; only open a fresh one if there isn't one yet.
    if _focus_filedrop_window():
        return
    # Chrome's --app mode opens a borderless, tab-less window for a URL --
    # the same look as an installed PWA shortcut (like Dromac's own window)
    # without needing FileDrop to actually be installed as one. Going through
    # `open -na` launches a genuinely separate Chrome process every time;
    # Chrome hands that off to the already-running instance via IPC and the
    # spare process exits, but macOS still registers the launch with
    # LaunchServices first, leaving a transient "ghost" Dock icon (and a
    # stray Dock recents entry) behind each time. Exec'ing the binary
    # directly skips LaunchServices entirely -- Chrome's own single-instance
    # handoff still works exactly the same either way.
    if CHROME_BINARY.exists():
        subprocess.Popen([str(CHROME_BINARY), f"--app={url}"])
    else:
        subprocess.Popen(["open", "-a", "Google Chrome", url])
    # The window doesn't exist yet the instant the process is spawned; give
    # Chrome a moment to create it, then bring it to the front explicitly --
    # a freshly opened app-mode window doesn't always steal focus on its own.
    threading.Timer(0.8, _focus_filedrop_window).start()


def pick_folder(prompt="Choose a folder"):
    """Native macOS folder picker; None if cancelled."""
    try:
        out = subprocess.run(
            ["osascript", "-e", 'POSIX path of (choose folder with prompt "' + prompt.replace('"', "") + '")'],
            capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return None
    return out.stdout.strip() or None


# Liquid-glass theme: the dashboard renders Rainy Desktop's rain shader over
# the desktop picture. Rainy serves its live settings/clock/wallpaper on a
# loopback bridge that deliberately sends no CORS headers (so web pages can't
# read it) -- Dromac proxies it server-side instead. When Rainy isn't
# running, settings come from its saved preferences and the wallpaper is
# read the same way Rainy itself does (NSWorkspace's desktop image URL).
RAINY_BRIDGE = "http://127.0.0.1:47823"
RAINY_PREFS = Path.home() / "Library" / "Preferences" / "com.rainydesktop.app.plist"
RAINY_SHADER_KEYS = (
    "rainIntensity", "rainSpeed", "staticDropDensity", "layer1Density", "layer2Density",
    "fogMinBlur", "fogMaxBlurLow", "fogMaxBlurHigh", "refractionStrength", "dropZoomOut",
    "lightningBoost", "lightningSpeed", "lightningSharpness",
    "colorGradeStrength", "vignetteStrength", "brightness", "dimAmount",
    "zoomAmount", "zoomSpeed",
)
WALLPAPER_CACHE_DIR = ROOT / ".wallpaper"
_BROWSER_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                        ".webp": "image/webp", ".gif": "image/gif"}
_desktop_image_cache = {"at": 0.0, "path": None}


def _rainy_bridge_get(path, timeout=0.5):
    req = urllib.request.Request(RAINY_BRIDGE + path)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(), resp.headers.get("Content-Type", "")


def _desktop_image_path():
    # Cached briefly: the dashboard polls every couple of seconds and this
    # spawns osascript. JXA's ObjC bridge calls NSWorkspace directly, so no
    # Automation permission prompt (unlike asking System Events).
    now = time.time()
    if now - _desktop_image_cache["at"] < 5:
        return _desktop_image_cache["path"]
    path = None
    try:
        out = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e",
             'ObjC.import("AppKit"); $.NSWorkspace.sharedWorkspace.desktopImageURLForScreen($.NSScreen.mainScreen).path.js'],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        if out and Path(out).is_file():
            path = Path(out)
    except Exception:
        pass
    _desktop_image_cache.update(at=now, path=path)
    return path


def _desktop_image_signature():
    path = _desktop_image_path()
    if not path:
        return "none"
    try:
        return f"{path}|{int(path.stat().st_mtime)}"
    except OSError:
        return "none"


def _read_rainy_prefs():
    try:
        with open(RAINY_PREFS, "rb") as f:
            prefs = plistlib.load(f)
    except Exception:
        return None, False
    settings = {k: float(prefs[f"rain.{k}"]) for k in RAINY_SHADER_KEYS if f"rain.{k}" in prefs}
    return settings, bool(prefs.get("rain.isPaused", False))


def rainy_state():
    try:
        raw, _ = _rainy_bridge_get("/state.json")
        state = json.loads(raw)
        return {
            "source": "rainy",
            "settings": state.get("settings"),
            "time": state.get("time"),
            "paused": state.get("paused", False),
            "wallpaperVersion": f"rainy:{state.get('version')}",
        }
    except Exception:
        pass
    settings, paused = _read_rainy_prefs()
    return {
        "source": "prefs" if settings is not None else "none",
        "settings": settings,
        "time": None,
        "paused": paused,
        "wallpaperVersion": f"desktop:{_desktop_image_signature()}",
    }


def rainy_wallpaper():
    """(bytes, content_type) of the wallpaper the theme should render, or
    (None, None) if there's none to be had (the client then uses a plain
    dark gradient, same as Rainy's own fallback)."""
    try:
        raw, ctype = _rainy_bridge_get("/wallpaper.jpg", timeout=2)
        if raw:
            return raw, ctype or "image/jpeg"
    except Exception:
        pass
    path = _desktop_image_path()
    if not path:
        return None, None
    ext = path.suffix.lower()
    try:
        if ext in _BROWSER_IMAGE_TYPES:
            return path.read_bytes(), _BROWSER_IMAGE_TYPES[ext]
        # HEIC/TIFF/etc. -- browsers can't decode these, so convert once per
        # wallpaper version with sips and reuse the result.
        WALLPAPER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha1(_desktop_image_signature().encode()).hexdigest()[:16]
        out = WALLPAPER_CACHE_DIR / f"{key}.jpg"
        if not out.exists():
            for old in WALLPAPER_CACHE_DIR.glob("*.jpg"):
                old.unlink(missing_ok=True)
            subprocess.run(["sips", "-s", "format", "jpeg", "-Z", "3840", str(path), "--out", str(out)],
                           capture_output=True, timeout=20)
        if out.exists():
            return out.read_bytes(), "image/jpeg"
    except Exception:
        pass
    return None, None


CAMERA_SAVE_DIR = Path.home() / "Documents" / "Dromac"
# A received capture (from either capture source) is never written into
# Documents/Dromac automatically anymore -- it only lands in this single
# scratch slot (outside the visible folder, so it never piles up on its own),
# gets auto-copied to the clipboard, and stays there for the dashboard's
# save/copy buttons until either Save writes it into Documents/Dromac or the
# next capture overwrites it.
PENDING_PHOTO_DIR = ROOT / ".pending"
PENDING_PHOTO_PATH = PENDING_PHOTO_DIR / "pending.jpg"
_pending_photo_lock = threading.Lock()
_pending_photo_at = 0.0


def set_pending_photo(raw):
    """Records a freshly received capture as the pending shot and copies it
    to the clipboard immediately -- shared by every capture source (the Mac's
    own live-preview capture button, and the phone's dedicated camera screen
    drained via camera_poll_loop below). Does NOT touch Documents/Dromac;
    that only happens if/when the Save button is clicked (see /api/camera/save)."""
    global _pending_photo_at
    PENDING_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_PHOTO_PATH.write_bytes(raw)
    with _pending_photo_lock:
        _pending_photo_at = time.time()
    copy_image_to_clipboard(str(PENDING_PHOTO_PATH))


def save_pending_photo():
    """The Save button's action: copies the current pending shot into
    Documents/Dromac with a unique timestamped name. Nothing is saved unless
    this is explicitly called."""
    with _pending_photo_lock:
        has_pending = _pending_photo_at > 0
    if not has_pending or not PENDING_PHOTO_PATH.exists():
        return None
    CAMERA_SAVE_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"dromac_{time.strftime('%Y%m%d_%H%M%S')}_{int(time.time() * 1000) % 1000:03d}.jpg"
    (CAMERA_SAVE_DIR / filename).write_bytes(PENDING_PHOTO_PATH.read_bytes())
    return filename


def clear_camera_folder():
    """Empties Documents/Dromac (deletes the saved photos, keeps the folder
    itself) so it doesn't quietly pile up -- an explicit, user-triggered
    action only, never automatic."""
    if not CAMERA_SAVE_DIR.exists():
        return 0
    count = 0
    for f in CAMERA_SAVE_DIR.iterdir():
        if f.is_file():
            try:
                f.unlink()
                count += 1
            except Exception:
                pass
    return count


def copy_image_to_clipboard(path):
    """Puts an actual image (not just its path/filename) on the macOS
    clipboard, so a captured photo can be pasted straight into Messages,
    Notes, etc. pbcopy only handles text -- AppleScript's clipboard setter is
    the standard way to put image data on the pasteboard from a shell call."""
    try:
        escaped = str(path).replace("\\", "\\\\").replace('"', '\\"')
        script = f'set the clipboard to (read (POSIX file "{escaped}") as JPEG picture)'
        subprocess.run(["osascript", "-e", script], timeout=5, check=True, capture_output=True)
        return True, None
    except subprocess.CalledProcessError as e:
        return False, (e.stderr or b"").decode("utf-8", "ignore") or str(e)
    except Exception as e:
        return False, str(e)


CAMERA_POLL_INTERVAL = 3  # seconds between drains of the phone's pending-capture mailbox


def camera_poll_loop():
    """Drains photos taken with the phone's own dedicated camera screen
    (StationServerService's pending-capture mailbox) into Documents/Dromac.
    Runs independently of the main watchdog -- only needs a known, reachable
    phone, not full /api/state polling -- and keeps draining in a tight loop
    whenever something's waiting so a burst of shots syncs quickly instead of
    trickling in one per interval."""
    while True:
        try:
            state = load_state()
            if state.get("phone_host"):
                while True:
                    raw, ctype = phone_request("GET", "/api/camera/pending", timeout=6, binary=True)
                    if raw is None or not (ctype or "").startswith("image/"):
                        break
                    try:
                        set_pending_photo(raw)
                        print("[camera] synced phone capture -> pending (copied to clipboard)", file=sys.stderr)
                    except Exception as e:
                        print(f"[camera] failed to stage pending capture: {e}", file=sys.stderr)
                        break
        except Exception:
            pass
        time.sleep(CAMERA_POLL_INTERVAL)

PHONE_PORT_DEFAULT = 8822
PHONE_TIMEOUT = 4
MDNS_SERVICE_TYPE = "_dromac._tcp.local."

_state_lock = threading.Lock()
_last_status = {"connected": False, "checked_at": 0}


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"phone_host": None, "phone_port": PHONE_PORT_DEFAULT, "last_connected": None, "method": None}


def save_state(state):
    with _state_lock:
        STATE_FILE.write_text(json.dumps(state))


def phone_base_url():
    state = load_state()
    host = state.get("phone_host")
    if not host:
        return None
    port = state.get("phone_port") or PHONE_PORT_DEFAULT
    return f"http://{host}:{port}"


def phone_request(method, path, body=None, timeout=PHONE_TIMEOUT, binary=False):
    base = phone_base_url()
    if not base:
        return None, "no phone address set"
    url = base + path
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if binary:
                return raw, resp.headers.get("Content-Type", "application/octet-stream")
            return json.loads(raw.decode("utf-8")), None
    except urllib.error.HTTPError as e:
        return None, f"phone returned HTTP {e.code}"
    except Exception as e:
        return None, str(e)


def is_phone_connected():
    state = load_state()
    host = state.get("phone_host")
    port = state.get("phone_port")
    if not host:
        _last_status["connected"] = False
        _last_status["checked_at"] = time.time()
        return False
    data, err = phone_request("GET", "/api/state", timeout=3)
    ok = data is not None
    _last_status["connected"] = ok
    _last_status["checked_at"] = time.time()
    if ok:
        # Re-read and only stamp last_connected if the host we just verified
        # is still the one on record -- a blind reload+patch here can race
        # with a concurrent forget()/BLE-triggered host swap on another
        # request thread and resurrect a stale/cleared state with a fresh
        # timestamp, which is exactly the "host: null but last_connected: now"
        # nonsense that made the watchdog look alive while doing nothing.
        current = load_state()
        if current.get("phone_host") == host and current.get("phone_port") == port:
            current["last_connected"] = time.time()
            save_state(current)
    return ok


def default_gateway():
    try:
        out = subprocess.run(
            ["route", "-n", "get", "default"], capture_output=True, text=True, timeout=3
        ).stdout
        m = re.search(r"gateway:\s*(\S+)", out)
        return m.group(1) if m else None
    except Exception:
        return None


def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def local_network():
    """The Mac's actual current subnet, using its real netmask — many hotspots
    and campus networks hand out something bigger than /24 (e.g. /22), and
    assuming /24 silently misses hosts outside that narrower guessed range."""
    ip = local_ip()
    if not ip:
        return None
    prefix = 24
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
        for block in out.split("\n\n"):
            if f"inet {ip} " in block:
                m = re.search(r"netmask (0x[0-9a-fA-F]+)", block)
                if m:
                    prefix = bin(int(m.group(1), 16)).count("1")
                break
    except Exception:
        pass
    try:
        network = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
    except Exception:
        return None
    # Cap scan size for pathologically large subnets so a scan can't balloon
    # to tens of thousands of hosts; fall back to the /24 around our own IP.
    if network.num_addresses > 4096:
        network = ipaddress.ip_network(f"{ip}/24", strict=False)
    return network


def probe_dromac(ip, port=PHONE_PORT_DEFAULT, timeout=0.35):
    try:
        req = urllib.request.Request(f"http://{ip}:{port}/api/state")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return "now_playing" in data and "battery" in data
    except Exception:
        return False




def discover_via_mdns(timeout=4):
    """Network-independent discovery: the phone advertises itself by name over
    mDNS/Bonjour (not by IP), so this works on any network — home, hotspot, or
    a large routed campus — without any assumption about address ranges."""
    if not HAVE_ZEROCONF:
        return None

    found = {"ip": None, "port": None}
    zc = Zeroconf()

    class Listener:
        def add_service(self, zeroconf, service_type, name):
            info = zeroconf.get_service_info(service_type, name, timeout=1500)
            if info and info.addresses:
                found["ip"] = socket.inet_ntoa(info.addresses[0])
                found["port"] = info.port

        def update_service(self, zeroconf, service_type, name):
            pass

        def remove_service(self, zeroconf, service_type, name):
            pass

    try:
        ServiceBrowser(zc, MDNS_SERVICE_TYPE, Listener())
        waited = 0.0
        while waited < timeout and not found["ip"]:
            time.sleep(0.2)
            waited += 0.2
    except Exception:
        pass
    finally:
        try:
            zc.close()
        except Exception:
            pass

    return found if found["ip"] else None


_last_scan_attempt = 0
RESCAN_INTERVAL = 20  # seconds between automatic rescans while disconnected


def watchdog():
    """Automatic background reconnection using only the CHEAP methods: the
    gateway fast-path and a quick mDNS browse (a few seconds, event-driven —
    no network flooding). BLE discovery runs independently and usually wins
    the race (see handle_ble_hit).

    Deliberately does NOT auto-trigger the expanding-ring IP scan: that walks
    up to a /12 (~786k addresses) and can take up to ~30 minutes, so it should
    only ever run when a person explicitly asks for it, never as a silent
    background fallback that keeps grinding after the phone's already found."""
    global _last_scan_attempt
    while True:
        try:
            state = load_state()
            host = state.get("phone_host")
            if host and is_phone_connected():
                pass  # still reachable at the known address, nothing to do
            else:
                now = time.time()
                if now - _last_scan_attempt >= RESCAN_INTERVAL:
                    _last_scan_attempt = now
                    gw = default_gateway()
                    if gw and probe_dromac(gw):
                        save_state({"phone_host": gw, "phone_port": PHONE_PORT_DEFAULT, "last_connected": time.time(), "method": "gateway"})
                    else:
                        result = discover_via_mdns(timeout=3)
                        if result and probe_dromac(result["ip"], result.get("port") or PHONE_PORT_DEFAULT):
                            save_state({
                                "phone_host": result["ip"],
                                "phone_port": result.get("port") or PHONE_PORT_DEFAULT,
                                "last_connected": time.time(), "method": "mdns",
                            })
        except Exception:
            pass
        time.sleep(6)


# ---------- BLE proximity beacon ----------
# The phone continuously advertises a tiny BLE packet ("here's my current
# IP:port") via manufacturer data. This is topology-blind — it works purely by
# radio proximity, so it finds the phone instantly even across routed
# subnets/VLANs where IP scanning and mDNS multicast can't reach, as long as
# it's physically near the Mac. No button, no scanning: this just listens
# continuously in the background for as long as the app runs.
#
# The actual CoreBluetooth scanning runs in a SEPARATE process (ble_helper.py)
# and reports sightings back over loopback HTTP (/internal/ble_hit below).
# CoreBluetooth can hard-abort the whole interpreter if macOS doesn't
# recognize the calling process as Bluetooth-authorized (SIGABRT, no
# exception to catch) -- isolating it in its own process means that failure
# mode can only kill the helper, never the server actually controlling the
# phone. If the helper dies for any reason it's respawned automatically.

BLE_HELPER_PATH = ROOT / "ble_helper.py"
_last_ble_hit = 0
BLE_PROBE_COOLDOWN = 3  # seconds; the phone re-advertises many times per second

# Diagnostics, deliberately independent of the reconnect logic above: this
# answers "is a BLE packet from the phone reaching this Mac at all", which is
# a different question from "did we act on it" (that's gated by the cooldown
# and by already being pointed at that address). When auto-reconnect stops
# working, checking this first tells us whether the fault is on the radio
# side (phone not advertising / helper not scanning) or in the reconnect
# logic further down -- without that split, both look identical from outside.
_ble_lock = threading.Lock()
_ble_last_seen_at = 0.0
_ble_last_seen_addr = None
_ble_hit_count = 0
_ble_last_logged_at = 0.0
BLE_LOG_INTERVAL = 15  # seconds; the phone re-advertises constantly, don't flood server.log

_ble_helper_alive = False
_ble_helper_last_exit_at = 0.0


def ble_signal_status():
    with _ble_lock:
        seen_at = _ble_last_seen_at
        addr = _ble_last_seen_addr
        count = _ble_hit_count
    return {
        "helperRunning": _ble_helper_alive,
        "lastSeenAgoSec": (time.time() - seen_at) if seen_at else None,
        "lastAddr": addr,
        "hitCount": count,
    }


def handle_ble_hit(ip, port):
    global _last_ble_hit, _ble_last_seen_at, _ble_last_seen_addr, _ble_hit_count, _ble_last_logged_at
    now = time.time()
    # Record the raw sighting unconditionally, before any of the "should we
    # act on this" logic below -- this is the part that must keep updating
    # even when we're already connected or on cooldown, so it truly reflects
    # radio reception rather than reconnect activity.
    with _ble_lock:
        _ble_last_seen_at = now
        _ble_last_seen_addr = f"{ip}:{port}"
        _ble_hit_count += 1
        if now - _ble_last_logged_at >= BLE_LOG_INTERVAL:
            _ble_last_logged_at = now
            print(f"[ble] packet received from {ip}:{port} (total seen: {_ble_hit_count})", file=sys.stderr)
    try:
        state = load_state()
        if state.get("phone_host") == ip and state.get("phone_port") == port:
            return  # already pointed at this exact address, nothing to do
        if now - _last_ble_hit < BLE_PROBE_COOLDOWN:
            return
        _last_ble_hit = now
        if probe_dromac(ip, port):
            print(f"[ble] reconnected via {ip}:{port}", file=sys.stderr)
            save_state({"phone_host": ip, "phone_port": port, "last_connected": time.time(), "method": "ble"})
        else:
            print(f"[ble] packet from {ip}:{port} but probe_dromac failed (not reachable / wrong port?)", file=sys.stderr)
    except Exception:
        pass


def ble_supervisor():
    """Keeps the isolated BLE helper process running indefinitely, respawning
    it (with a short backoff) whenever it exits -- crash, Bluetooth adapter
    reset, permission prompt, anything. Runs until the app itself is quit."""
    global _ble_helper_alive, _ble_helper_last_exit_at
    if not BLE_HELPER_PATH.exists():
        print("[ble] ble_helper.py not found, BLE discovery disabled", file=sys.stderr)
        return
    while True:
        try:
            proc = subprocess.Popen(
                [sys.executable, str(BLE_HELPER_PATH)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            _ble_helper_alive = True
            exit_code = proc.wait()
            _ble_helper_alive = False
            _ble_helper_last_exit_at = time.time()
            if exit_code != 0:
                print(f"[ble] ble_helper.py exited with code {exit_code}, respawning in 5s", file=sys.stderr)
        except Exception as e:
            _ble_helper_alive = False
            print(f"[ble] failed to spawn ble_helper.py: {e}", file=sys.stderr)
        time.sleep(5)


# ---------- recently played ----------

_recent_tracks = deque(maxlen=5)


def log_recent_track(title, artist):
    if not title:
        return
    entry = {"title": title, "artist": artist or ""}
    if _recent_tracks and _recent_tracks[0] == entry:
        return
    _recent_tracks.appendleft(entry)


# ---------- OTP passthrough ----------
# The phone extracts a verification code from incoming SMS (see SmsOtpReceiver
# on the Android side) and reports it via /api/state; this copies it straight
# to the Mac's own clipboard the moment a NEW code shows up, so it's a paste
# away without touching the phone. Only the extracted code ever reaches here,
# never the SMS body.

_last_otp_copied = None


def handle_otp(otp):
    global _last_otp_copied
    if otp == _last_otp_copied:
        return
    _last_otp_copied = otp
    try:
        subprocess.run(["pbcopy"], input=otp.encode("utf-8"), timeout=2)
    except Exception:
        pass


# ---------- artwork ----------

_artwork_cache = {}
_ARTWORK_TTL = 3600


def get_itunes_artwork(title, artist):
    if not title:
        return None
    key = (title.strip().lower(), (artist or "").strip().lower())
    cached = _artwork_cache.get(key)
    if cached and time.time() - cached["ts"] < _ARTWORK_TTL:
        return cached["url"]
    term = f"{title} {artist}".strip()
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": term, "entity": "song", "limit": 1}
    )
    art_url = None
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        results = data.get("results") or []
        if results:
            raw = results[0].get("artworkUrl100")
            if raw:
                art_url = raw.replace("100x100bb", "600x600bb")
    except Exception:
        art_url = None
    _artwork_cache[key] = {"url": art_url, "ts": time.time()}
    return art_url


# ---------- lyrics ----------
# lrclib.net is a free, keyless, public database of time-synced (LRC format)
# lyrics -- no signup, no API key, matches how the rest of this app avoids
# needing accounts for anything.

_lyrics_cache = {}
_LYRICS_TTL = 3600
_LRC_LINE = re.compile(r"^\[(\d+):(\d+(?:\.\d+)?)\](.*)$")


def _parse_lrc(text):
    lines = []
    for raw_line in text.splitlines():
        m = _LRC_LINE.match(raw_line.strip())
        if not m:
            continue
        minutes, seconds, lyric = m.groups()
        ms = int((int(minutes) * 60 + float(seconds)) * 1000)
        lyric = lyric.strip()
        if lyric:
            lines.append({"t": ms, "text": lyric})
    lines.sort(key=lambda l: l["t"])
    return lines


def _lrclib_get(title, artist):
    try:
        url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(
            {"track_name": title, "artist_name": artist or ""}
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Dromac/1.0"})
        with urllib.request.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        synced = data.get("syncedLyrics")
        if synced:
            return _parse_lrc(synced), data.get("duration")
    except Exception:
        pass
    return None, None


# We're matching against YouTube Music titles, which put an artist name and a
# song title together in every possible order and punctuation style --
# "Title - Artist", "Artist - Title", "Title | Artist", an artist name that's
# actually a quality tag like "4K", etc. Trying to parse out "the title" vs
# "the artist" by position/delimiter (a pipe, a hyphen, ...) is exactly a
# fixed-logic, whack-a-mole approach: it only covers whichever ordering it was
# written for, and breaks the moment a real title shows up in the other order
# or the split point isn't actually an artist at all.
#
# So this doesn't try to parse the string's structure at all. It sends a
# lightly-cleaned version (brackets stripped -- that part's format-agnostic)
# to lrclib's own search, then re-ranks every candidate purely by how much of
# that candidate's OWN title/artist text can be found as a contiguous run
# somewhere inside our messy raw string, in ANY position. "Dusk Till Dawn"
# scores perfectly against "zayn & sia - dusk till dawn (slowed + reverb) |
# 4k" whether the real title sits at the front, the back, or the middle --
# there's no assumption about order to get backwards.
_SEARCH_MIN_SIMILARITY = 0.45


def _contained_similarity(candidate, haystack):
    candidate = (candidate or "").strip().lower()
    haystack = (haystack or "").strip().lower()
    if not candidate or not haystack:
        return 0.0
    matcher = difflib.SequenceMatcher(None, haystack, candidate, autojunk=False)
    match = matcher.find_longest_match(0, len(haystack), 0, len(candidate))
    score = match.size / len(candidate)
    # A very short candidate title ("Bhare", one word) trivially scores a
    # perfect 1.0 the moment it appears ANYWHERE inside a long messy string --
    # that's not actually a confident match, just a short string being easy to
    # find. Taper the score down for candidates under ~8 characters so a
    # coincidental short match can't outscore (and silently replace) a longer,
    # genuinely specific one.
    if len(candidate) < 8:
        score *= len(candidate) / 8
    return score


# lrclib's own search needs a reasonably clean query to return ANYTHING --
# feeding it the whole messy string (artist, title, and noise all jammed
# together) tends to come back empty, before our own re-ranking ever gets a
# candidate to look at. So each common separator (|, -/–/—) is used to
# mechanically split the string into fragments and EVERY fragment is tried as
# its own query -- not because we've decided which fragment is "the title",
# just because one of them usually results in a clean enough hit. Whichever
# candidate, from whichever fragment, actually scores best against the
# original raw string (see _contained_similarity) wins.
def _split_fragments(title):
    parts = re.split(r"\s*[|｜]\s*|\s+[-–—]\s+", title or "")
    return [p.strip() for p in parts if p.strip()]


# Descriptor words tacked on with no bracket/separator at all ("Bhare Naina
# Slowed Reverb" -- no punctuation whatsoever around "Slowed Reverb") can't be
# split off by any punctuation-based rule, and lrclib's search is strict
# enough that even ONE leftover word ("Bhare Naina Slowed") returns nothing.
# Real audio-effect/fan-edit descriptors are overwhelmingly appended as
# trailing words rather than prepended, so progressively dropping words off
# the END is a general way to find a query that actually gets a hit --
# without deciding which specific word is or isn't noise.
def _truncation_candidates(title):
    words = (title or "").split()
    # Never truncate down to a single leftover word -- a lone word is both a
    # weak, ambiguous query on its own and (per the short-candidate taper
    # above) prone to spuriously matching something unrelated.
    max_cut = min(3, len(words) - 2)
    return [" ".join(words[:len(words) - cut]) for cut in range(1, max_cut + 1)]


def _lrclib_search(raw_title, raw_artist=""):
    cleaned = _general_clean_title(raw_title)
    fragments = _split_fragments(cleaned)

    queries = []
    for q in [cleaned] + fragments + _truncation_candidates(cleaned):
        if q and q not in queries:
            queries.append(q)

    best, best_score = None, 0.0
    seen_ids = set()
    for query in queries:
        try:
            url = "https://lrclib.net/api/search?" + urllib.parse.urlencode({"track_name": query})
            req = urllib.request.Request(url, headers={"User-Agent": "Dromac/1.0"})
            with urllib.request.urlopen(req, timeout=4) as resp:
                results = json.loads(resp.read().decode("utf-8"))
        except Exception:
            continue
        for r in results or []:
            if not r.get("syncedLyrics"):
                continue
            rid = r.get("id")
            if rid is not None and rid in seen_ids:
                continue
            seen_ids.add(rid)
            candidate_title = r.get("trackName", "")
            candidate_artist = r.get("artistName", "")
            # Scoring title against the WHOLE raw string is what let the old
            # version misfire: the raw string still contains the artist name
            # as literal text ("... - ARIJIT SINGH"), so ANY candidate whose
            # title happens to contain that name (a totally different song by
            # the same artist) picked up a big, coincidental title_score. When
            # a real delimiter split the string into fragments, try each
            # fragment as the "title" in turn, scoring artist against the
            # OTHER fragments -- still no assumption about which side of the
            # delimiter is which, just never let the same text count as both
            # the title match and the artist match at once.
            if len(fragments) > 1:
                score = 0.0
                for i, title_guess in enumerate(fragments):
                    other_hints = [f for j, f in enumerate(fragments) if j != i]
                    if raw_artist:
                        other_hints.append(raw_artist)
                    title_score = _contained_similarity(candidate_title, title_guess)
                    artist_score = max(
                        (_contained_similarity(candidate_artist, hint) for hint in other_hints),
                        default=0.0,
                    )
                    score = max(score, title_score * 0.65 + artist_score * 0.35)
            else:
                title_score = _contained_similarity(candidate_title, cleaned)
                artist_score = _contained_similarity(candidate_artist, raw_artist) if raw_artist else 0.0
                score = title_score * 0.65 + artist_score * 0.35
            if score > best_score:
                best, best_score = r, score
    if best and best_score >= _SEARCH_MIN_SIMILARITY:
        return _parse_lrc(best["syncedLyrics"]), best.get("duration")
    return None, None


# Fan-made "slowed + reverb" (and similar: "slowed down", "slow reverb") edits
# keep the original song's title/artist tags but stretch the actual playback
# tempo, so the track plays noticeably LONGER than the original -- lrclib
# only indexes the original, unedited song, timed against its true (shorter)
# duration. Used raw against a slowed track, those timestamps run fast and
# drift the synced lyrics further behind the audio as the track plays.
_SLOWED_TITLE_RE = re.compile(r"(?i)slowed|reverb")


def is_slowed_title(title):
    return bool(_SLOWED_TITLE_RE.search(title or ""))


# Brackets/parens almost never contain part of the real searchable title in
# ANY of these messy title conventions -- unlike trying to enumerate every
# fan-edit descriptor phrase ("Slowed & Reverb", "Underwater", "Extended",
# "Sped Up", "Nightcore", ...) or every "Title - Artist" vs "Artist - Title"
# convention, "strip anything bracketed" is a format-agnostic rule that holds
# up regardless of what's actually inside them or what order things appear in.
_BRACKETED_RE = re.compile(r"[\(\[][^)\]]*[\)\]]")

# "Slowed + Reverb" (and "Slow Reverb", "Slowed & Reverb", ...) is often typed
# OUTSIDE any brackets, with real artist/uploader info chained after it via a
# further separator ("Sahiba Slowed + Reverb | Aditya Rikhari | ..."). The
# bare "cut at the first +" rule below (for trailing junk like "+ Underwater &
# Extended") can't tell that apart from a genuine "+" inside the descriptor
# phrase itself, and ends up chopping off that trailing artist info too. Strip
# the descriptor phrase itself first, using the same slowed/reverb vocabulary
# already used to detect these edits, so the "+" cut only ever catches
# unrelated trailing junk.
_SLOWED_PHRASE_RE = re.compile(r"(?i)\bslow(?:ed)?\b\s*(?:[+&/,]|and)?\s*\breverb\b")


def _general_clean_title(title):
    cleaned = _BRACKETED_RE.sub(" ", title or "")
    cleaned = _SLOWED_PHRASE_RE.sub(" ", cleaned)
    plus_idx = cleaned.find("+")
    if plus_idx != -1:
        cleaned = cleaned[:plus_idx]
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -–—.,|｜([])")
    return cleaned if cleaned else title


def _normalize_unicode(text):
    """Some uploaders style titles with Unicode "Mathematical Alphanumeric"
    lookalike letters (or similar decorative blocks) for a stylized font look
    -- these are entirely different codepoints from plain Latin letters, so
    no amount of fuzzy string matching sees them as related to the real
    title. NFKD compatibility decomposition is exactly what Unicode defines
    to map those styled forms back to their plain equivalents -- a general
    fix, not a workaround for this one specific title."""
    if not text:
        return text
    return unicodedata.normalize("NFKD", text)


def get_lyrics(title, artist, duration_ms=None):
    if not title:
        return None
    title = _normalize_unicode(title)
    artist = _normalize_unicode(artist)
    key = (title.strip().lower(), (artist or "").strip().lower(), duration_ms)
    cached = _lyrics_cache.get(key)
    if cached and time.time() - cached["ts"] < _LYRICS_TTL:
        return cached["lines"]

    slowed = is_slowed_title(title)
    clean_title = _general_clean_title(title)

    # Cheap exact-lookup attempts first (covers the case lrclib's title is
    # already an exact or near-exact match); the fuzzy, contained-similarity
    # search above is what actually carries messy real-world titles.
    lines, original_duration = _lrclib_get(title, artist)
    if not lines and clean_title != title:
        lines, original_duration = _lrclib_get(clean_title, artist)
    if not lines:
        lines, original_duration = _lrclib_search(title, artist)

    if slowed:
        print(f"[lyrics] slowed title={title!r} duration_ms={duration_ms} original_duration={original_duration} lines={bool(lines)}", file=sys.stderr)
    if lines and slowed and duration_ms and original_duration:
        original_ms = original_duration * 1000
        scale = duration_ms / original_ms if original_ms > 0 else 1
        print(f"[lyrics] computed scale={scale}", file=sys.stderr)
        # Only stretch if the matched "original" genuinely looks shorter (a
        # real slowdown, not just a mismatched search result), and keep the
        # factor within a plausible range so a bad match can't wildly distort
        # the timeline instead of just leaving it unstretched.
        if 1.02 <= scale <= 2.5:
            lines = [{"t": int(l["t"] * scale), "text": l["text"]} for l in lines]
            print("[lyrics] stretch applied", file=sys.stderr)
        else:
            print(f"[lyrics] stretch SKIPPED, scale {scale} outside [1.02, 2.5]", file=sys.stderr)

    _lyrics_cache[key] = {"lines": lines, "ts": time.time()}
    return lines


def lyrics_at_position(lines, position_ms):
    """Same current/prev/next line lookup as the dashboard's own
    highlightLyricsAt -- kept in one place so /api/external/now-playing
    can't silently drift from what the dashboard itself shows."""
    idx = -1
    for i, line in enumerate(lines):
        if line["t"] <= position_ms:
            idx = i
        else:
            break
    if idx < 0:
        return None, None, None
    current = lines[idx]["text"]
    prev = lines[idx - 1]["text"] if idx > 0 else None
    nxt = lines[idx + 1]["text"] if idx + 1 < len(lines) else None
    return current, prev, nxt


# ---------- HTTP handler ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "Dromac/2.0"

    def log_message(self, fmt, *args):
        pass

    # This server only listens on loopback, but any web page open in the
    # browser can still aim requests at it: via DNS rebinding (a wrong Host)
    # or a plain cross-site request (form post, <img src>). Either could read
    # the phone's data or fire actions such as opening a CodeGate room.
    # Legitimate callers (Dromac's own page, the launcher, ble_helper, Rainy
    # Desktop) all use a loopback Host and are never cross-site.
    _LOOPBACK_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
    _OWN_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
    _CROSS_SITE_OK = {"/api/artwork"}  # read-only image, may be embedded elsewhere

    def _request_allowed(self, path):
        if (self.headers.get("Host") or "").lower() not in self._LOOPBACK_HOSTS:
            return False
        if path in self._CROSS_SITE_OK and self.command == "GET":
            return True
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self._OWN_ORIGINS:
            return False
        return self.headers.get("Sec-Fetch-Site") not in ("cross-site", "same-site")

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"
        safe = os.path.normpath(path).lstrip("/")
        fpath = (STATIC_DIR / safe).resolve()
        if not str(fpath).startswith(str(STATIC_DIR.resolve())) or not fpath.exists():
            self.send_response(404)
            self.end_headers()
            return
        ctype = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
        data = fpath.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if fpath.name == "sw.js":
            self.send_header("Service-Worker-Allowed", "/")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        p = parsed.path
        if not self._request_allowed(p):
            return self._json({"error": "forbidden"}, 403)

        if p == "/api/state":
            state = load_state()
            phone_data, err = phone_request("GET", "/api/state")
            connected = phone_data is not None
            payload = {
                "connected": connected,
                "host": state.get("phone_host"),
                "port": state.get("phone_port"),
                "method": state.get("method"),
                "device_model": None,
                "now_playing": None,
                "battery": None,
                "volume": None,
                "error": None if connected else err,
                "ble": ble_signal_status(),
            }
            with _pending_photo_lock:
                payload["cameraPendingAt"] = _pending_photo_at or None
            if connected:
                state["last_connected"] = time.time()
                save_state(state)
                payload["device_model"] = phone_data.get("device_model")
                payload["battery"] = phone_data.get("battery")
                vol = phone_data.get("volume")
                if vol:
                    payload["volume"] = {"level": vol.get("level", 0), "min": 0, "max": vol.get("max", 15)}
                np = phone_data.get("now_playing")
                if np:
                    log_recent_track(np.get("title"), np.get("artist"))
                    artwork = None
                    if np.get("hasArt"):
                        artwork = f"/api/artwork?t={int(time.time())}"
                    else:
                        artwork = get_itunes_artwork(np.get("title"), np.get("artist"))
                    payload["now_playing"] = {
                        "title": np.get("title"),
                        "artist": np.get("artist"),
                        "playing": np.get("playing", False),
                        "artwork": artwork,
                        "liked": np.get("liked"),
                        "likeAction": np.get("likeAction"),
                        "dislikeAction": np.get("dislikeAction"),
                        "position": np.get("position", 0),
                        "duration": np.get("duration", -1),
                    }
                payload["dnd"] = phone_data.get("dnd", False)
                payload["dndAvailable"] = phone_data.get("dndAvailable", False)
                payload["flashlight"] = phone_data.get("flashlight", False)
                payload["keepAwake"] = phone_data.get("keepAwake", False)
                payload["lockAvailable"] = phone_data.get("lockAvailable", False)
                payload["locked"] = phone_data.get("locked", False)
                payload["blackout"] = phone_data.get("blackout", False)
                payload["overlayGranted"] = phone_data.get("overlayGranted", False)
                payload["keyboardActive"] = phone_data.get("keyboardActive", False)
                payload["mirrorGranted"] = phone_data.get("mirrorGranted", False)
                payload["mirrorActive"] = phone_data.get("mirrorActive", False)
                payload["accessibilityAvailable"] = phone_data.get("accessibilityAvailable", False)
                payload["cameraGranted"] = phone_data.get("cameraGranted", False)
                payload["incomingCall"] = phone_data.get("incomingCall")
                payload["nextEvent"] = phone_data.get("nextEvent")
                payload["screenTimeTodayMin"] = phone_data.get("screenTimeTodayMin", -1)
                otp = phone_data.get("otp")
                payload["otp"] = otp
                if otp:
                    handle_otp(otp)
                payload["recentTracks"] = list(_recent_tracks)[1:]  # [0] is the current track
            return self._json(payload)

        if p == "/api/artwork":
            raw, ctype = phone_request("GET", "/api/artwork", binary=True)
            if raw is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype or "image/jpeg")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)
            return

        if p == "/api/screen/stream":
            base = phone_base_url()
            if not base:
                self.send_response(502)
                self.end_headers()
                return
            try:
                upstream = urllib.request.urlopen(base + "/api/screen/stream", timeout=10)
                self.send_response(200)
                self.send_header("Content-Type", upstream.headers.get("Content-Type", "multipart/x-mixed-replace"))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while True:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:
                pass
            return

        if p == "/api/camera/stream":
            base = phone_base_url()
            if not base:
                self.send_response(502)
                self.end_headers()
                return
            try:
                upstream = urllib.request.urlopen(base + "/api/camera/stream", timeout=10)
                self.send_response(200)
                self.send_header("Content-Type", upstream.headers.get("Content-Type", "multipart/x-mixed-replace"))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while True:
                    chunk = upstream.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:
                pass
            return

        if p == "/api/notifications":
            data, err = phone_request("GET", "/api/notifications")
            if data is None:
                return self._json({"notifications": [], "error": err}, 502)
            return self._json(data)

        if p == "/api/apps":
            data, err = phone_request("GET", "/api/apps", timeout=6)
            if data is None:
                return self._json({"error": err}, 502)
            return self._json(data)

        if p == "/api/location":
            data, err = phone_request("GET", "/api/location")
            if data is None:
                return self._json({"error": err}, 502)
            return self._json(data)

        if p == "/api/camera/last":
            with _pending_photo_lock:
                has_pending = _pending_photo_at > 0
            if not has_pending or not PENDING_PHOTO_PATH.exists():
                self.send_response(404)
                self.end_headers()
                return
            data = PENDING_PHOTO_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        if p == "/api/lyrics":
            qs = urllib.parse.parse_qs(parsed.query)
            title = (qs.get("title") or [""])[0]
            artist = (qs.get("artist") or [""])[0]
            duration_raw = (qs.get("duration") or [""])[0]
            try:
                duration_ms = int(duration_raw) if duration_raw else None
            except ValueError:
                duration_ms = None
            lines = get_lyrics(title, artist, duration_ms)
            return self._json({"lines": lines})

        if p == "/api/filedrop/status":
            return self._json(filedrop_status())

        if p == "/api/codegate/status":
            if not codegate_up():
                return self._json({"codegateRunning": False, "installed": CODEGATE_SERVER.exists()})
            return self._json(codegate_request("GET", "/api/status"))

        if p in ("/api/rainy/state", "/api/rainy/wallpaper"):
            # Rainy's bridge only answers requests whose Host is loopback
            # (a DNS-rebinding guard); proxying it must not become a way
            # around that, so apply the same check here.
            host = (self.headers.get("Host") or "").lower()
            if host not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
                return self._json({"error": "forbidden"}, 403)
            if p == "/api/rainy/state":
                return self._json(rainy_state())
            data, ctype = rainy_wallpaper()
            if data is None:
                return self._json({"error": "no wallpaper"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return

        if p == "/api/external":
            return self._json({
                "endpoints": {
                    "/api/external/now-playing": {
                        "method": "GET",
                        "description": "Current phone playback, derived artwork, and synced lyrics -- "
                                        "a stable contract for other apps on this Mac, separate from "
                                        "/api/state (which is internal to the dashboard and can change shape).",
                        "returns": {
                            "connected": "bool",
                            "title": "string|null", "artist": "string|null",
                            "playing": "bool",
                            "positionMs": "int (position at the moment of this request; extrapolate "
                                          "forward yourself using `playing` if you need it live)",
                            "durationMs": "int|null",
                            "artworkUrl": "string|null -- absolute URL, safe to fetch directly",
                            "lyrics": {
                                "available": "bool",
                                "current": "string|null", "prev": "string|null", "next": "string|null",
                                "lines": "[{t: ms, text: string}]|null -- full synced timeline, "
                                         "for building your own live ticker",
                            },
                        },
                    },
                },
            })

        if p == "/api/external/now-playing":
            phone_data, _err = phone_request("GET", "/api/state")
            payload = {
                "connected": phone_data is not None,
                "title": None, "artist": None, "playing": False,
                "positionMs": 0, "durationMs": None, "artworkUrl": None,
                "lyrics": {"available": False, "current": None, "prev": None, "next": None, "lines": None},
            }
            np = phone_data.get("now_playing") if phone_data else None
            if np:
                title = np.get("title")
                artist = np.get("artist")
                position_ms = np.get("position", 0)
                duration_ms = np.get("duration")
                if not duration_ms or duration_ms <= 0:
                    duration_ms = None

                if np.get("hasArt"):
                    artwork_url = f"http://127.0.0.1:{PORT}/api/artwork?t={int(time.time())}"
                else:
                    artwork_url = get_itunes_artwork(title, artist)

                lines = get_lyrics(title, artist, duration_ms) if title else None
                lyrics_payload = {"available": bool(lines), "current": None, "prev": None, "next": None, "lines": lines}
                if lines:
                    current, prev, nxt = lyrics_at_position(lines, position_ms)
                    lyrics_payload.update({"current": current, "prev": prev, "next": nxt})

                payload.update({
                    "title": title,
                    "artist": artist,
                    "playing": np.get("playing", False),
                    "positionMs": position_ms,
                    "durationMs": duration_ms,
                    "artworkUrl": artwork_url,
                    "lyrics": lyrics_payload,
                })
            return self._json(payload)

        return self._serve_static(p)

    def do_POST(self):
        parsed = urlparse(self.path)
        p = parsed.path
        if not self._request_allowed(p):
            return self._json({"error": "forbidden"}, 403)
        body = self._read_json()

        if p == "/internal/ble_hit":
            ip = body.get("ip", "")
            port = body.get("port")
            if ip and port:
                handle_ble_hit(ip, int(port))
            return self._json({"ok": True})

        if p in (
            "/api/media/play_pause", "/api/media/next", "/api/media/prev",
            "/api/volume/up", "/api/volume/down", "/api/notifications/clear_all",
        ):
            _, err = phone_request("POST", p)
            return self._json({"ok": err is None, "error": err})

        if p == "/api/volume/set":
            level = body.get("level")
            if level is None:
                return self._json({"error": "level required"}, 400)
            _, err = phone_request("POST", p, {"level": level})
            return self._json({"ok": err is None, "error": err})

        if p == "/api/media/seek":
            position_ms = body.get("positionMs")
            if position_ms is None:
                return self._json({"error": "positionMs required"}, 400)
            _, err = phone_request("POST", p, {"positionMs": position_ms})
            return self._json({"ok": err is None, "error": err})

        if p == "/api/flashlight/toggle":
            data, err = phone_request("POST", p)
            return self._json({"ok": err is None, "on": (data or {}).get("on", False), "error": err})

        if p == "/api/dnd/toggle":
            enabled = bool(body.get("enabled"))
            data, err = phone_request("POST", p, {"enabled": enabled})
            ok = err is None and bool((data or {}).get("ok"))
            return self._json({"ok": ok, "error": err})

        if p == "/api/call/answer":
            _, err = phone_request("POST", p)
            return self._json({"ok": err is None, "error": err})

        if p == "/api/apps/launch":
            pkg = body.get("package", "")
            if not pkg:
                return self._json({"error": "package required"}, 400)
            _, err = phone_request("POST", p, {"package": pkg})
            return self._json({"ok": err is None, "error": err})

        if p == "/api/screen/wake":
            _, err = phone_request("POST", p)
            return self._json({"ok": err is None, "error": err})

        if p == "/api/screen/keep_awake/toggle":
            data, err = phone_request("POST", p)
            return self._json({"ok": err is None, "on": (data or {}).get("on", False), "error": err})

        if p == "/api/screen/lock":
            _, err = phone_request("POST", p)
            return self._json({"ok": err is None, "error": err})

        if p == "/api/screen/blackout/toggle":
            data, err = phone_request("POST", p)
            return self._json({
                "ok": err is None and bool((data or {}).get("ok")),
                "on": (data or {}).get("on", False),
                "error": err or (data or {}).get("error"),
            })

        if p == "/api/screen/nav":
            action = body.get("action", "")
            data, err = phone_request("POST", p, {"action": action})
            return self._json({
                "ok": err is None and bool((data or {}).get("ok")),
                "error": err or (data or {}).get("error"),
            })

        if p == "/api/keyboard/text":
            text = body.get("text", "")
            data, err = phone_request("POST", p, {"text": text})
            return self._json({
                "ok": err is None and bool((data or {}).get("ok")),
                "error": err or (data or {}).get("error"),
            })

        if p == "/api/keyboard/key":
            key = body.get("key", "")
            data, err = phone_request("POST", p, {"key": key})
            return self._json({
                "ok": err is None and bool((data or {}).get("ok")),
                "error": err or (data or {}).get("error"),
            })

        if p == "/api/camera/start":
            facing = body.get("facing", "back")
            flash = bool(body.get("flash", False))
            data, err = phone_request("POST", p, {"facing": facing, "flash": flash}, timeout=8)
            return self._json({
                "ok": err is None and bool((data or {}).get("ok")),
                "error": err or (data or {}).get("error"),
            })

        if p == "/api/camera/photo":
            raw, ctype = phone_request("POST", p, timeout=8, binary=True)
            if raw is None:
                return self._json({"ok": False, "error": ctype}, 502)
            if not (ctype or "").startswith("image/"):
                # Capture failed phone-side -- it responds 200 with a JSON
                # error body instead of JPEG bytes in that case, so urlopen
                # doesn't raise; only the content type gives it away.
                try:
                    err = json.loads(raw.decode("utf-8")).get("error", "capture failed")
                except Exception:
                    err = "capture failed"
                return self._json({"ok": False, "error": err}, 502)
            try:
                set_pending_photo(raw)
            except Exception as e:
                return self._json({"ok": False, "error": f"captured but failed to stage: {e}"}, 500)
            return self._json({"ok": True})

        if p == "/api/camera/flash":
            on = bool(body.get("on", False))
            _, err = phone_request("POST", p, {"on": on})
            return self._json({"ok": err is None, "error": err})

        if p == "/api/camera/switch":
            facing = body.get("facing", "back")
            data, err = phone_request("POST", p, {"facing": facing}, timeout=8)
            return self._json({
                "ok": err is None and bool((data or {}).get("ok")),
                "error": err or (data or {}).get("error"),
            })

        if p == "/api/camera/stop":
            _, err = phone_request("POST", p)
            return self._json({"ok": err is None, "error": err})

        if p == "/api/camera/copy":
            with _pending_photo_lock:
                has_pending = _pending_photo_at > 0
            if not has_pending or not PENDING_PHOTO_PATH.exists():
                return self._json({"ok": False, "error": "no photo to copy"}, 404)
            ok, err = copy_image_to_clipboard(str(PENDING_PHOTO_PATH))
            return self._json({"ok": ok, "error": err})

        if p == "/api/camera/save":
            filename = save_pending_photo()
            if not filename:
                return self._json({"ok": False, "error": "no photo to save"}, 404)
            return self._json({"ok": True, "filename": filename})

        if p == "/api/camera/folder/clear":
            count = clear_camera_folder()
            return self._json({"ok": True, "count": count})

        if p == "/api/screen/mirror/start":
            data, err = phone_request("POST", p, timeout=8)
            return self._json({
                "ok": err is None and bool((data or {}).get("ok")),
                "needsConsent": (data or {}).get("needsConsent", False),
                "error": err or (data or {}).get("error"),
            })

        if p == "/api/screen/mirror/stop":
            _, err = phone_request("POST", p)
            return self._json({"ok": err is None, "error": err})

        if p == "/api/screen/tap":
            x = body.get("x")
            y = body.get("y")
            if x is None or y is None:
                return self._json({"error": "x and y required"}, 400)
            _, err = phone_request("POST", p, {"x": x, "y": y})
            return self._json({"ok": err is None, "error": err})

        if p == "/api/screen/swipe":
            _, err = phone_request("POST", p, {
                "x1": body.get("x1"), "y1": body.get("y1"),
                "x2": body.get("x2"), "y2": body.get("y2"),
                "durationMs": body.get("durationMs", 200),
            })
            return self._json({"ok": err is None, "error": err})

        if p == "/api/ring":
            _, err = phone_request("POST", "/api/ring")
            return self._json({"ok": err is None, "error": err})

        if p == "/api/clipboard":
            text = body.get("text", "")
            _, err = phone_request("POST", "/api/clipboard", {"text": text})
            return self._json({"ok": err is None, "error": err})

        if p == "/api/notifications/dismiss":
            key = body.get("key", "")
            if not key:
                return self._json({"error": "key required"}, 400)
            _, err = phone_request("POST", "/api/notifications/dismiss", {"key": key})
            return self._json({"ok": err is None, "error": err})

        if p == "/api/notifications/action":
            key = body.get("key", "")
            action_index = body.get("actionIndex")
            if not key or action_index is None:
                return self._json({"error": "key and actionIndex required"}, 400)
            payload = {"key": key, "actionIndex": action_index}
            if "text" in body:
                payload["text"] = body.get("text", "")
            _, err = phone_request("POST", "/api/notifications/action", payload)
            return self._json({"ok": err is None, "error": err})

        if p == "/api/connect/set":
            host = body.get("host", "").strip()
            port = body.get("port")
            try:
                port = int(port) if port else PHONE_PORT_DEFAULT
            except (TypeError, ValueError):
                port = PHONE_PORT_DEFAULT
            if not host:
                return self._json({"ok": False, "message": "host required"}, 400)
            save_state({"phone_host": host, "phone_port": port, "last_connected": None, "method": "manual"})
            ok = is_phone_connected()
            return self._json({"ok": ok, "message": "connected" if ok else "saved, but not reachable yet"})

        if p == "/api/connect/reconnect":
            ok = is_phone_connected()
            return self._json({"ok": ok})

        if p == "/api/connect/forget":
            save_state({"phone_host": None, "phone_port": PHONE_PORT_DEFAULT, "last_connected": None, "method": None})
            return self._json({"ok": True})

        if p == "/api/filedrop/start":
            return self._json(start_filedrop())

        if p == "/api/codegate/pick_folder":
            return self._json({"folder": pick_folder("Choose the starter files folder")})

        if p.startswith("/api/codegate/"):
            ok, err = start_codegate()
            if not ok:
                return self._json({"error": err}, 502)
            # Exporting zips a whole workspace, and opening a room can boot the VM.
            result = codegate_request("POST", "/api/" + p[len("/api/codegate/"):], body, timeout=120)
            return self._json(result, 400 if "error" in result else 200)

        if p == "/api/filedrop/open_window":
            status = start_filedrop()
            if not status["running"]:
                return self._json({"ok": False, "error": status.get("error", "FileDrop did not start")}, 502)
            open_filedrop_window(status["url"])
            return self._json({"ok": True, "url": status["url"]})

        return self._json({"error": "not found"}, 404)



def main():
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=ble_supervisor, daemon=True).start()
    threading.Thread(target=camera_poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Dromac listening on http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
