# Dromac

Control your Android phone wirelessly from your Mac: a phone app that
exposes the phone over your local network, a small Python server on the
Mac that finds and talks to it, and a dashboard (served by that server)
that you open as a Mac app.

## What it does

- **Now playing**: title, artist and artwork (the phone's own art, or an
  iTunes lookup), play/pause/skip, seek, like/dislike.
- **Synced lyrics** from [lrclib.net](https://lrclib.net), as a three-line
  scrolling ticker tinted from the album art. It fuzzy-matches messy
  YouTube Music titles, and for "slowed + reverb" edits it stretches the
  timeline to the edit's real length.
- **Volume**, battery and today's screen time.
- **Notifications**, with their actions and inline replies. OTPs from SMS
  are copied to the Mac clipboard automatically. Incoming calls can be
  answered from the Mac.
- **Remote screen**: live screen mirroring you can drive with the
  trackpad (click, two-finger scroll) and keyboard.
- **Camera**: a dedicated instant-capture camera on the phone, and each
  shot lands on the Mac clipboard, with one click to save it to
  `~/Documents/Dromac`.
- **Quick actions**: ring, flashlight, Do Not Disturb, wake/keep awake,
  locate, lock, launch an app, send text to the phone's clipboard.
- **Finds the phone by itself**, over Bluetooth LE, then mDNS, then a
  network scan, and reconnects when it changes networks.
- **Liquid glass theme** (optional): the dashboard rendered as Apple-style
  liquid glass over a live rain-on-glass scene. See
  [Works with Rainy Desktop](#works-with-rainy-desktop).
- **Local API** for other apps on the Mac: now playing, artwork and synced
  lyrics as JSON. See [docs/EXTERNAL_API.md](docs/EXTERNAL_API.md).

## How it fits together

```
 Android phone                          Mac
┌──────────────────────┐   LAN/HTTP   ┌───────────────────────────────┐
│ Dromac app           │◀────────────▶│ mac/server.py  (127.0.0.1:8811)│
│ StationServerService │   :8822      │  ├─ ble_helper.py (BLE finder) │
│ (+ notification,     │              │  └─ serves mac/static/ ────────┼──▶ dashboard
│  accessibility, IME, │   BLE beacon │                               │    (Dromac.app)
│  device admin)       │─────────────▶│                               │
└──────────────────────┘              └───────────────────────────────┘
```

The phone app runs an HTTP server on port 8822. The Mac server finds the
phone, proxies everything the dashboard needs, and only listens on
`127.0.0.1`, so the dashboard and its API are reachable from your Mac
alone.

## Setup

### Phone (`android/`)

Needs JDK 17 and the Android SDK (compileSdk 34). There's no Gradle wrapper
checked in; open `android/` in Android Studio, or build with Gradle 8.7+:

```bash
cd android
gradle assembleDebug
adb install app/build/outputs/apk/debug/app-debug.apk
```

Open the app and work through its buttons. Each one opens the relevant
system setting: notification access, battery optimisation, Do Not
Disturb access, usage access, display over other apps, camera, remote lock
(device admin), remote control (accessibility service), the Dromac
keyboard (for typing from the Mac), and screen sharing. Grant only what you
want to use; features whose permission is missing just stay unavailable.

### Mac (`mac/`)

Needs Python 3. Bluetooth discovery uses `bleak` and mDNS discovery uses
`zeroconf`; both are optional (Dromac falls back to a network scan):

```bash
pip3 install bleak zeroconf
./mac/install.sh
```

This installs the server into `~/Library/Application Support/Dromac` and
the launcher at `~/Applications/Dromac.app`. Re-run it to update. Open
Dromac and allow Bluetooth when asked. With the phone app running on the
same network it connects on its own; if not, use **+ link phone manually**
with the phone's Wi-Fi IP address (Android Settings → Wi-Fi → your network).

The launcher opens the dashboard in your default browser. For a proper app
window instead, open `http://127.0.0.1:8811` in Chrome and install it as an
app (⋮ → Cast, save and share → Install page as app, keeping the name
"Dromac"). The launcher picks that up and opens it from then on.

## Security

The phone app's server on port 8822 **has no authentication**. Any device
on the same network can use everything Dromac can: see and control the
screen, type, read notifications and OTPs, use the camera. Only run it on
networks you trust. There's no in-app off switch yet; on shared or public
Wi-Fi, force-stop the app (Android Settings → Apps → Dromac → Force stop).
It starts again at the next boot.

The Mac side is local-only: its server binds `127.0.0.1`, and the routes
that proxy Rainy Desktop's bridge also reject requests whose `Host` header
isn't loopback, to block DNS rebinding.

## Works with Rainy Desktop

[Rainy Desktop](https://github.com/yatharth1011/rainy-desktop) is a live
rain-on-glass wallpaper for macOS. The two talk to each other over
loopback:

- Rainy's floating radio widget shows what's playing on your phone, via
  Dromac's `/api/external/now-playing`.
- Dromac's **liquid glass theme** (the droplet button in the dashboard's
  top bar) renders Rainy's rain shader behind the dashboard and draws every
  card as a glass lens over it. With **Use Rainy Desktop's settings** on,
  it follows Rainy's live settings, rain clock and wallpaper through Rainy's
  loopback bridge (`127.0.0.1:47823`), so the rain inside the window lines
  up with the rainy desktop around it. If Rainy isn't running, it uses
  Rainy's saved settings and reads the desktop picture itself. Turn that
  option off to set everything (dimming, fog, rain, lightning, glass tint)
  in Dromac instead.

Rainy isn't required. Without it installed, the theme uses Dromac's own
settings over your desktop picture.

## FileDrop

The dashboard has a card for [FileDrop](https://github.com/yatharth1011/filedrop),
a separate password-protected LAN file drop. Install it with its own
`./install.sh` (into `~/Library/Application Support/FileDrop`), and the
card can start it, copy its URL, and open it as its own window. Without it,
the card just reports that FileDrop isn't installed.

## License

MIT, except `mac/static/glass.js`, which contains rain shader code adapted
from ["Heartfelt"](https://www.shadertoy.com/view/ltffzl) by Martijn
Steinrucken (BigWings) and is CC BY-NC-SA 3.0. See [LICENSE](LICENSE).
