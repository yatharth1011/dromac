#!/usr/bin/env python3
"""Dromac backend: talks to the Dromac companion app on the phone over plain HTTP
(Wi-Fi normally; over a USB cable via adb forward when Wi-Fi can't reach it) and serves the dashboard."""

import difflib
import hashlib
import ipaddress
import unicodedata
import json
import mimetypes
import os
import plistlib
import re
import shutil
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


# Set whenever a method above the ring scan (USB, beacon, gateway, mDNS)
# connects, so a scan in progress stops within one probe instead of grinding
# on, and can never overwrite that better link with its own find.
_better_link_found = threading.Event()


def link_found(state):
    save_state(state)
    threading.Thread(target=restore_phone_bt_if_owed, daemon=True).start()
    _last_status["connected"] = True
    _last_status["checked_at"] = time.time()
    if state.get("method") != "scan":
        _better_link_found.set()


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
    if ok:
        follow_phone_link(data)
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
    if os.environ.get("DROMAC_NO_WIFI") and ip != "127.0.0.1":  # for testing the fallbacks
        return False
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


# ---------- ring scan (last resort) ----------
# Probes every address on the Wi-Fi for Dromac, nearest to the Mac's own
# address first ("innermost ring"), widening out to the whole subnet and then
# larger blocks (up to a /16). Only runs on its own once USB, the beacon's
# addresses, the gateway and mDNS have all failed for a while, stops the
# moment the phone answers, and rests between full passes.
SCAN_AFTER_DISCONNECTED = 30   # seconds of nothing else working before scanning
SCAN_REST = 300                # seconds between full passes that found nothing
SCAN_WORKERS = 128
SCAN_MAX_PREFIX = 16
_disconnected_since = None
_scan_running = False
_last_full_scan = 0


def ring_order(own_ip):
    """Addresses around own_ip, nearest first: the Mac's own subnet, then each
    wider block's new addresses, again nearest first."""
    own = ipaddress.ip_address(own_ip)
    seen = set()
    blocks = []
    net = local_network()
    if net:
        blocks.append(net)
    prefix = (net.prefixlen if net else 24) - 1
    while prefix >= SCAN_MAX_PREFIX:
        blocks.append(ipaddress.ip_network(f"{own_ip}/{prefix}", strict=False))
        prefix -= 1
    for block in blocks:
        hosts = [h for h in block.hosts() if h != own and h not in seen]
        hosts.sort(key=lambda h: abs(int(h) - int(own)))
        seen.update(hosts)
        for h in hosts:
            yield str(h)


def ring_scan():
    """Returns the phone's address, or None. Gives up early if something else
    reconnects in the meantime."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    own = local_ip()
    if not own:
        return None
    print(f"[scan] ring scan starting around {own}", file=sys.stderr)
    _better_link_found.clear()
    order = ring_order(own)
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        while True:
            batch = [ip for _, ip in zip(range(SCAN_WORKERS * 4), order)]
            if not batch:
                break
            if _better_link_found.is_set():
                print("[scan] stopped: a higher-priority link connected", file=sys.stderr)
                return None
            futures = {pool.submit(probe_dromac, ip): ip for ip in batch}
            for f in as_completed(futures):
                if _better_link_found.is_set():
                    for other in futures:
                        other.cancel()
                    print("[scan] stopped: a higher-priority link connected", file=sys.stderr)
                    return None
                if f.result():
                    for other in futures:
                        other.cancel()
                    return futures[f]
    print("[scan] ring scan finished, phone not found", file=sys.stderr)
    return None


def maybe_ring_scan():
    """Kicks off a background ring scan when it's the only option left."""
    global _scan_running, _last_full_scan
    now = time.time()
    if _scan_running or _disconnected_since is None:
        return
    if now - _disconnected_since < SCAN_AFTER_DISCONNECTED or now - _last_full_scan < SCAN_REST:
        return
    _scan_running = True

    def run():
        global _scan_running, _last_full_scan
        try:
            ip = ring_scan()
            if ip and not _better_link_found.is_set():
                print(f"[scan] found the phone at {ip}", file=sys.stderr)
                link_found({"phone_host": ip, "phone_port": PHONE_PORT_DEFAULT, "last_connected": time.time(), "method": "scan"})
            elif not ip:
                _last_full_scan = time.time()
        finally:
            _scan_running = False

    threading.Thread(target=run, daemon=True).start()


# ---------- Bluetooth data link (3rd choice) ----------
# ble_link.py relays HTTP to the phone over a Bluetooth LE connection and
# listens on 127.0.0.1:BT_LINK_PORT, so the phone looks like one more address.
# Tried (in the background, it can take ~15 s to find and connect) only once
# USB and Wi-Fi have failed; the ring scan waits until this has failed too.
# Wi-Fi and USB still take over from it the moment they work (see
# handle_ble_hit and the watchdog). Needs the pairing key, which the phone
# only hands out over the cable.
BT_LINK_PORT = 18823
BT_LINK_PATH = ROOT / "ble_link.py"
BLE_KEY_FILE = ROOT / "ble_key"
BT_RETRY_INTERVAL = 20
_bt_running = False
_bt_last_try = 0
_bt_last_failed = False


def fetch_ble_key():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{USB_LOCAL_PORT}/api/ble_key", timeout=3) as resp:
            key = json.loads(resp.read().decode("utf-8")).get("key")
        if key and (not BLE_KEY_FILE.exists() or BLE_KEY_FILE.read_text().strip() != key):
            BLE_KEY_FILE.write_text(key)
            os.chmod(BLE_KEY_FILE, 0o600)
            print("[bt] saved the Bluetooth pairing key from the phone", file=sys.stderr)
    except Exception:
        pass  # older phone app without the link


def bluetooth_link_pending():
    """True while the Bluetooth link is still worth waiting for (so the ring scan holds off)."""
    return _bt_running or not _bt_last_failed


def maybe_bluetooth():
    global _bt_running, _bt_last_try
    now = time.time()
    if _bt_running or now - _bt_last_try < BT_RETRY_INTERVAL:
        return
    if not BLE_KEY_FILE.exists():
        _set_bt_failed(True)
        return
    _bt_running = True
    _bt_last_try = now

    def run():
        global _bt_running
        try:
            if probe_dromac("127.0.0.1", BT_LINK_PORT, timeout=30) and not _better_link_found.is_set():
                print("[bt] connected over the Bluetooth data link", file=sys.stderr)
                link_found({"phone_host": "127.0.0.1", "phone_port": BT_LINK_PORT, "last_connected": time.time(), "method": "bluetooth"})
                _set_bt_failed(False)
            else:
                _set_bt_failed(True)
        finally:
            _bt_running = False

    threading.Thread(target=run, daemon=True).start()


def _set_bt_failed(failed):
    global _bt_last_failed
    _bt_last_failed = failed


def bt_link_supervisor():
    """Keeps ble_link.py running (it holds no connection until asked)."""
    if os.environ.get("DROMAC_NO_BLE") or not BT_LINK_PATH.exists():
        return
    while True:
        try:
            proc = subprocess.Popen([sys.executable, str(BT_LINK_PATH)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            code = proc.wait()
            if code != 0:
                print(f"[bt] ble_link.py exited with code {code}, respawning in 5s", file=sys.stderr)
        except Exception as e:
            print(f"[bt] failed to spawn ble_link.py: {e}", file=sys.stderr)
        time.sleep(5)


# ---------- phone audio on the Mac speakers ----------
# The Mac becomes the phone's speaker, like plugging in headphones: scrcpy's
# "output" audio source (run over adb, so no screen-capture permission on the
# phone) takes the phone's whole audio output, silences the phone's own
# speaker, and plays it on whatever this Mac is outputting to (built-in
# speakers, AirPods, Bluetooth speakers, AirPlay, a Multi-Output Device).
# Same priority idea as the control link, but adb only runs over a cable or
# the network:
#   1. USB cable      -> raw PCM (lossless, lowest delay)
#   2. Wi-Fi (adb)    -> FLAC (lossless, about half the data)
# and it moves back up to the cable the moment it's plugged in. The Bluetooth
# link can't carry adb, so audio needs one of those two.
ADB_TCP_PORT = 5555
SCRCPY_CANDIDATES = ("/opt/homebrew/bin/scrcpy", "/usr/local/bin/scrcpy")
_audio_on = False
_audio_thread = None
_audio_status = {"on": False, "route": None, "error": None}

# Effects (dromac_fx.py): {"8d": preset name or None, "slowed": bool}. With
# none on, audio takes scrcpy's own direct path (lowest delay); with any on,
# scrcpy hands raw audio to dromac_fx.py, which feeds the dromac-audio player.
# Presets are switched live through FX_FILE, so changing them is gapless.
FX_FILE = ROOT / "fx.json"
FX_SCRIPT = ROOT / "dromac_fx.py"
AUDIO_PLAYER_SRC = ROOT / "dromac_audio.swift"
AUDIO_PLAYER_PLIST = ROOT / "dromac_audio.plist"
NOW_PLAYING_FILE = ROOT / "now_playing.json"
ARTWORK_FILE = ROOT / "now_playing_art.jpg"
AUDIO_PLAYER_BIN = ROOT / "dromac-audio"
HEAD_SRC = ROOT / "dromac_head.swift"          # head tracking (camera), run by dromac_fx.py
HEAD_PLIST = ROOT / "dromac_head.plist"
HEAD_BIN = ROOT / "dromac-head"
FX_POS_FILE = ROOT / "fx_pos.json"
FX_8D_PRESETS = ("orbit", "helix", "loop", "infinity", "saddle", "knot", "lissajous", "breathe", "gyro", "swing", "wander", "lorenz", "stage", "still", "auto")
FX_MODES = ("8d", "dsp", "ai")  # whole song moves / live instrument split / AI instrument split
# 8d_on / slowed are the two on-off switches; "8d" remembers the chosen path
# even while 8D is off, so switching it back on resumes the same one.
_fx = {"8d_on": False, "8d": "orbit", "speed": 1.0, "slowed": False, "mode": "8d", "vocals": True,
       "accel": 50}  # ai split: % of the GPU to use -- more GPU, less delay (80 = "ultramuse")
HEAR_PARTS = ("vocals", "drums", "bass", "other")
AUDIO_PREFS_FILE = ROOT / "audio_prefs.json"
try:
    _fx.update(json.loads(AUDIO_PREFS_FILE.read_text()).get("fx", {}))
except Exception:
    pass


def save_audio_prefs():
    try:
        AUDIO_PREFS_FILE.write_text(json.dumps({"on": _audio_on, "fx": _fx}))
    except Exception:
        pass


def restore_audio():
    """Audio that was on before a restart comes back on. With headphones only
    set, the phone paused itself in the meantime; if that was just now, play
    again once the Mac has the audio back."""
    global _paused_for_lock
    try:
        if not json.loads(AUDIO_PREFS_FILE.read_text()).get("on"):
            return
    except Exception:
        return
    data, _ = phone_request("GET", "/api/state", timeout=3)
    if (data or {}).get("audioLink") is False:
        return  # switched off on the phone: stay off
    ago = (data or {}).get("spillPausedAgoMs", -1)
    if 0 <= ago < 60_000:
        _paused_for_lock = True
    set_audio(True)


def fx_active():
    return bool(_fx.get("8d_on") or _fx.get("slowed") or _fx.get("mode") in ("dsp", "ai")
                or (_fx.get("rain") or {}).get("on"))


RAIN_PRESETS = ("storm cycle", "drizzle", "steady", "downpour", "thunderstorm", "distant storm",
                "tin roof", "window", "forest")


# "Headphones only": the phone's audio may play on these headphones and nowhere
# else. On the Mac, the player goes silent instead of following macOS to its
# own speakers (dromac_audio.swift); on the phone, SpillGuard pauses the music
# rather than let it out of the phone's speaker. Persisted across restarts.
LOCK_FILE = ROOT / "headphones_lock.json"
BUILTIN_OUTPUTS = ("MacBook Pro Speakers", "MacBook Air Speakers", "Mac mini Speakers", "iMac Speakers",
                   "Built-in Output", "Mac Studio Speakers")
try:
    _lock = json.loads(LOCK_FILE.read_text())
except Exception:
    _lock = {"on": False, "device": None}
_paused_for_lock = False


def save_lock():
    LOCK_FILE.write_text(json.dumps(_lock))


def mac_output_name():
    try:
        return subprocess.run([ensure_audio_player(), "--current-output"],
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def use_own_player():
    """Always Dromac's own path (effects process + player), even with every
    effect off: switching to scrcpy's own player and back on each toggle meant
    tearing the whole audio chain down -- the sound cut out every time 8D was
    switched. One path: toggles are instant, and the cushion (seamless cable
    switches), the headphone buttons and Now Playing work in every mode."""
    return True


def set_lock(on):
    if on:
        device = mac_output_name()
        if not device or device in BUILTIN_OUTPUTS:
            return "connect your headphones to the Mac first, then turn this on"
        _lock.update(on=True, device=device)
    else:
        _lock.update(on=False)
    save_lock()
    phone_request("POST", "/api/audio/guard", {"on": bool(_lock["on"]), "device": _lock.get("device") or ""}, timeout=3)
    return None


def fx_position_any():
    """The effects process's last report, however old."""
    try:
        return json.loads(FX_POS_FILE.read_text())
    except Exception:
        return None


def fx_position():
    """The effects process's latest report of where the sound is (fresh only)."""
    try:
        data = json.loads(FX_POS_FILE.read_text())
        return data if time.time() - data.get("t", 0) < 2 else None
    except Exception:
        return None


def write_fx():
    tmp = FX_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_fx))
    tmp.replace(FX_FILE)


def ensure_head_tracker():
    """Builds dromac-head if it's missing or older than its source. Never fatal:
    without it, head tracking just stays off."""
    try:
        if HEAD_BIN.exists() and HEAD_BIN.stat().st_mtime >= HEAD_SRC.stat().st_mtime:
            return
        cmd = ["swiftc", "-O", str(HEAD_SRC), "-o", str(HEAD_BIN),
               "-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist", "-Xlinker", str(HEAD_PLIST)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print("[head] couldn't build the head tracker:", (result.stderr.strip().splitlines() or ["?"])[-1], flush=True)
    except Exception as e:
        print("[head] couldn't build the head tracker:", e, flush=True)


def ensure_audio_player():
    ensure_head_tracker()
    if AUDIO_PLAYER_BIN.exists() and AUDIO_PLAYER_BIN.stat().st_mtime >= AUDIO_PLAYER_SRC.stat().st_mtime:
        return str(AUDIO_PLAYER_BIN)
    cmd = ["swiftc", "-O", str(AUDIO_PLAYER_SRC), "-o", str(AUDIO_PLAYER_BIN)]
    if AUDIO_PLAYER_PLIST.exists():  # a bundle id, so macOS will make it the Now Playing app
        cmd += ["-Xlinker", "-sectcreate", "-Xlinker", "__TEXT", "-Xlinker", "__info_plist", "-Xlinker", str(AUDIO_PLAYER_PLIST)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError("couldn't build the audio player: " + (result.stderr.strip().splitlines() or ["swiftc failed"])[-1])
    return str(AUDIO_PLAYER_BIN)


def find_scrcpy():
    found = shutil.which("scrcpy")
    if found:
        return found
    return next((p for p in SCRCPY_CANDIDATES if os.access(p, os.X_OK)), None)


def adb_devices():
    """[(serial, state)] from `adb devices`."""
    adb = find_adb()
    if not adb:
        return []
    try:
        out = subprocess.run([adb, "devices"], capture_output=True, text=True, timeout=4).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            rows.append((parts[0], parts[1]))
    return rows


def usb_serial():
    """The phone over the cable (network adb entries are "ip:port")."""
    for serial, st in adb_devices():
        if st == "device" and ":" not in serial and "._adb-tls" not in serial:
            return serial
    return None


WIFI_IP_FILE = ROOT / "phone_wifi_ip"


def remember_wifi_ip(data):
    """The phone reports its addresses (the Wi-Fi it's joined, and its own
    hotspot); remembering them is what lets the Mac move to Wi-Fi the moment
    the cable goes -- without the Bluetooth beacon, which is off while the Mac
    has the headphones."""
    ips = [ip for ip in ((data or {}).get("ips") or [(data or {}).get("wifiIp")]) if ip and not str(ip).startswith("127.")]
    if ips:
        try:
            text = "\n".join(ips)
            if not WIFI_IP_FILE.exists() or WIFI_IP_FILE.read_text().strip() != text:
                WIFI_IP_FILE.write_text(text)
        except Exception:
            pass


def known_wifi_ips():
    try:
        return [l.strip() for l in WIFI_IP_FILE.read_text().splitlines() if l.strip()]
    except Exception:
        return []


def known_wifi_ip():
    """The best remembered address from here: on the phone's hotspot, the
    phone is the Mac's gateway; otherwise the Wi-Fi address it reported."""
    ips = known_wifi_ips()
    gw = default_gateway()
    if gw and gw in ips:
        return gw
    return ips[0] if ips else None


def reachable(ip, port, timeout=0.7):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except Exception:
        return False


def phone_wifi_ips():
    """Every address the phone might be reached on over the network, best
    first: the Mac's gateway if that's the phone (Mac on its hotspot), the
    addresses it reported, the beacon's, and where the control link found it."""
    gw = default_gateway()
    with _ble_lock:
        ips = known_wifi_ips() + list(_ble_last_ips)
    state = load_state()
    if state.get("method") in ("ble", "gateway", "mdns", "scan", "manual", "wifi") and state.get("phone_host"):
        ips.append(state["phone_host"])
    ordered = ([gw] if gw and gw in ips else []) + ips
    seen, out = set(), []
    for ip in ordered:
        if ip and not ip.startswith("127.") and ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def phone_wifi_ip():
    ips = phone_wifi_ips()
    return ips[0] if ips else None


def enable_adb_over_wifi(serial):
    """While on the cable, make the phone's adb also listen on the network so
    audio can carry on over Wi-Fi after unplugging. adb over the network still
    needs this Mac's key, which the phone already trusts from the cable. Lasts
    until the phone reboots; done only when audio is on."""
    adb = find_adb()
    try:
        port = subprocess.run([adb, "-s", serial, "shell", "getprop", "service.adb.tcp.port"],
                              capture_output=True, text=True, timeout=4).stdout.strip()
        if port != str(ADB_TCP_PORT):
            # Restarts the phone's adb, so the cable drops for a second or two;
            # the control link and BlazingNet reconnect on their own.
            subprocess.run([adb, "-s", serial, "tcpip", str(ADB_TCP_PORT)], capture_output=True, timeout=8)
            time.sleep(3)
    except Exception:
        pass


def wifi_serial():
    """Connect adb to the phone over the network (its Wi-Fi address, or its
    hotspot address when the Mac is on the hotspot); returns the serial."""
    adb = find_adb()
    if not adb:
        return None
    devices = adb_devices()
    candidates = phone_wifi_ips()
    for ip in candidates:                       # already connected on one of them?
        if (f"{ip}:{ADB_TCP_PORT}", "device") in devices:
            return f"{ip}:{ADB_TCP_PORT}"
    for ip in candidates:
        if reachable(ip, ADB_TCP_PORT):         # don't wait on an address that can't work from here
            serial = _adb_connect(adb, ip)
            if serial:
                return serial
    return None


def _adb_connect(adb, ip):
    serial = f"{ip}:{ADB_TCP_PORT}"
    for attempt in range(2):
        try:
            subprocess.run([adb, "connect", serial], capture_output=True, text=True, timeout=6)
        except Exception:
            return None
        if (serial, "device") in adb_devices():
            return serial
        if attempt == 0 and not _restart_adb_server_if_blocked(ip):
            break
    return None


_last_adb_restart = 0


def _restart_adb_server_if_blocked(ip):
    """The adb server is a background daemon that keeps the network rights of
    whichever app happened to start it. Started from somewhere without macOS's
    Local Network permission (a launchd job, say), every `adb connect` fails
    with "No route to host" even though the phone is right there. If this
    process can reach the phone's adb port itself, restart the server from
    here, where the permission is in place. At most once a minute."""
    global _last_adb_restart
    if time.time() - _last_adb_restart < 60:
        return False
    try:
        with socket.create_connection((ip, ADB_TCP_PORT), timeout=2):
            pass
    except Exception:
        return False  # genuinely unreachable; restarting wouldn't help
    _last_adb_restart = time.time()
    adb = find_adb()
    print("[audio] adb server can't reach the network; restarting it from Dromac", file=sys.stderr)
    try:
        subprocess.run([adb, "kill-server"], capture_output=True, timeout=6)
        subprocess.run([adb, "start-server"], capture_output=True, timeout=10)
    except Exception:
        return False
    return True


def drop_wifi_adb():
    adb = find_adb()
    for serial, _ in adb_devices():
        if serial.endswith(f":{ADB_TCP_PORT}"):
            try:
                subprocess.run([adb, "disconnect", serial], capture_output=True, timeout=4)
            except Exception:
                pass


# The long-lived half of the audio path: the pipe scrcpy writes into, the
# effects/AI process reading it, and the player. It outlives any one capture,
# so when the route changes (cable out -> Wi-Fi) only scrcpy restarts: the AI
# keeps its model and buffer, the player keeps playing.
_pipe = None
_capture_lost_at = None   # when the last capture died (to time route changes)
# The player's own queue. Wi-Fi jitter is now absorbed by the effects' cushion
# upstream, so this only needs to keep the device fed: small, so a click
# (vocals, a preset) is heard quickly.
PIPE_BUFFER_MS = 100


def ensure_pipeline():
    global _pipe
    locked = _lock["device"] if _lock.get("on") else ""
    if _pipe and _pipe["locked"] == locked and all(p.poll() is None for p in (_pipe["fx"], _pipe["player"])):
        return _pipe
    stop_pipeline()
    player_bin = ensure_audio_player()
    try:
        FX_POS_FILE.unlink()   # a new effects process counts captures from 0 again
    except FileNotFoundError:
        pass
    fifo = ROOT / "phone_audio.wav"
    try:
        fifo.unlink()
    except FileNotFoundError:
        pass
    os.mkfifo(fifo)
    write_fx()
    # effects -> player over a socket pair with small buffers (~0.08 s): a pipe
    # holds up to 64 KB (~0.33 s), all of it delay between a change and hearing it
    fx_end, player_end = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    fx_end.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
    player_end.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8192)
    player = subprocess.Popen([player_bin, str(PIPE_BUFFER_MS // 20), locked], stdin=player_end.fileno())
    effects = subprocess.Popen([sys.executable, str(FX_SCRIPT), str(fifo), str(FX_FILE)],
                               stdout=fx_end.fileno(), stderr=subprocess.PIPE)
    fx_end.close()        # each process owns its end now
    player_end.close()
    # Start capturing only once the effects are built (see dromac_fx.py), or the
    # audio queued meanwhile would stay as delay.
    import select
    ready, _, _ = select.select([effects.stderr], [], [], 20)
    if not ready or not effects.stderr.readline().startswith(b"ready"):
        for p in (effects, player):
            p.kill()
        return None
    _pipe = {"fifo": fifo, "fx": effects, "player": player, "locked": locked}
    return _pipe


def stop_pipeline():
    global _pipe
    if not _pipe:
        return
    for p in (_pipe["fx"], _pipe["player"]):
        if p.poll() is None:
            p.terminate()
    for p in (_pipe["fx"], _pipe["player"]):
        try:
            p.wait(timeout=3)
        except Exception:
            p.kill()
    try:
        _pipe["fifo"].unlink()
    except FileNotFoundError:
        pass
    _pipe = None


def _play(scrcpy, serial, route):
    """Runs one capture until scrcpy exits, audio is switched off, the cable
    comes back (when on Wi-Fi), or effects/lock are switched (which needs the
    other path)."""
    with_fx = use_own_player()
    # Dromac's own player (effects / headphones only) takes the raw stream on
    # any link -- scrcpy can only hand raw audio to a WAV pipe. Lossless either
    # way; raw is ~1.5 Mbps, easy for Wi-Fi. scrcpy's own player gets FLAC on
    # Wi-Fi (half the data, still lossless).
    codec = "raw" if (route == "usb" or with_fx) else "flac"
    # How far behind live the Mac plays. Campus Wi-Fi's delay swings by 100 ms+
    # from one moment to the next; with a buffer smaller than that the audio
    # runs dry and scrcpy skips samples to catch up -- the clicks. 300 ms rides
    # that out (measured: no underflows); the cable is steady, so it can stay close.
    buffer_ms = 80 if route == "usb" else 300
    started_at = time.time()
    base = [scrcpy, "-s", serial, "--no-video", "--no-control", "--no-window",
            "--audio-source=output", f"--audio-codec={codec}"]
    if route != "usb":
        # over Wi-Fi debugging, scrcpy's "adb reverse" tunnel is unreliable (it was
        # torn down within 0.1 s, so the first capture after unplugging failed);
        # a forward tunnel works first time
        base.append("--force-adb-forward")
    pipe = None
    # a capture left running on the phone by the previous route (the cable was
    # pulled before it could stop) would block this one: clear it first
    try:
        subprocess.run([find_adb(), "-s", serial, "shell", "pkill", "-f", "com.genymobile.scrcpy.Server"],
                       capture_output=True, timeout=3)
    except Exception:
        pass
    if with_fx:
        pipe = ensure_pipeline()
        if not pipe:
            _audio_status.update(error="audio effects failed to start")
            time.sleep(3)
            return
        proc = subprocess.Popen(base + ["--no-audio-playback", f"--record={pipe['fifo']}", "--record-format=wav"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    else:
        stop_pipeline()
        proc = subprocess.Popen(base + [f"--audio-buffer={buffer_ms}", "--audio-output-buffer=10"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    _audio_status.update(route=route, codec=codec, error=None)
    print(f"[audio] playing over {route} ({codec}{', effects' if with_fx else ''})", file=sys.stderr)

    seq0 = (fx_position_any() or {}).get("capture_seq", 0)

    def resume_after_route_change():
        # The route just changed (cable out -> Wi-Fi, or back): the phone paused
        # itself so nothing came out of its speaker in between. Once this capture
        # is up, play on -- the cushion covers the moment it took. Keeps trying
        # for a few seconds, and goes straight to the phone's Wi-Fi address if
        # the control link still points at the cable that's just been pulled.
        # wait until this capture's audio is actually arriving -- the phone's
        # audio goes to it from then on; resuming before that sent the music to
        # the phone's speaker for a moment (and the phone rightly paused again)
        if with_fx:
            until = time.time() + 8
            while time.time() < until and (fx_position_any() or {}).get("capture_seq", 0) <= seq0:
                if proc.poll() is not None or not _audio_on:
                    return
                time.sleep(0.1)
            time.sleep(0.2)
        else:
            time.sleep(1.5)
        deadline = time.time() + 6
        while time.time() < deadline and proc.poll() is None and _audio_on:
            data, via = phone_request("GET", "/api/state", timeout=1.5), None
            data = data[0]
            if data is None and route != "usb" and known_wifi_ip():
                via = f"http://{known_wifi_ip()}:{PHONE_PORT_DEFAULT}"
                try:
                    with urllib.request.urlopen(via + "/api/state", timeout=1.5) as r:
                        data = json.loads(r.read().decode("utf-8"))
                except Exception:
                    data = None
            if data is None:
                time.sleep(0.4)
                continue
            ago = data.get("spillPausedAgoMs", -1)
            if 0 <= ago < 15000:
                if via:
                    req = urllib.request.Request(via + "/api/media/resume_paused", data=b"{}", method="POST",
                                                 headers={"Content-Type": "application/json"})
                    try:
                        urllib.request.urlopen(req, timeout=3).read()
                    except Exception:
                        time.sleep(0.4)
                        continue
                    reply = "sent over Wi-Fi"
                else:
                    reply = phone_request("POST", "/api/media/resume_paused", timeout=3)
                took = f", {time.time() - _capture_lost_at:.1f} s after the old capture died" if _capture_lost_at else ""
                print(f"[audio] resume after the route change (phone paused {ago} ms ago{took}): {reply}", file=sys.stderr)
            return
    threading.Thread(target=resume_after_route_change, daemon=True).start()
    last_phone_check = 0
    last_standby = 0
    player_exit = None
    try:
        while _audio_on and proc.poll() is None:
            if pipe and pipe["player"].poll() is not None:
                player_exit = pipe["player"].poll()
                break
            time.sleep(0.5)
            if time.time() - last_phone_check > 2:
                last_phone_check = time.time()
                data, _ = phone_request("GET", "/api/state", timeout=2)
                remember_wifi_ip(data)
                follow_phone_link(data)
                update_now_playing(data)
                if route == "usb" and time.time() - last_standby > 30:
                    last_standby = time.time()
                    # keep a Wi-Fi debugging connection ready alongside the cable,
                    # so audio can move straight onto it if the cable goes
                    threading.Thread(target=wifi_serial, daemon=True).start()
                if _lock.get("on") and data and _lock.get("device") in (data.get("phoneHeadphones") or []):
                    phone_bt(False)  # it would play its silence to them instead of the Mac
            if use_own_player() != with_fx or (pipe and pipe["locked"] != (_lock["device"] if _lock.get("on") else "")):
                print("[audio] effects/lock switched, changing path", file=sys.stderr)
                break
            if route != "usb" and usb_serial():
                print("[audio] cable plugged in, moving audio onto it", file=sys.stderr)
                break
        if player_exit == 3:
            headphones_left_mac()
        elif player_exit == 4:
            # the Mac is going to sleep: give the phone its headphones back now
            print("[audio] Mac going to sleep; handing the headphones to the phone", file=sys.stderr)
            give_headphones_back_to_phone()
    finally:
        crashed = proc.poll() is not None and _audio_on and player_exit is None
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        if player_exit is not None or not _audio_on or use_own_player() != with_fx:
            stop_pipeline()   # the player's gone, audio's off, or the other path is needed
        if crashed:
            globals()["_capture_lost_at"] = time.time()
            err = (proc.stderr.read() or "").strip().splitlines()
            print(f"[audio] scrcpy stopped ({route}): " + (" | ".join(err[-4:]) if err else str(proc.returncode)), file=sys.stderr)
            if time.time() - started_at < 5:
                time.sleep(1)  # failing straight away: don't flap the audio on and off


# ---- who gets the headphones: the Mac first ----
# With the headphones on both (multipoint), they listen to whichever device
# streams -- and the phone streams silence while Dromac has its audio, so you'd
# hear nothing. So while the Mac plays them, the phone's Bluetooth is off; it's
# switched back on whenever the Mac lets the headphones go, so the phone can
# reconnect to them and carry on. Never while the control link itself is the
# Bluetooth one.

_np_art_key = None


def update_now_playing(data):
    """Song info for the Mac's Now Playing (read by dromac-audio), and the
    phone's pauses/plays for the instant hold -- from one phone state fetch."""
    global _np_art_key
    np = (data or {}).get("now_playing")
    if not np:
        return
    if "playing" in np:
        note_phone_playing(bool(np["playing"]))
    note_track(np)
    pos = fx_position() or {}
    key = f"{np.get('title')}::{np.get('artist')}"
    if np.get("hasArt") and key != _np_art_key:
        raw, _ = phone_request("GET", "/api/artwork", timeout=4, binary=True)
        if raw:
            ARTWORK_FILE.write_bytes(raw)
            _np_art_key = key
    info = {
        "title": np.get("title"), "artist": np.get("artist"), "playing": bool(np.get("playing")),
        "duration_ms": float(np.get("duration") or 0),
        "position_ms": max(0.0, float(np.get("position") or 0) - float(pos.get("delay_ms") or 0)),
        "artwork": str(ARTWORK_FILE) if _np_art_key == key else None,
    }
    try:
        tmp = NOW_PLAYING_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(info))
        tmp.replace(NOW_PLAYING_FILE)
    except Exception:
        pass


def adb_any_serial():
    serial = usb_serial()
    if serial:
        return serial
    return next((sr for sr, st in adb_devices() if st == "device" and sr.endswith(f":{ADB_TCP_PORT}")), None)


def phone_bt(on):
    if not on and load_state().get("method") == "bluetooth":
        return  # that would cut the very link we're talking over
    serial, adb = adb_any_serial(), find_adb()
    if not serial or not adb:
        return
    try:
        subprocess.run([adb, "-s", serial, "shell", "cmd", "bluetooth_manager", "enable" if on else "disable"],
                       capture_output=True, timeout=8)
    except Exception:
        return
    _lock["phone_bt_off"] = not on
    save_lock()
    # tell the phone too: if the Mac then goes quiet with its Bluetooth still
    # off, the phone offers to turn it back on (see SpillGuard.btOffByMac)
    phone_request("POST", "/api/audio/bt_by_mac", {"off": not on}, timeout=3)
    print(f"[audio] phone Bluetooth {'on: it can take the headphones' if on else 'off: the headphones are the Mac’s'}", file=sys.stderr)


def phone_has_locked():
    data, _ = phone_request("GET", "/api/state", timeout=2)
    return bool(data and _lock.get("device") in (data.get("phoneHeadphones") or []))


def mac_connect_locked():
    """Ask this Mac to connect the headphones (they must be paired with it)."""
    try:
        out = subprocess.run([ensure_audio_player(), "--connect", _lock.get("device", "")],
                             capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return False
    return out == "connected"


def give_headphones_back_to_phone():
    if _lock.get("phone_bt_off"):
        phone_bt(True)


def restore_phone_bt_if_owed():
    """Bluetooth that Dromac switched off is switched back on as soon as the
    Mac can reach the phone again -- if it couldn't at the time (no cable, no
    Wi-Fi debugging), the phone stayed without Bluetooth: no beacon, so on a
    new network the Mac couldn't even find it."""
    if _lock.get("phone_bt_off") and not (_audio_on and _lock.get("on")):
        phone_bt(True)


def headphones_left_mac():
    """The locked headphones dropped off the Mac: the player has already gone
    silent. Hand the audio back to the phone -- which plays it on the same
    headphones if they're connected to it too (multipoint) -- and if they
    aren't, pause first so the phone's speaker never gets a note of it."""
    global _paused_for_lock
    print(f"[audio] {_lock.get('device')} left the Mac; handing audio back to the phone", file=sys.stderr)
    data, _ = phone_request("GET", "/api/state", timeout=2)
    phone_has_headphones = bool(data and data.get("phoneHeadphones"))
    if not phone_has_headphones:
        _, err = phone_request("POST", "/api/media/pause", timeout=2)
        _paused_for_lock = err is None
    # let the phone reconnect to them; its guard plays on as soon as they're
    # there (or, if they never show up, allows the speaker -- on your press)
    give_headphones_back_to_phone()
    _audio_status.update(route=None, error=None,
                         waiting=f"{_lock.get('device')} isn't connected to the Mac"
                         + ("" if phone_has_headphones else " (music paused)"))


def wait_for_locked_headphones():
    """With the lock on, only play once the Mac's output is those headphones.
    If the phone has them instead, pull them over to the Mac (the Mac comes
    first); if neither has them, keep trying to connect them to the Mac."""
    global _paused_for_lock
    device = _lock.get("device")
    next_try = 0
    while _audio_on and _lock.get("on"):
        if mac_output_name() == device:
            if _audio_status.get("waiting"):
                print(f"[audio] {device} is back on the Mac", file=sys.stderr)
            _audio_status["waiting"] = None
            return True
        if time.time() >= next_try:
            if phone_has_locked():
                print(f"[audio] {device} is on the phone; pulling it over to the Mac", file=sys.stderr)
                _audio_status.update(route=None, waiting=f"moving {device} from the phone to the Mac…")
                _, err = phone_request("POST", "/api/media/pause", timeout=2)
                _paused_for_lock = _paused_for_lock or err is None
                phone_bt(False)
                time.sleep(2)  # let the headphones notice the phone is gone
                mac_connect_locked()
                for _ in range(10):
                    if mac_output_name() == device:
                        break
                    time.sleep(1)
                if mac_output_name() != device:
                    print(f"[audio] couldn't move {device} to the Mac; giving it back to the phone", file=sys.stderr)
                    give_headphones_back_to_phone()
                    if _paused_for_lock:
                        time.sleep(4)
                        phone_request("POST", "/api/media/resume_paused", timeout=3)
                        _paused_for_lock = False
                    next_try = time.time() + 60
                continue
            # on neither: maybe they just switched on or came back in range
            mac_connect_locked()
            next_try = time.time() + 15
        _audio_status.update(route=None, waiting=f"{device} isn't connected to the Mac"
                             + (" (music paused)" if _paused_for_lock else ""))
        time.sleep(1)
    return _audio_on


def _resume_if_we_paused():
    global _paused_for_lock
    if _paused_for_lock:
        time.sleep(1.5)  # let the capture take hold first, so play starts on the Mac
        # plays exactly what was paused (play/pause would hit whichever app is first)
        phone_request("POST", "/api/media/resume_paused", timeout=3)
        _paused_for_lock = False


def audio_loop():
    scrcpy = find_scrcpy()
    if not scrcpy:
        _audio_status.update(error="phone audio needs scrcpy (brew install scrcpy)")
        return
    while _audio_on:
        if _lock.get("on") and not wait_for_locked_headphones():
            break
        if _paused_for_lock:
            threading.Thread(target=_resume_if_we_paused, daemon=True).start()
        serial = usb_serial()
        if serial:
            drop_wifi_adb()
            enable_adb_over_wifi(serial)
            serial = usb_serial()  # it re-appears once adb has restarted
            if serial:
                _play(scrcpy, serial, "usb")
                continue
        serial = wifi_serial()
        if serial:
            _play(scrcpy, serial, "wi-fi")
            continue
        _audio_status.update(route=None, error="waiting for the phone over USB or Wi-Fi (audio can't go over Bluetooth)")
        time.sleep(3)
    _audio_status.update(route=None)


def stop_leftover_audio():
    """A previous server that crashed or was killed leaves its scrcpy capture
    running; a second capture next to it splits the phone's audio between the
    two and both sound broken. Matches only Dromac's own audio command line."""
    for pattern in (str(ROOT / "dromac_fx.py"), str(ROOT / "dromac-audio"),
                    "^[^ ]*/scrcpy -s [^ ]+ --no-video --no-control --no-window --audio-source=output"):
        subprocess.run(["pkill", "-f", pattern], capture_output=True)


_hold_set_at = 0
_phone_playing = None


def set_hold(hold):
    """ai split plays ~9.5 s behind; pausing must still be instant (see dromac_fx)."""
    global _hold_set_at
    if bool(_fx.get("hold")) != bool(hold):
        _fx["hold"] = bool(hold)
        _hold_set_at = time.time()
        write_fx()


def note_phone_playing(playing):
    """Follow pauses/plays made on the phone itself (seen in the state poll)."""
    global _phone_playing
    _phone_playing = playing
    if time.time() - _hold_set_at > 3:   # a press here wins over a not-yet-updated poll
        set_hold(not playing)


def signal_skip(offset_ms):
    """ai split plays a few seconds behind: on a skip, stop the old song now and
    separate the new one from its start (see AISplit.skip_to)."""
    if not (_audio_on and _fx.get("8d_on") and _fx.get("mode") == "ai"):
        return
    prev = _fx.get("skip") or {}
    _fx["skip"] = {"n": prev.get("n", 0) + 1, "offset_ms": int(offset_ms)}
    write_fx()


_last_track = {"key": None, "pos": 0, "dur": 0}


def note_track(np_):
    """A song change that isn't the old one finishing is a skip made on the
    phone: its new position says how long ago the new song began."""
    key = f"{np_.get('title')}::{np_.get('artist')}"
    last = dict(_last_track)
    if key != _fx.get("song"):
        _fx["song"] = key          # auto's learning starts over for each song
        write_fx()
    _last_track.update(key=key, pos=float(np_.get("position") or 0), dur=float(np_.get("duration") or 0))
    if last["key"] and key != last["key"] and last["dur"] and last["pos"] < last["dur"] - 12000:
        signal_skip(-min(4000, float(np_.get("position") or 0)))


def set_fx(body):
    """Live: within the effects path a preset change is just a file write."""
    if body.get("8d") in FX_8D_PRESETS:
        _fx["8d"] = body["8d"]
        _fx["8d_on"] = True  # picking a path turns 8D on
    if "8d_on" in body:
        _fx["8d_on"] = bool(body["8d_on"])
    if body.get("mode") in FX_MODES:
        _fx["mode"] = body["mode"]   # the split: independent of 8D now
    if "speed" in body:
        try:
            _fx["speed"] = round(min(4.0, max(0.25, float(body["speed"]))), 2)
        except (TypeError, ValueError):
            pass
    if "accel" in body:
        try:
            _fx["accel"] = int(min(80, max(15, float(body["accel"]))))
        except (TypeError, ValueError):
            pass
    if "room" in body:
        _fx["room"] = bool(body["room"])
    if "head" in body:
        _fx["head"] = bool(body["head"])      # head tracking: the camera is on only while this is
    if body.get("recenter"):
        _fx["head_center"] = int(_fx.get("head_center", 0)) + 1
    for key in ("kit_tight", "kit_boost", "self_mix"):
        if key in body:
            _fx[key] = bool(body[key])
    # ambience: rain and thunder, made live by dromac_fx.py (Weather)
    if isinstance(body.get("rain"), dict):
        rain = dict(_fx.get("rain") or {"on": False, "preset": "storm cycle", "volume": 35})
        r = body["rain"]
        if "on" in r:
            rain["on"] = bool(r["on"])
            rain["paused"] = False
        if r.get("preset") in RAIN_PRESETS:
            rain["preset"] = r["preset"]
        if "volume" in r:
            try:
                rain["volume"] = int(min(100, max(0, float(r["volume"]))))
            except (TypeError, ValueError):
                pass
        if "thunder" in r:
            try:
                rain["thunder"] = int(min(12, max(-12, float(r["thunder"]))))   # dB, against the rain
            except (TypeError, ValueError):
                pass
        _fx["rain"] = rain
    if "kit" in body:
        _fx["kit"] = bool(body["kit"])
        if _fx["kit"]:
            _fx["mode"] = "ai"       # the drum kit split is AI only
    if "bass" in body:
        try:
            _fx["bass"] = int(min(12, max(0, float(body["bass"]))))
        except (TypeError, ValueError):
            pass
    # which instruments you hear (split modes): vocals, drums, bass, other --
    # any mix, e.g. just the bass. "vocals" alone is the older switch.
    hear = dict(_fx.get("hear") or {})
    if "vocals" in body:
        hear["vocals"] = bool(body["vocals"])
    if isinstance(body.get("hear"), dict):
        for part in HEAR_PARTS:
            if part in body["hear"]:
                hear[part] = bool(body["hear"][part])
    if "vocals" in body or "hear" in body:
        _fx["hear"] = {part: hear.get(part, True) for part in HEAR_PARTS}
        _fx["vocals"] = _fx["hear"]["vocals"]
        if not all(_fx["hear"].values()) and _fx.get("mode") not in ("dsp", "ai"):
            _fx["mode"] = "ai"       # leaving anything out needs the song split: AI quality
    if "slowed" in body:
        _fx["slowed"] = bool(body["slowed"])
    write_fx()
    save_audio_prefs()
    if fx_active() and not _audio_on:
        set_link(True)  # picking an effect turns the audio link on


# ---- the audio link: one switch, on the Mac and on the phone ----
# Off = back to completely normal: no capture, no effects, nothing paused, the
# phone's Bluetooth its own again. The phone keeps the setting (AudioLink.kt);
# either side can flip it and the other follows.
_link_seen = None


def set_link(on, from_phone=False):
    global _link_seen
    _link_seen = on
    if not from_phone:
        phone_request("POST", "/api/audio/link", {"on": on}, timeout=3)
    if on:
        if _lock.get("on"):
            phone_request("POST", "/api/audio/guard", {"on": True, "device": _lock.get("device") or ""}, timeout=3)
        set_audio(True)
    else:
        set_audio(False)  # stops the capture and hands Bluetooth back (see set_audio)
    print(f"[audio] audio link {'on' if on else 'off'}{' (from the phone)' if from_phone else ''}", file=sys.stderr)


def follow_phone_link(data):
    """The phone's audio link switch, seen in its state: follow changes to it."""
    global _link_seen
    v = (data or {}).get("audioLink")
    if v is None:
        return
    if _link_seen is None:
        _link_seen = v
        if not v and _audio_on:
            set_link(False, from_phone=True)
    elif v != _link_seen:
        set_link(v, from_phone=True)


def set_audio(on):
    global _audio_on, _audio_thread
    _audio_on = on
    _audio_status["on"] = on
    save_audio_prefs()
    if on:
        _audio_status.update(error=None)
        if not (_audio_thread and _audio_thread.is_alive()):
            _audio_thread = threading.Thread(target=audio_loop, daemon=True)
            _audio_thread.start()
    else:
        _audio_status.update(route=None)
        give_headphones_back_to_phone()  # audio off: the phone may use them again
        threading.Thread(target=lambda: (time.sleep(1.5), None if _audio_on else stop_pipeline()), daemon=True).start()


# ---------- USB fallback ----------
# When Wi-Fi can't reach the phone at all (campus networks that block devices
# from talking to each other, a beacon address that isn't routable from here),
# a USB cable still works: `adb forward` maps a Mac port onto the phone's
# server, so it answers on 127.0.0.1. Needs USB debugging on the phone, which
# BlazingNet uses too. Preferred over Wi-Fi whenever the cable is in.
# Order: USB cable -> Wi-Fi at the beacon's addresses -> (gateway / mDNS)
# -> Bluetooth data link -> ring scan of the Wi-Fi.
USB_LOCAL_PORT = 18822
ADB_CANDIDATES = (
    "/opt/homebrew/share/android-commandlinetools/platform-tools/adb",
    "/opt/homebrew/bin/adb",
    "/usr/local/bin/adb",
    str(Path.home() / "Library/Android/sdk/platform-tools/adb"),
)


def find_adb():
    found = shutil.which("adb")
    if found:
        return found
    for path in ADB_CANDIDATES:
        if os.access(path, os.X_OK):
            return path
    return None


_last_usb_try = 0
USB_RETRY_INTERVAL = 5  # seconds; `adb devices` is cheap but not free


def try_usb():
    """connect_via_usb, at most every few seconds (called from several loops)."""
    global _last_usb_try
    now = time.time()
    if now - _last_usb_try < USB_RETRY_INTERVAL:
        return False
    _last_usb_try = now
    return connect_via_usb()


def connect_via_usb():
    if os.environ.get("DROMAC_NO_USB"):  # for testing the Wi-Fi / Bluetooth paths with the cable in
        return False
    adb = find_adb()
    if not adb:
        return False
    # Named explicitly: with the phone also on network adb (audio over Wi-Fi)
    # a bare `adb forward` fails, and only the cable counts as USB here.
    serial = usb_serial()
    if not serial:
        return False
    try:
        subprocess.run(
            [adb, "-s", serial, "forward", f"tcp:{USB_LOCAL_PORT}", f"tcp:{PHONE_PORT_DEFAULT}"],
            capture_output=True, timeout=4, check=True,
        )
    except Exception:
        return False
    if not probe_dromac("127.0.0.1", USB_LOCAL_PORT, timeout=1.5):
        return False
    print(f"[usb] connected over the cable (adb forward {USB_LOCAL_PORT} -> {PHONE_PORT_DEFAULT})", file=sys.stderr)
    fetch_ble_key()
    link_found({"phone_host": "127.0.0.1", "phone_port": USB_LOCAL_PORT, "last_connected": time.time(), "method": "usb"})
    if _lock.get("on"):
        phone_request("POST", "/api/audio/guard", {"on": True, "device": _lock.get("device") or ""}, timeout=3)
    return True


_last_known_wifi_try = 0


def connect_via_known_wifi():
    """Cable gone: try the phone's remembered addresses straight away (every
    few seconds) -- its Wi-Fi one, or its hotspot one when the Mac is on the
    hotspot -- before the slower searches."""
    global _last_known_wifi_try
    if time.time() - _last_known_wifi_try < 3:
        return False
    _last_known_wifi_try = time.time()
    gw = default_gateway()
    ips = known_wifi_ips()
    for ip in ([gw] if gw in ips else []) + [i for i in ips if i != gw]:
        if probe_dromac(ip, timeout=1.0):
            print(f"[wifi] reconnected at the phone's remembered address {ip}", file=sys.stderr)
            link_found({"phone_host": ip, "phone_port": PHONE_PORT_DEFAULT, "last_connected": time.time(), "method": "wifi"})
            return True
    return False


def watchdog():
    """Automatic background reconnection using only the CHEAP methods: the
    gateway fast-path and a quick mDNS browse (a few seconds, event-driven —
    no network flooding). BLE discovery runs independently and usually wins
    the race (see handle_ble_hit).

    The ring scan is the last resort: started only after everything else has
    failed for SCAN_AFTER_DISCONNECTED seconds, capped at a /16, and abandoned
    as soon as any other method reconnects (see maybe_ring_scan)."""
    global _last_scan_attempt, _disconnected_since
    while True:
        try:
            state = load_state()
            host = state.get("phone_host")
            if host and is_phone_connected():
                if _disconnected_since is not None:
                    _set_bt_failed(False)  # next outage: try Bluetooth again before scanning
                _disconnected_since = None
                if state.get("method") != "usb":
                    try_usb()  # cable plugged in since: move onto it
                elif not BLE_KEY_FILE.exists():
                    fetch_ble_key()  # already on the cable from before the key existed
            else:
                now = time.time()
                if _disconnected_since is None:
                    _disconnected_since = now
                if try_usb():
                    pass
                elif connect_via_known_wifi():
                    pass
                elif now - _last_scan_attempt >= RESCAN_INTERVAL:
                    _last_scan_attempt = now
                    gw = default_gateway()
                    if gw and probe_dromac(gw):
                        link_found({"phone_host": gw, "phone_port": PHONE_PORT_DEFAULT, "last_connected": time.time(), "method": "gateway"})
                    else:
                        result = discover_via_mdns(timeout=3)
                        if result and probe_dromac(result["ip"], result.get("port") or PHONE_PORT_DEFAULT):
                            link_found({
                                "phone_host": result["ip"],
                                "phone_port": result.get("port") or PHONE_PORT_DEFAULT,
                                "last_connected": time.time(), "method": "mdns",
                            })
                        else:
                            maybe_bluetooth()
                            if not bluetooth_link_pending():
                                maybe_ring_scan()
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
_ble_last_ips = []
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


def handle_ble_hit(ips, port):
    global _last_ble_hit, _ble_last_seen_at, _ble_last_seen_addr, _ble_hit_count, _ble_last_logged_at
    now = time.time()
    shown = ", ".join(f"{ip}:{port}" for ip in ips)
    # Record the raw sighting unconditionally, before any of the "should we
    # act on this" logic below -- this is the part that must keep updating
    # even when we're already connected or on cooldown, so it truly reflects
    # radio reception rather than reconnect activity.
    with _ble_lock:
        _ble_last_seen_at = now
        _ble_last_seen_addr = shown
        _ble_last_ips[:] = ips
        _ble_hit_count += 1
        if now - _ble_last_logged_at >= BLE_LOG_INTERVAL:
            _ble_last_logged_at = now
            print(f"[ble] packet received from {shown} (total seen: {_ble_hit_count})", file=sys.stderr)
    try:
        state = load_state()
        # Only stand down while the current address is actually answering.
        # Skipping whenever the stored address matched the beacon used to leave
        # us pinned to an unreachable one (the phone's hotspot address, seen
        # from campus Wi-Fi) forever, with the beacon unable to fix it. A
        # working USB link is kept: the beacon doesn't list it, and it's the
        # more reliable path anyway.
        connected = _last_status["connected"] and now - _last_status["checked_at"] < 10
        if connected and (state.get("phone_host") in ips or state.get("method") == "usb"):
            return
        if now - _last_ble_hit < BLE_PROBE_COOLDOWN:
            return
        _last_ble_hit = now
        if try_usb():
            return
        if not ips:
            # the phone's here but has no address (mobile data only): the
            # Bluetooth data link is the way in -- go straight to it
            if not (connected and state.get("method") == "bluetooth"):
                maybe_bluetooth()
            return
        for ip in ips:
            if probe_dromac(ip, port):
                print(f"[ble] reconnected via {ip}:{port}", file=sys.stderr)
                link_found({"phone_host": ip, "phone_port": port, "last_connected": time.time(), "method": "ble"})
                return
        print(f"[ble] packet from {shown} but probe_dromac failed on every address (not reachable / wrong port?)", file=sys.stderr)
    except Exception:
        pass


def ble_supervisor():
    """Keeps the isolated BLE helper process running indefinitely, respawning
    it (with a short backoff) whenever it exits -- crash, Bluetooth adapter
    reset, permission prompt, anything. Runs until the app itself is quit."""
    global _ble_helper_alive, _ble_helper_last_exit_at
    if os.environ.get("DROMAC_NO_BLE"):  # for testing the other paths
        return
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

        if p == "/api/audio/pos":
            # where the sound is right now (fast-polled by the 8D view in "auto",
            # where the positions come from the conductor, not a formula)
            return self._json(fx_position_any() or {})

        if p == "/api/state":
            state = load_state()
            phone_data, err = phone_request("GET", "/api/state")
            connected = phone_data is not None
            remember_wifi_ip(phone_data)
            follow_phone_link(phone_data)
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
                "audio": dict(_audio_status, fx=dict(_fx), lock=dict(_lock), pos=fx_position() if fx_active() else None),
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
                if np and "playing" in np:
                    note_phone_playing(bool(np.get("playing")))
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
            ips = [ip for ip in (body.get("ips") or [body.get("ip", "")]) if ip]
            port = body.get("port")
            if port:
                handle_ble_hit(ips, int(port))
            return self._json({"ok": True})

        # With rain on, play/pause (the headphones' button, the media keys) steps
        # through: music paused (the rain plays on) -> rain paused too -> rain
        # back -> music back. Rain off: the usual play/pause.
        rain = _fx.get("rain") or {}
        if p == "/api/media/play_pause" and rain.get("on") and _audio_on:
            music_on = _phone_playing is not False
            if not music_on:
                if rain.get("paused"):
                    rain["paused"] = False                  # rain back first...
                    globals()["_rain_back"] = True
                    _fx["rain"] = rain; write_fx(); save_audio_prefs()
                    return self._json({"ok": True, "rain": "playing"})
                if not globals().get("_rain_back"):
                    rain["paused"] = True                   # ...after the music, the rain pauses
                    _fx["rain"] = rain; write_fx(); save_audio_prefs()
                    return self._json({"ok": True, "rain": "paused"})
            globals()["_rain_back"] = False                 # (then the music, as usual)
            if rain.get("paused"):
                rain["paused"] = False
                _fx["rain"] = rain; write_fx(); save_audio_prefs()

        if p == "/api/media/play_pause" and _phone_playing is not None:
            set_hold(_phone_playing)  # silence (or sound) right now; the phone follows
            if not _phone_playing:
                globals()["_phone_playing"] = True
            else:
                globals()["_phone_playing"] = False

        if p in ("/api/media/next", "/api/media/prev"):
            signal_skip(400)  # the phone switches ~0.4 s after the press
            _last_track["key"] = None  # this change is known; don't count it twice

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

        if p in ("/api/audio/start", "/api/audio/stop"):
            set_audio(p.endswith("start"))
            return self._json({"ok": True, "audio": dict(_audio_status, fx=dict(_fx))})

        if p == "/api/audio/catchup":
            # leaving ai split seamlessly (see dromac_fx.py): pause the phone while
            # the Mac plays out what it had buffered, then resume it in step
            phase = urllib.parse.parse_qs(parsed.query).get("phase", [""])[0]
            global _hold_set_at
            _hold_set_at = time.time() + 15  # this pause isn't yours: don't hold the audio for it
            if phase == "pause":
                phone_request("POST", "/api/media/pause", timeout=3)
            elif phase == "resume":
                phone_request("POST", "/api/media/resume_paused", timeout=3)
            return self._json({"ok": True})

        if p == "/api/audio/link":
            set_link(bool(body.get("on")))
            return self._json({"ok": True, "audio": dict(_audio_status, fx=dict(_fx), lock=dict(_lock))})

        if p == "/api/audio/lock":
            err = set_lock(bool(body.get("on")))
            return self._json({"ok": err is None, "error": err, "audio": dict(_audio_status, fx=dict(_fx), lock=dict(_lock))})

        if p == "/api/audio/fx":
            set_fx(body)
            return self._json({"ok": True, "audio": dict(_audio_status, fx=dict(_fx))})

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
    stop_leftover_audio()
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=lambda: (time.sleep(4), restore_audio()), daemon=True).start()
    threading.Thread(target=bt_link_supervisor, daemon=True).start()
    threading.Thread(target=ble_supervisor, daemon=True).start()
    threading.Thread(target=camera_poll_loop, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Dromac listening on http://127.0.0.1:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
