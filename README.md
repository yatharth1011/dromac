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
- **Phone audio on the Mac**, like plugging in headphones: the phone's sound
  plays through the Mac (to whatever it's using: AirPods, Bluetooth
  headphones, speakers), with 8D, AI instrument separation, head tracking,
  and live rain and thunder. No screen-sharing permission needed. See
  [Phone audio on the Mac](#phone-audio-on-the-mac).
- **Finds the phone by itself**, best link first: USB cable, then Wi-Fi at
  the address its Bluetooth beacon carries (works on its hotspot too), then
  a Bluetooth data link, then a network scan. A better link takes over the
  moment it appears, and it reconnects when the phone changes networks.
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

Phone audio takes its own path:

```
 phone ──scrcpy (adb, USB or Wi-Fi)──▶ dromac_fx.py ──▶ dromac-audio ──▶ your headphones
                                        effects, 8D,      Swift player:
                                        AI split ◀──▶     follows the Mac's
                                        dromac_sep.py     output, Now Playing,
                                        (Demucs, GPU)     headphone buttons
```

## Phone audio on the Mac

Turn on the **audio link** (on the Mac or in the phone app) and the phone's
sound comes out of the Mac instead, like plugging headphones into it. It's
captured with [scrcpy](https://github.com/Genymobile/scrcpy) over adb, so
the phone shows no screen-sharing prompt: raw PCM over the USB cable, or
over Wi-Fi when unplugged (adb over Wi-Fi is set up while the cable is in).
Pulling or plugging the cable switches over without a gap: the phone pauses
for the moment it takes and the Mac plays on from a few seconds' cushion.

- **Your headphones' buttons** play, pause and skip on the phone; the Mac
  shows the phone's song in Control Center (it's the Now Playing app, so
  Apple Music stops opening).
- **Headphones only**: if the headphones drop, the Mac goes silent instead
  of falling back to its speakers, and the phone pauses instead of playing
  out loud. With the Mac playing, the phone's Bluetooth steps aside so it
  doesn't fight the Mac for the headphones, and takes them back if the Mac
  loses the phone.
- **One switch** on either side turns it all back to normal.

### 8D and effects

- **8D paths** the sound travels around you: orbit, helix, loop, ∞,
  saddle, knot, Lissajous, gyro, swing, wander, Lorenz, breathe, plus a
  **stage** (a concert in front of you) and **still**. Rendered for
  headphones with interaural time and level differences, outer-ear height
  cues, distance (level, darkness, Doppler) and a room around it (switchable).
- **Slowed + reverb**, **bass boost** (0 to +12 dB, no added delay).
- A live **3D view** of where everything is, under the now-playing card.

### Instruments, split apart

- **Live split** (instant, DSP) or **AI split** ([Demucs](https://github.com/facebookresearch/demucs)
  htdemucs on the Mac's GPU, a few seconds behind the phone; the **AI
  acceleration** slider trades GPU for delay, up to "ultramuse"). Each
  instrument then moves on its own.
- **Hear**: any mix of vocals, drums, bass and the rest (karaoke, drums only,
  just the bass...); double-click one to solo it. Leftover traces of the voice
  are moved back into the vocals so they don't leak.
- **Drum kit**: the drums split into kick, snare, toms, hi-hat and cymbals
  ([LarsNet](https://github.com/polimi-ispl/larsnet)), standing together as
  one kit (**tight**) with a **boost** to keep their punch.

### Auto: the music moves itself

The **auto ✨** path listens and decides, with nothing to label or teach:
loudness sets distance, brightness height, hits push, the chords (on the
circle of fifths) set direction. It learns the song's sections as they go by
(unsupervised, from CLAP audio embeddings) and gives each its own
arrangement; it hears drops coming (it's a few seconds ahead of you) and
draws everything in before bursting out on them; it puts whatever part stands
out in the **spotlight**; and big moves land on the bar or the phrase.
**Self-mix** listens to its own output and engineers it (unmasking parts that
clash, keeping it wide, bright, moving and balanced).

### Head tracking

The Mac's camera follows which way your head is turned (Apple Vision, nothing
recorded), and the whole 3D scene turns the other way, so sounds stay put in
the room, through a full 360° (it carries on through the turn the camera
can't see). The delay to your ears is predicted and made up; **recenter**
sets the front.

### Rain and thunder

An **ambience** layer under the music, made live so it never repeats:
thousands of drops a second (real size distribution, near and far, puddles,
leaves, drips from a gutter), swelling in waves with the wind. Thunder is
real: 40 claps and rolls cut from field recordings, cleaned of their rain,
bass-boosted, and never played the same way twice; the 3D view flashes with
the lightning first and the thunder follows, ~3 s per kilometre. Presets:
drizzle, steady, downpour, thunderstorm, distant storm, tin roof, window,
forest, and a half-hour **storm cycle** through all of them. Rain volume and
a separate **thunder** level. With rain on, play/pause pauses the music, then
the rain; play brings the rain back, then the music.

## Setup

### Phone (`android/`)

Needs JDK 17 and the Android SDK (compileSdk 34). There's no Gradle wrapper
checked in; open `android/` in Android Studio, or build with Gradle 8.7+:

```bash
cd android
gradle assembleDebug
adb install app/build/outputs/apk/debug/app-debug.apk
```

For phone audio, turn on **USB debugging** (Developer options) and connect
the cable once to allow the Mac. The optional `bthelper` module (a tiny app
with an older target SDK) lets the Mac turn the phone's Bluetooth back on
after it stepped aside for the Mac:

```bash
gradle :bthelper:assembleDebug
adb install bthelper/build/outputs/apk/debug/bthelper-debug.apk
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

Phone audio needs `scrcpy` (5.0+) and `adb`, and Xcode's command-line tools
(the player and head tracker are small Swift programs, built on first use):

```bash
brew install scrcpy android-platform-tools
xcode-select --install
```

The AI parts (AI split, drum kit, auto) need PyTorch, Demucs and Transformers
for the Python that runs Dromac (Demucs and CLAP download their own models on
first use):

```bash
pip3 install torch torchaudio demucs transformers
```

Two sets of model files aren't in this repository (their licences, or their
size); Dromac works without them, just without that feature:

- **Drum kit**: LarsNet's pretrained checkpoints (CC BY-NC 4.0), from the
  [LarsNet repository](https://github.com/polimi-ispl/larsnet), unzipped so
  that `~/Library/Application Support/Dromac/models/larsnet/kick/pretrained_kick_unet.pth`
  (and snare, toms, hihat, cymbals) exist.
- **Thunder**: ten recordings from Wikimedia Commons (listed, with authors
  and licences, in `mac/tools/make_thunder_pool.py`), built into a pool with
  that script.
  Without it, rain plays with no thunder.

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

For phone audio, adb over Wi-Fi is switched on (port 5555) while the cable
is in. adb only accepts computers you've allowed on the phone, but switch it
off (or reboot the phone) on networks you don't trust. The Bluetooth data link
is paired with a key the phone only hands over the USB cable.

The Mac side is local-only: its server binds `127.0.0.1`, and every request
must carry a loopback `Host` header (blocking DNS rebinding). Browser requests
must also come from Dromac's own page, so another website open in your browser
can't read your phone's data or trigger actions. Local tools (the launcher,
`ble_helper.py`, Rainy Desktop) are unaffected.

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

## CodeGate

The dashboard has a separate `$ codegate` card for
[CodeGate](https://github.com/yatharth1011/codegate): PIN-protected rooms where
people on your network get their own private VS Code or Ubuntu desktop in the
browser, each in an isolated container with their own copy of the files you set
up. Install it with its own `./install.sh` (into
`~/Library/Application Support/CodeGate`), and the card starts it when you open
a room. The dashboard warns you before opening one, shows each room's PIN and
the join URL, and has a manager for rooms, starter files, members (stop, save
their work as a zip, remove), an internet switch and the container runtime.
Without it, the card just says CodeGate isn't installed. See its README for how
it's isolated and what it needs (Colima + Docker).

## License

MIT, except `mac/static/glass.js`, which contains rain shader code adapted
from ["Heartfelt"](https://www.shadertoy.com/view/ltffzl) by Martijn
Steinrucken (BigWings) and is CC BY-NC-SA 3.0. See [LICENSE](LICENSE).

Models and recordings Dromac can use are not part of this repository and keep
their own licences: LarsNet's checkpoints (CC BY-NC 4.0), Demucs (MIT), CLAP
(Apache 2.0), and the thunder recordings (CC BY / CC BY-SA, credited in the
pool's `CREDITS.txt`).
