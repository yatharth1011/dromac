#!/usr/bin/env python3
"""Live effects for the phone's audio on the Mac: 8D and slowed + reverb.

Reads the WAV stream scrcpy records (s16le, 48 kHz, stereo) from the path in
argv[1] (a FIFO), processes it in 20 ms blocks, and writes raw s16le to stdout
for the dromac-audio player. The current effect settings live in argv[2] (a
small JSON file the server rewrites); it's re-read a few times a second, so
switching presets is instant and gapless.

8D: the song is folded to mono and flown along a 3D path around the
listener's head (circles, a helix, a trefoil knot, a Lissajous knot, the
Lorenz attractor...). Direction comes from the level and time differences
between the ears; height from the outer ear's spectral cues (an "air" boost
and a notch that slides from ~6 to ~10 kHz as the source rises); distance
from level, extra ear difference up close, lost highs and more room far away.

Slowed + reverb: a live stream can't actually play slower than it arrives (it
would fall further behind every second), so this keeps the tempo and does what
gives the sound its character: pitch down ~3 semitones (a two-tap delay-line
pitch shifter with crossfades) and a big, dark, long reverb.
"""
import json
import os
import sys

import numpy as np
from collections import deque

RATE = 48000
BLOCK = 960  # 20 ms
TWO_PI = 2 * np.pi

# 8D paths: where the sound is, as a curve through 3D space around your head.
# Coordinates: x = right, y = in front, z = up; 1.0 is about arm's length.
# Each preset maps phase u (radians, advancing at 2*pi per lap) to positions.
LAP_SECONDS = 8.0  # one lap at speed 1


def _orbit(u):
    return np.sin(u), np.cos(u), 0 * u


def _helix(u):
    # climbs over your head across four laps, then spirals back down
    return np.sin(u), np.cos(u), 0.85 * np.sin(u / 4)


def _loop(u):
    # a vertical wheel: overhead, right ear, under you, left ear
    return np.sin(u), 0.2 + 0 * u, np.cos(u)


def _infinity(u):
    # a figure-8 around each ear, swooping over your head where it crosses
    return 1.1 * np.sin(u), 0.8 * np.sin(2 * u), 0.75 * np.cos(u) ** 2


def _saddle(u):
    # an orbit bent like a saddle: rises in front and behind, dips at the sides
    return np.sin(u), np.cos(u), 0.65 * np.cos(2 * u)


def _knot(u):
    # trefoil knot tied around your head
    x = np.sin(u) + 2 * np.sin(2 * u)
    y = np.cos(u) - 2 * np.cos(2 * u)
    z = -np.sin(3 * u)
    return 0.45 * x, 0.45 * y, 0.55 * z


def _lissajous(u):
    # a 3:2:5 Lissajous knot (slowed 3x so its shape can be followed)
    v = u / 3
    return 1.2 * np.cos(3 * v + 0.3), 1.2 * np.cos(2 * v + 1.1), 0.7 * np.cos(5 * v + 2.0)


def _breathe(u):
    # a circle drifting from a whisper at your ear out to the far side of the room
    r = 0.45 + 1.6 * (1 - np.cos(u / 3)) / 2
    return r * np.sin(u), r * np.cos(u), 0.25 * np.sin(u / 2)


def _gyro(u):
    # an orbit in a tilted plane that slowly turns, like a precessing gyroscope
    tilt = np.radians(55)
    x0, y0, z0 = np.cos(u), np.sin(u) * np.cos(tilt), np.sin(u) * np.sin(tilt)
    p = u * 0.17
    return x0 * np.cos(p) - y0 * np.sin(p), x0 * np.sin(p) + y0 * np.cos(p), z0


def _swing(u):
    a = (np.pi / 2) * np.sin(u)
    return np.sin(a), np.cos(a), 0.1 * np.sin(2 * u)


def _wander(u):
    # smooth, never quite repeating drift in all three directions
    return (np.sin(u * 0.83 + 1.3) * 0.7 + np.sin(u * 0.31 + 0.2) * 0.6,
            np.sin(u * 0.57 + 2.1) * 0.8 + np.cos(u * 0.23) * 0.5,
            np.sin(u * 0.41 + 0.7) * 0.6)


# --- near & far: paths that dive in to your ear and fly out across the room ---
PHI = (1 + 5 ** 0.5) / 2


def _comet(u):
    # a Kepler orbit with you at the focus: lingers ~6 m out, then whips past
    # your ear at its fastest (equal areas in equal times). Solves Kepler's
    # equation M = E - e sin E by Newton's method.
    e, a = 0.9, 3.2
    M = np.mod(u, TWO_PI)
    E = M + e * np.sin(M)
    for _ in range(8):
        E = E - (E - e * np.sin(E) - M) / (1 - e * np.cos(E))
    px, py = a * (np.cos(E) - e), a * np.sqrt(1 - e * e) * np.sin(E)
    tilt = np.radians(25)  # orbit plane tilted, so it arcs over you
    return py, -px * np.cos(tilt), px * np.sin(tilt)


def _golden(u):
    # the golden spiral, r = r0 * phi^(2*theta/pi): winds out from your ear to
    # the far wall and back in, turning exactly as the golden ratio dictates
    r_min, r_max = 0.2, 6.0
    log_r = np.log(r_min) + (np.log(r_max) - np.log(r_min)) * (1 - np.cos(u / 4)) / 2
    theta = (log_r - np.log(r_min)) / (np.log(PHI) / (np.pi / 2))
    r = np.exp(log_r)
    return r * np.sin(theta), r * np.cos(theta), 0.15 * r * np.sin(u / 3)


def _rose(u):
    # a rose curve r = |cos(5/2 theta)| in 3D: every petal reaches far out and
    # comes back to graze your head
    th = u / 2
    r = 0.22 + 3.4 * np.abs(np.cos(2.5 * th))
    return r * np.sin(th), r * np.cos(th), 0.5 * np.sin(3 * th)


def _torus(u):
    # a (2,7) torus knot wound around you, swinging between near and far
    p, q, R, rr = 2, 7, 2.3, 2.0
    ph = u / 3
    w = R + rr * np.cos(q * ph)
    return w * np.cos(p * ph), w * np.sin(p * ph), 0.9 * np.sin(q * ph)


def _spiro(u):
    # a spirograph (hypotrochoid, R=5 r=3 d=2.2) that nearly touches you on every pass
    R, r, d, k = 5.0, 3.0, 2.2, 0.85
    th = u * 1.5
    x = (R - r) * np.cos(th) + d * np.cos((R - r) / r * th)
    y = (R - r) * np.sin(th) - d * np.sin((R - r) / r * th)
    return k * x, k * y, 0.3 * np.sin(u / 2)


def _pendulum(u):
    # a Foucault pendulum: swings right through your head while its plane turns
    swing = 3.0 * np.cos(u)
    plane = 0.12 * u
    return swing * np.cos(plane), swing * np.sin(plane), 0.35 - 0.05 * swing ** 2


def _zoom(u):
    # dives in from far away straight into one ear, then flies out; next dive, the other ear
    r = 0.15 + 5.5 * (1 + np.cos(u)) / 2
    az = np.pi / 2 * np.sign(np.sin(u / 2) + 1e-9) * (0.85 + 0.15 * np.cos(u / 3))
    return r * np.sin(az), r * np.cos(az) * 0.4, 0.2 * np.sin(u / 5)


def _tide(u):
    # distance and height move at golden-ratio rates to the turning, so the
    # pattern never quite repeats
    r = 0.3 + 4.7 * (1 - np.cos(u / PHI ** 2)) / 2
    return r * np.sin(u), r * np.cos(u), 0.6 * np.sin(u / PHI)


def _loxodrome(u):
    # a rhumb line on a sphere, spiralling from straight above you to under
    # your feet and back, while the sphere breathes in and out
    lat = np.radians(78) * np.cos(u / 6)
    lon = 3.0 * np.log(np.tan(np.pi / 4 + lat / 2))
    r = 1.6 + 1.2 * np.sin(u / 5)
    return r * np.cos(lat) * np.sin(lon), r * np.cos(lat) * np.cos(lon), r * np.sin(lat)


PATHS = {
    "orbit": _orbit, "helix": _helix, "loop": _loop, "infinity": _infinity,
    "saddle": _saddle, "knot": _knot, "lissajous": _lissajous, "breathe": _breathe,
    "gyro": _gyro, "swing": _swing, "wander": _wander,
    "lorenz": None,  # integrated live, see Lorenz
    "comet": _comet, "golden": _golden, "rose": _rose, "torus": _torus, "spiro": _spiro,
    "pendulum": _pendulum, "rossler": None, "zoom": _zoom, "tide": _tide, "loxodrome": _loxodrome,
}
# "still": no 3D, no motion -- the instruments where the original mix has them
# (with vocals off: just the song without the singer)
PRESETS_8D = tuple(PATHS) + ("stage", "still", "auto")


class Lorenz:
    """The Lorenz attractor, integrated live (RK4): the sound follows the
    'butterfly' of chaos theory and never retraces its path."""

    SIGMA, RHO, BETA = 10.0, 28.0, 8.0 / 3.0

    def __init__(self):
        self.s = np.array([1.0, 1.0, 20.0])

    def _deriv(self, s):
        x, y, z = s
        return np.array([self.SIGMA * (y - x), x * (self.RHO - z) - y, x * y - self.BETA * z])

    def advance(self, dt, steps=4):
        h = dt / steps
        for _ in range(steps):
            k1 = self._deriv(self.s)
            k2 = self._deriv(self.s + h / 2 * k1)
            k3 = self._deriv(self.s + h / 2 * k2)
            k4 = self._deriv(self.s + h * k3)
            self.s = self.s + h / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
        x, y, z = self.s
        return x / 15.0, (z - 25.0) / 13.0, y / 20.0  # wings left/right, loops front/back, up/down


class Rossler:
    """The Rossler attractor (a=b=0.2, c=5.7), integrated live: spirals outward
    and then snaps back in, never the same way twice."""

    A, B, C = 0.2, 0.2, 5.7

    def __init__(self):
        self.s = np.array([1.0, 1.0, 0.0])

    def _deriv(self, s):
        x, y, z = s
        return np.array([-y - z, x + self.A * y, self.B + z * (x - self.C)])

    def advance(self, dt, steps=4):
        h = dt / steps
        for _ in range(steps):
            k1 = self._deriv(self.s)
            k2 = self._deriv(self.s + h / 2 * k1)
            k3 = self._deriv(self.s + h / 2 * k2)
            k4 = self._deriv(self.s + h * k3)
            self.s = self.s + h / 6 * (k1 + 2 * k2 + 2 * k3 + k4)
        x, y, z = self.s
        return x / 2.6, y / 2.6, z / 7.0 - 0.6


def _fir_band(lo, hi, taps=127):
    """Windowed-sinc band-pass (lo=0 for a low-pass)."""
    n = np.arange(taps) - (taps - 1) / 2
    def lowpass(fc):
        return 2 * fc / RATE * np.sinc(2 * fc / RATE * n)
    h = lowpass(hi) - (lowpass(lo) if lo > 0 else 0)
    return h * np.blackman(taps)


class CueFilters:
    """Fixed filters on the mono signal; the 3D renderer mixes them with
    smoothly varying per-sample weights (no switching, so no clicks)."""

    BANDS = {
        "low": (0, 2500),         # muffle: behind, below, far away
        "front": (3000, 5000),    # a little presence when the sound is in front
        "n6": (5300, 6700),       # pinna notch positions: the notch slides up
        "n8": (7200, 8800),       #   from ~6 kHz to ~10 kHz as the source rises
        "n10": (9200, 10800),
        "air": (7000, 11000),     # the "overhead" brightness band
        "bass": (0, 180),         # proximity: bass swells as a source nears your ear
    }

    def __init__(self):
        self.h = {k: _fir_band(*v) for k, v in self.BANDS.items()}
        self.hist = np.zeros(126)

    def process(self, x):
        ext = np.concatenate([self.hist, x])
        self.hist = ext[-126:]
        return {k: np.convolve(ext, h, mode="valid") for k, h in self.h.items()}


def make_reverb_ir(seconds, damping, seed):
    """Stereo synthetic hall: decorrelated noise with an exponential decay,
    darker as it decays (damping), after a short pre-delay."""
    rng = np.random.default_rng(seed)
    n = int(RATE * seconds)
    t = np.arange(n) / RATE
    env = np.exp(-6.9 * t / seconds)  # -60 dB at `seconds`
    ir = rng.standard_normal((2, n)) * env
    # darken the tail: a low-pass whose cutoff falls over time, applied in a
    # few segments (cheap, and close enough to real air/wall absorption)
    out = np.zeros_like(ir)
    seg = n // 8
    freqs = np.fft.rfftfreq(seg * 2, 1 / RATE)
    for k in range(8):
        cutoff = 12000 * (1 - damping * k / 8) + 600
        shape = 1 / np.sqrt(1 + (freqs / cutoff) ** 4)
        part = np.zeros((2, seg * 2))
        part[:, :seg] = ir[:, k * seg:(k + 1) * seg]
        spec = np.fft.rfft(part, axis=1) * shape
        piece = np.fft.irfft(spec, axis=1)
        end = min(n, k * seg + seg * 2)
        out[:, k * seg:end] += piece[:, :end - k * seg]
    predelay = int(RATE * 0.02)
    out = np.concatenate([np.zeros((2, predelay)), out], axis=1)
    return out / np.sqrt((out ** 2).sum(axis=1, keepdims=True))  # unit energy


class Reverb:
    """Uniformly partitioned FFT convolution (block-sized partitions)."""

    def __init__(self, ir):
        self.n_fft = BLOCK * 2
        parts = int(np.ceil(ir.shape[1] / BLOCK))
        padded = np.zeros((2, parts * BLOCK))
        padded[:, :ir.shape[1]] = ir
        seg = padded.reshape(2, parts, BLOCK)
        self.H = np.fft.rfft(seg, n=self.n_fft, axis=2)          # (2, P, K)
        self.fdl = np.zeros((parts, self.n_fft // 2 + 1), complex)  # input spectra, newest first
        self.tail = np.zeros((2, BLOCK))

    def process(self, mono):
        self.fdl = np.roll(self.fdl, 1, axis=0)
        self.fdl[0] = np.fft.rfft(mono, n=self.n_fft)
        y = np.fft.irfft(np.einsum("pk,cpk->ck", self.fdl, self.H), n=self.n_fft, axis=1)
        out = y[:, :BLOCK] + self.tail
        self.tail = y[:, BLOCK:]
        return out


class FracDelay:
    """Per-sample fractional delay on a mono signal (linear interpolation)."""

    def __init__(self, max_delay):
        self.hist = np.zeros(max_delay + 2)

    def process(self, x, delay):
        ext = np.concatenate([self.hist, x])
        pos = np.arange(len(x)) + len(self.hist) - 1 - delay  # always >= 1 sample back
        i = np.floor(pos).astype(int)
        f = pos - i
        y = ext[i] * (1 - f) + ext[i + 1] * f
        self.hist = ext[-len(self.hist):]
        return y


class PitchShifter:
    """Two read taps sliding through a delay line at `ratio` speed, crossfaded
    (sin^2 windows sum to 1), so pitch changes while tempo stays put."""

    def __init__(self, window=2400):
        self.W = window
        self.hist = np.zeros(window + 2)
        self.phase = 0.0

    def process(self, x, ratio):
        n = len(x)
        ext = np.concatenate([self.hist, x])
        step = (1 - ratio) / self.W
        ph = (self.phase + step * np.arange(1, n + 1)) % 1.0
        self.phase = ph[-1]
        base = np.arange(n) + len(self.hist)
        out = np.zeros(n)
        for offset in (0.0, 0.5):
            p = (ph + offset) % 1.0
            pos = base - 1 - p * self.W
            i = np.floor(pos).astype(int)
            f = pos - i
            out += (ext[i] * (1 - f) + ext[i + 1] * f) * np.sin(np.pi * p) ** 2
        self.hist = ext[-len(self.hist):]
        return out


class Spatializer:
    """Places one mono source at a 3D position (per sample) for headphones.
    Direction from the level and time difference between the ears; height
    from the outer ear's cues ("air" boost, a notch sliding ~6 -> 10 kHz as it
    rises); distance from level, extra ear difference up close, lost highs far
    away, and travel time -- whose change with distance is the Doppler shift."""

    def __init__(self):
        # each ear's delay = travel time from the source (~140 samples per metre,
        # up to ~8 m) plus the time difference between the ears
        self.itd = [FracDelay(1300), FracDelay(1300)]
        self.cues = CueFilters()
        self.dry_delay = np.zeros(63)  # matches the cue filters' latency

    def render(self, mono, pos):
        """-> (stereo direct sound, the dry signal for the room reverb, mean distance)"""
        r = np.clip(np.linalg.norm(pos, axis=1), 0.15, 8.0)
        lat = pos[:, 0] / r       # +1 right .. -1 left
        front = pos[:, 1] / r     # +1 in front .. -1 behind
        up = pos[:, 2] / r        # +1 overhead .. -1 below

        c = self.cues.process(mono)
        ext = np.concatenate([self.dry_delay, mono])
        self.dry_delay = ext[-63:]
        dry = ext[:BLOCK]
        shaped = dry.copy()
        shaped += 0.9 * np.clip(up, 0, 1) * c["air"]
        pos_n = np.clip((up + 1) / 2, 0, 1) * 2            # 0..2 across n6, n8, n10
        w6 = np.clip(1 - pos_n, 0, 1)
        w10 = np.clip(pos_n - 1, 0, 1)
        w8 = 1 - w6 - w10
        shaped -= 0.7 * (w6 * c["n6"] + w8 * c["n8"] + w10 * c["n10"])
        shaped += 0.25 * np.clip(front, 0, 1) * c["front"]
        back = np.clip(-front, 0, 1) * (1 - np.abs(up))
        below = np.clip(-up, 0, 1)
        far = np.clip((r - 1.0) / 4.0, 0, 1)
        m = np.clip(0.55 * back + 0.45 * below + 0.6 * far, 0, 0.9)
        shaped = shaped * (1 - m) + c["low"] * m
        shaped += 1.4 * np.clip(0.6 - r, 0, 0.45) * c["bass"]   # proximity: bass swells up close

        raw = (1.0 / r) ** 0.85
        level = 2.4 * np.tanh(raw / 2.4)          # whispers stay loud but never blast
        near = np.clip(1.0 - r, 0, 0.85)          # up close the far ear hears much less
        gR = np.sqrt((1 + lat) / 2) * (1 - near * np.clip(-lat, 0, 1))
        gL = np.sqrt((1 - lat) / 2) * (1 - near * np.clip(lat, 0, 1))
        max_itd = 0.00066 * RATE
        travel = r * RATE / 343.0                 # speed of sound
        left = self.itd[0].process(shaped, travel + np.clip(lat, 0, 1) * max_itd) * gL * level
        right = self.itd[1].process(shaped, travel + np.clip(-lat, 0, 1) * max_itd) * gR * level
        return np.stack([left, right]), dry, float(r.mean())


# ---------------- splitting a song into instruments ----------------
STEMS = ("vocals", "drums", "bass", "other")


class LiveSplit:
    """Instant, maths-only split into vocals / drums / bass / other (~0.1 s).
    Short-time spectra of the song, then:
      drums  -- percussive: what's spiky across frequency (median over
                frequency beats median over time; harmonic/percussive separation)
      bass   -- the harmonic part below ~160 Hz
      vocals -- the harmonic part that's identical in both channels (mixed to
                the centre), in the voice's range
      other  -- what's left, kept in stereo
    Approximate (some bleed between parts), but live."""

    N, H, FUT = 1920, 480, 4          # frame, hop (2 per block), future frames for the time median

    def __init__(self):
        n = np.arange(self.N)
        self.win = np.sqrt(0.5 - 0.5 * np.cos(TWO_PI * n / self.N))   # sqrt-Hann: analysis x synthesis = Hann
        self.inbuf = np.zeros((2, self.N))
        self.frames = []                                   # recent (XL, XR, magnitude)
        f = np.fft.rfftfreq(self.N, 1 / RATE)
        self.bass_w = np.clip((200 - f) / 60, 0, 1)        # below ~160 Hz, smooth edge
        self.voice_w = np.clip((f - 150) / 100, 0, 1) * np.clip((8000 - f) / 2000, 0, 1)
        # overlap-add outputs: vocals, drums, bass (mono) and other (stereo)
        self.ola = np.zeros((5, self.N))
        self.out = np.zeros((5, 0))

    def _hop(self, chunk):
        self.inbuf = np.concatenate([self.inbuf[:, self.H:], chunk], axis=1)
        X = np.fft.rfft(self.inbuf * self.win, axis=1)
        mag = np.abs(X[0] + X[1]) / 2
        self.frames.append((X, mag))
        if len(self.frames) > 2 * self.FUT + 1:
            self.frames.pop(0)
        if len(self.frames) < 2 * self.FUT + 1:
            self._emit(np.zeros((5, self.N)))
            return
        Xc, mc = self.frames[self.FUT]                       # the frame in the middle of the window
        harm = np.median(np.stack([m for _, m in self.frames]), axis=0)
        padded = np.pad(mc, 8, mode="edge")
        perc = np.median(np.lib.stride_tricks.sliding_window_view(padded, 17), axis=1)
        p2, h2 = perc ** 2, harm ** 2
        m_drums = p2 / (p2 + h2 + 1e-12)
        m_harm = 1 - m_drums
        XL, XR = Xc
        sim = 2 * np.abs(XL * np.conj(XR)) / (np.abs(XL) ** 2 + np.abs(XR) ** 2 + 1e-12)
        pan = np.abs(np.abs(XL) - np.abs(XR)) / (np.abs(XL) + np.abs(XR) + 1e-12)
        centre = sim ** 4 * (1 - pan)
        m_bass = m_harm * self.bass_w
        m_voc = m_harm * (1 - self.bass_w) * centre * self.voice_w
        m_other = np.clip(m_harm - m_bass - m_voc, 0, 1)
        mid = (XL + XR) / 2
        spec = np.stack([m_voc * mid, m_drums * mid, m_bass * mid, m_other * XL, m_other * XR])
        self._emit(np.fft.irfft(spec, n=self.N, axis=1) * self.win / 2)  # Hann at N/4 hops sums to 2

    def _emit(self, frame):
        self.ola += frame
        self.out = np.concatenate([self.out, self.ola[:, :self.H]], axis=1)
        self.ola = np.concatenate([self.ola[:, self.H:], np.zeros((5, self.H))], axis=1)

    def process(self, stereo):
        """stereo block in -> (5, BLOCK) stems out: vocals, drums, bass, other L, other R."""
        for k in range(0, BLOCK, self.H):
            self._hop(stereo[:, k:k + self.H])
        out, self.out = self.out[:, :BLOCK], self.out[:, BLOCK:]
        return out


class AISplit:
    """Studio-grade split (Demucs htdemucs) in a worker process, streamed:
    the song is cut into 6 s chunks, each separated with 1.5 s of context on
    both sides (so the edges are clean) and stitched with short crossfades.
    Playback runs DELAY seconds behind the phone -- the price of the quality.
    Until the first chunk is back, it plays nothing."""

    # How far behind the phone it plays is set by the "AI acceleration" slider:
    # one Demucs call costs ~0.5 s of GPU whatever the window (up to ~5 s), so
    # smaller chunks = more calls = more GPU = less delay. See set_accel.
    # Context around each chunk: 1 s ahead (that costs delay) and 3 s behind
    # (free -- it's already here). Demucs learned on ~8 s excerpts; with only a
    # second either side it loses the voice's thread and lets bits of it leak
    # into the band.
    CTX, CTX_BEFORE, XF = RATE, 3 * RATE, 960
    CHUNK = 1 * RATE                       # defaults (50% GPU), until set_accel
    DELAY = int(2.8 * RATE)
    RING = 90 * RATE

    def __init__(self, worker_path):
        import threading
        import struct
        self.struct = struct
        self.worker_path = worker_path
        self.log_path = os.path.join(os.path.dirname(os.path.abspath(worker_path)), "sep.log")
        self.proc = None
        self.ready = False
        self.device = None
        self.call_started = None          # when the call in flight began (for the hang watchdog)
        self.t = 0                                           # samples received
        self.inp = np.zeros((2, self.RING), np.float32)      # ring of input
        # ring of separated stems: vocals, drums, bass, other L, other R, then
        # the drum kit (kick, snare, toms, hihat, cymbals) where it was asked for
        self.stems = np.zeros((10, self.RING), np.float32)
        self.kit = False                  # ask the worker for the kit too
        self.kit_from = self.kit_to = -1  # the stretch whose kit rows are real
        self.kit_here = False             # the block just pulled has them
        self.done_upto = 0                                    # stems valid below this absolute index
        self.rd = -self.DELAY                                 # next sample to play (absolute)
        # a skipped-to song: everything before `floor` is the old song -- not
        # played any more, and not used as context for the new one
        self.floor = 0
        self.gen = 0
        self.next_chunk = 0
        self.chunk, self.delay = self.CHUNK, self.DELAY
        self.call_time = 0.5               # measured Demucs call time (s), smoothed
        self.last_push = 0.0
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.alive = True
        self._spawn()
        threading.Thread(target=self._worker_loop, daemon=True).start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    def _spawn(self):
        """Starts the worker. Its stderr is read for as long as it runs -- into
        sep.log -- or a burst of warnings would fill the pipe and freeze it."""
        import subprocess
        import threading
        self.ready = False
        self.proc = subprocess.Popen([sys.executable, self.worker_path], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        threading.Thread(target=self._read_stderr, args=(self.proc,), daemon=True).start()

    def _read_stderr(self, proc):
        try:
            if os.path.exists(self.log_path) and os.path.getsize(self.log_path) > 1 << 20:
                os.replace(self.log_path, self.log_path + ".old")
            log = open(self.log_path, "a")
        except Exception:
            log = None
        for raw in proc.stderr:
            line = raw.decode(errors="ignore").rstrip()
            if line.startswith("ready") and proc is self.proc:
                self.device = line.split()[-1]
                self.ready = True
            if log:
                log.write(line + "\n")
                log.flush()

    def _restart(self, why):
        """The worker died or hung: start a fresh one (the chunk it was on is
        redone) -- the music pauses for a moment instead of going silent."""
        import time
        try:
            self.proc.kill()
        except Exception:
            pass
        try:
            with open(self.log_path, "a") as f:
                f.write(f"--- {time.strftime('%H:%M:%S')} worker restarted: {why}\n")
        except Exception:
            pass
        self._spawn()

    def _watchdog(self):
        """A call that never comes back (a hung worker) is ended after 40 s."""
        import time
        while self.alive:
            time.sleep(1)
            started = self.call_started
            if started is not None and time.time() - started > 40:
                try:
                    self.proc.kill()            # its read fails, and the loop restarts it
                except Exception:
                    pass

    def _ring_read(self, ring, start, n):
        """n samples of a ring from absolute index start (two plain slices: a
        fraction of the cost of index arrays, so the audio loop isn't held up)."""
        s0 = start % self.RING
        first = min(n, self.RING - s0)
        if first == n:
            return ring[:, s0:s0 + n].copy()
        return np.concatenate([ring[:, s0:], ring[:, :n - first]], axis=1)

    def _ring_write(self, ring, start, data):
        n = data.shape[1]
        s0 = start % self.RING
        first = min(n, self.RING - s0)
        ring[:, s0:s0 + first] = data[:, :first]
        if first < n:
            ring[:, :n - first] = data[:, first:]

    def _window(self, start, end):
        out = np.zeros((2, end - start), np.float32)
        lo = max(start, self.t - self.RING, 0, self.floor)
        if end > lo:
            out[:, lo - start:] = self._ring_read(self.inp, lo, end - lo)
        return out

    def set_accel(self, gpu_pct):
        """Share of the GPU to use (15-80 %): chunk = call time / share, and the
        delay is the chunk + look-ahead + one call's time + a little margin."""
        share = min(0.8, max(0.15, gpu_pct / 100))
        self.chunk = int(min(3.5, max(0.6, self.call_time / share)) * RATE)
        self.delay = self.chunk + self.CTX + int((self.call_time * 1.4 + 0.15) * RATE)

    def _worker_loop(self):
        import time
        st = self.struct
        while self.alive:
            self.wake.wait(0.05)
            self.wake.clear()
            while self.alive and self.ready:
                gen = self.gen
                k0 = self.next_chunk
                chunk = self.chunk
                if self.t < k0 + chunk + self.CTX:
                    # Input has stalled (the phone's paused for a route change or
                    # a catch-up): separate what's there now without waiting for
                    # look-ahead that isn't coming -- so all of the delay, not just
                    # part of it, carries playback through the pause.
                    # And the same when playback is about to run out of separated
                    # audio (just after such a pause, or with a short delay set):
                    # never let the music stop for want of look-ahead.
                    tail = self.t - k0
                    stalled = time.time() - self.last_push > 0.4
                    starving = self.done_upto - self.rd < int((self.call_time + 0.3) * RATE)
                    if tail >= BLOCK and (stalled or (starving and self.rd >= 0)):
                        chunk = tail
                    else:
                        break
                before = self.CTX_BEFORE
                w0, w1 = k0 - before, k0 + chunk + self.CTX
                t_call = time.time()
                with self.lock:
                    win = self._window(w0, w1)
                try:
                    flag = 0x80000000 if self.kit else 0
                    self.call_started = t_call
                    self.proc.stdin.write(st.pack("<I", win.shape[1] | flag) + win.T.astype("<f4").tobytes())
                    self.proc.stdin.flush()
                    head = self.proc.stdout.read(8)
                    if len(head) < 8:
                        raise EOFError("the worker stopped")
                    n, count = st.unpack("<II", head)
                    sep = np.frombuffer(self.proc.stdout.read(n * count * 4), "<f4").reshape(count, n)
                except Exception as e:
                    self.call_started = None
                    if not self.alive:
                        return
                    self._restart(type(e).__name__ + (": " + str(e) if str(e) else ""))
                    break                    # back to waiting until the new one is ready
                self.call_started = None
                self.call_time = 0.8 * self.call_time + 0.2 * (time.time() - t_call)
                # stems: vocals, drums, bass mono; "other" kept as the window's stereo
                # minus the other three (so nothing's lost and it keeps its width)
                voc, drm, bas, oth = sep[:4]
                rest = win - (voc + drm + bas)[None] * 1.0
                has_kit = sep.shape[0] >= 9
                kit = sep[4:9] if has_kit else np.zeros((5, n), np.float32)
                rows = np.concatenate([np.stack([voc, drm, bas, rest[0], rest[1]]), kit])
                seg = rows[:, before - self.XF:before + chunk]      # chunk + crossfade lead-in
                a0 = k0 - self.XF
                fade = np.linspace(0, 1, self.XF, dtype=np.float32)
                if gen != self.gen:
                    continue                 # a skip came in while this was separating: drop it
                with self.lock:
                    cur = self._ring_read(self.stems, a0, self.XF)
                    seg[:, :self.XF] = cur * (1 - fade) + seg[:, :self.XF] * fade if k0 > 0 else seg[:, :self.XF]
                    self._ring_write(self.stems, a0, seg)
                    self.done_upto = k0 + chunk
                    if has_kit:
                        if self.kit_to != k0:
                            self.kit_from = k0           # a new run of kit chunks
                        self.kit_to = k0 + chunk
                    elif self.kit_to > k0:
                        self.kit_to = k0
                if gen == self.gen:          # not skipped meanwhile
                    self.next_chunk = k0 + chunk

    def push(self, stereo):
        import time
        with self.lock:
            idx = np.arange(self.t, self.t + BLOCK) % self.RING
            self.inp[:, idx] = stereo
            self.t += BLOCK
        self.last_push = time.time()
        self.wake.set()

    def restart_from(self, history):
        """Continue from the start of `history` (raw audio not yet heard): it's
        separated first, so playback resumes exactly there after the wait."""
        with self.lock:
            self.gen += 1
            n = history.shape[1]
            base = self.t
            if n:
                idx = np.arange(base, base + n) % self.RING
                self.inp[:, idx] = history
            self.t = base + n
            self.rd = base
            self.floor = base
            self.next_chunk = base
            self.done_upto = base
        self.wake.set()

    def skip_to(self, at):
        """The song changed by a skip at input position `at`: stop the old one
        now (a short fade) and separate the new one from its first note."""
        with self.lock:
            self.gen += 1
            self.floor = at
            self.next_chunk = at
            self.done_upto = min(self.done_upto, at)
            self._fade_next = True
        self.wake.set()

    def next_is_silent(self):
        """The next block to play is (near) silence -- a safe place to wait."""
        start = self.rd
        if start < 0 or start + BLOCK > self.done_upto:
            return False
        with self.lock:
            block = self.stems[:, np.arange(start, start + BLOCK) % self.RING]
        return float(np.abs(block).max()) < 0.002

    def pull(self):
        start = self.rd
        if start < self.floor and self.floor > 0:
            # the old song after a skip: fade out on the first block, then quiet
            # until the new one is separated
            self.rd += BLOCK
            if getattr(self, "_fade_next", False) and start >= 0 and start + BLOCK <= self.floor:
                self._fade_next = False
                with self.lock:
                    old = self.stems[:, np.arange(start, start + BLOCK) % self.RING].astype(np.float64)
                self.kit_here = self.kit_from <= start and start + BLOCK <= self.kit_to
                return old * np.linspace(1, 0, BLOCK)
            return np.zeros((10, BLOCK))
        if start < 0 or start + BLOCK > self.done_upto:
            if start < 0:
                self.rd += BLOCK
            return np.zeros((10, BLOCK))                     # still separating the first chunk
        self.kit_here = self.kit_from <= start and start + BLOCK <= self.kit_to
        self.rd += BLOCK
        with self.lock:
            return self.stems[:, np.arange(start, start + BLOCK) % self.RING].astype(np.float64)

    def process(self, stereo):
        self.push(stereo)
        return self.pull()

    @property
    def delay_samples(self):
        return self.t - self.rd

    @property
    def warming(self):
        return self.rd < 0 or self.rd + BLOCK > self.done_upto

    def close(self):
        self.alive = False
        try:
            self.proc.kill()
        except Exception:
            pass


# The cushion every mode but ai split plays from: ~2.5 s behind the phone, so a
# route change (cable out -> Wi-Fi, ~1.5-2 s all told) happens without a gap.
CONT = int(2.5 * RATE)


# where each instrument stands in the "stage" layout (x right, y front, z up; metres)
STAGE = {
    "vocals": (0.0, 1.5, 0.15),      # the singer, right in front of you
    "drums": (0.0, 3.2, 0.6),        # behind the singer, up on the riser
    "bass": (0.9, 1.1, -0.35),       # low and close, front right
    "other_l": (-2.2, 1.6, 0.2),     # guitars, keys, synths: wide left...
    "other_r": (2.2, 1.6, 0.2),      # ...and wide right
    # the drum kit, split ("drum kit" on): the riser opened out around you
    "kick": (0.0, 2.6, -0.5),        # low, straight ahead
    "snare": (-0.7, 2.2, 0.3),       # front left, at hand height
    "toms": (1.3, 2.6, 0.2),         # across to the right
    "hihat": (-1.4, 1.6, 0.5),       # left, close, crisp
    "cymbals": (0.4, 3.4, 1.5),      # high above the kit
}
NAMES = ("vocals", "drums", "bass", "other_l", "other_r")
# with the kit split: the kick takes the drums' place (and so its role), and
# the other pieces come after the band
NAMES_KIT = ("vocals", "kick", "bass", "other_l", "other_r", "snare", "toms", "hihat", "cymbals")
KIT = (1, 5, 6, 7, 8)                 # the kit's places in NAMES_KIT

# The drum kit, split, stays one kit: its pieces stand around one centre (where
# the drums would be) and travel together, a real kit's width apart -- you hear
# the kick, snare, toms, hi-hat and cymbals in their places, and every hit
# still lands from one direction instead of smearing round your head. Offsets
# are (to your right, away from you, up) in metres, at 1.5 m; further off the
# kit scales up, so it keeps its width to the ear.
KIT_OFFSETS = {1: (0.0, 0.0, -0.3),      # kick: the middle, low
               5: (-0.3, -0.2, 0.0),     # snare: front left, at hand height
               6: (0.35, 0.05, 0.1),     # toms: across to the right
               7: (-0.5, -0.05, 0.25),   # hi-hat: left, a little up
               8: (0.2, 0.15, 0.5)}      # cymbals: up over the kit
KIT_BOOST = 10 ** (2.5 / 20)            # +2.5 dB for the split kit: the hits keep their punch


def kit_around(center, k):
    """Where kit piece k stands, around the kit's centre (a point, or BLOCK x 3)."""
    c = np.atleast_2d(np.asarray(center, float))
    horiz = np.hypot(c[:, 0], c[:, 1])
    fwd = np.where(horiz[:, None] > 0.05, c[:, :2] / np.maximum(horiz, 1e-6)[:, None], [0.0, 1.0])
    right = np.stack([fwd[:, 1], -fwd[:, 0]], axis=1)
    scale = np.clip(np.linalg.norm(c, axis=1) / 1.5, 0.6, 2.0)[:, None]
    dr, do, du = KIT_OFFSETS[k]
    off = np.concatenate([(right * dr + fwd * do), np.full((len(c), 1), du)], axis=1) * scale
    out = c + off
    return out if np.ndim(center) > 1 else out[0]


# How each instrument travels a path in the split modes: its own speed and its
# own starting point on the curve -- so they overtake each other, swap places,
# and on knotted curves take different routes through space (a rigid rotation
# of one point kept them in the same order forever). The two halves of "the
# rest" stay partners, half a lap apart, so it keeps its width.
#             vocals      drums/kick        bass            other L              other R
STEM_SPEED = (1.00,       1.17,             0.83,           1.29,                1.29,
#             snare       toms              hihat           cymbals
              1.41,       0.71,             1.53,           0.91)
STEM_PHASE = (0.0,        TWO_PI / 4,       TWO_PI / 2,     3 * TWO_PI / 4,      3 * TWO_PI / 4 + TWO_PI / 2,
              TWO_PI / 8, 5 * TWO_PI / 8,   3 * TWO_PI / 8, 7 * TWO_PI / 8)


def rotate_z(xyz, angle):
    x, y, z = xyz
    c, s = np.cos(angle), np.sin(angle)
    return x * c - y * s, x * s + y * c, z


def _tell_server(path):
    """Fire-and-forget POST to Dromac's server (used to pause/resume the phone)."""
    import threading
    import urllib.request

    def go():
        try:
            req = urllib.request.Request("http://127.0.0.1:8811" + path, data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


class SongLearner:
    """Learns each song's structure while it plays -- no labels, no categories.

    Every 2 s the last 5 s of the song go to a listening model (dromac_mood.py,
    CLAP's audio half) and come back as a 512-number fingerprint. From those:
      * the song's own main directions of change (principal components of
        its fingerprints so far) -- three numbers that move continuously and
        shape the space: how open, how high, how fast and which way it turns;
      * its distinct states (verse feel, chorus feel, a breakdown...) found by
        clustering as it goes: a fingerprint unlike every state so far starts
        a new one -- "unlike" measured against how much this song normally
        varies from one moment to the next, also learned;
      * a formation for each state, generated from it and fixed, so when the
        song comes back to a state, the space comes back to its formation.
    Starts over with each new song."""

    WINDOW, EVERY = 5 * RATE, 2 * RATE   # a 5 s fingerprint every 2 s

    def __init__(self, worker_path):
        import subprocess
        import threading
        self.proc = subprocess.Popen([sys.executable, worker_path], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.ready = False
        self.ring = np.zeros(self.WINDOW, np.float32)
        self.since = 0
        self.lock = threading.Lock()
        self.want = threading.Event()
        self.alive = True
        self.reset()
        threading.Thread(target=self._wait_ready, daemon=True).start()
        threading.Thread(target=self._loop, daemon=True).start()

    def reset(self):
        self.embs = []                  # this song's fingerprints
        self.centroids = []             # its states
        self.counts = []
        self.state = 0
        self.steps = deque(maxlen=50)   # how far it moves between fingerprints
        self.comp = np.zeros(3)         # position on its main directions (z-scores)
        self.comp_s = np.zeros(3)       # smoothed, for the motion
        self.formation_s = None

    def _wait_ready(self):
        for line in self.proc.stderr:
            if line.decode(errors="ignore").startswith("ready"):
                self.ready = True
                return

    def feed(self, mono):
        """Called every block with the song (mono)."""
        n = len(mono)
        self.ring = np.concatenate([self.ring[n:], mono.astype(np.float32)])
        self.since += n
        if self.since >= self.EVERY and self.ready:
            self.since = 0
            self.want.set()
        # ease the continuous part toward the latest learned position
        self.comp_s += (self.comp - self.comp_s) * 0.01

    def _loop(self):
        import struct
        while self.alive:
            self.want.wait(1.0)
            if not self.want.is_set():
                continue
            self.want.clear()
            audio = self.ring.copy()
            if float(np.abs(audio).max()) < 1e-3:
                continue                                  # silence teaches nothing
            try:
                self.proc.stdin.write(struct.pack("<I", len(audio)) + audio.astype("<f4").tobytes())
                self.proc.stdin.flush()
                emb = np.frombuffer(self.proc.stdout.read(512 * 4), "<f4").astype(np.float64)
            except Exception:
                self.alive = False
                return
            with self.lock:
                self._learn(emb)

    def _learn(self, e):
        if self.embs:
            self.steps.append(float(np.linalg.norm(e - self.embs[-1])))
        self.embs.append(e)
        if len(self.embs) > 120:
            self.embs.pop(0)
        # states: the nearest one -- or, if this is unlike all of them, a
        # candidate. A new state needs patience: three fingerprints in a row
        # (~6 s) that agree with each other and differ from everything known.
        # (A window straddling a verse/chorus boundary is half of each and
        # never lasts that long, so it doesn't become a state of its own.)
        # "Unlike": a few times this song's own usual step, within sane bounds.
        typical = float(np.median(self.steps)) if len(self.steps) >= 3 else 0.12
        lam = float(np.clip(3.0 * typical, 0.25, 0.7))
        if not self.centroids:
            self.centroids.append(e.copy()); self.counts.append(1); self.state = 0
            self.pending = []
        else:
            d = [float(np.linalg.norm(e - c)) for c in self.centroids]
            k = int(np.argmin(d))
            if d[k] <= lam:
                self.counts[k] += 1
                self.centroids[k] += (e - self.centroids[k]) / min(self.counts[k], 20)
                self.state = k
                self.pending = []
            else:
                self.pending = (getattr(self, "pending", []) + [e])[-3:]
                agree = len(self.pending) == 3 and all(
                    np.linalg.norm(a - b) <= lam for i, a in enumerate(self.pending) for b in self.pending[i + 1:])
                if agree and len(self.centroids) < 12:
                    self.centroids.append(np.mean(self.pending, 0)); self.counts.append(3)
                    self.state = len(self.centroids) - 1
                    self.pending = []
                # (otherwise: unsure yet -- stay in the current state)
        # its main directions of change, and where it is along them now
        if len(self.embs) >= 4:
            X = np.array(self.embs)
            mu = X.mean(0)
            _, sv, vt = np.linalg.svd(X - mu, full_matrices=False)
            k = min(3, len(sv))
            proj = (e - mu) @ vt[:k].T
            scale = sv[:k] / np.sqrt(len(X)) + 1e-9
            comp = np.zeros(3)
            comp[:k] = np.clip(proj / scale, -2.5, 2.5)
            self.comp = comp

    def formation(self, n, state=None):
        """The current state's formation (or `state`'s), generated from the
        state's number (so a state always gets the same one), eased between
        states."""
        rng = np.random.default_rng(1000 + (self.state if state is None else state))
        f = {"offsets": rng.uniform(-np.pi, np.pi, n), "spread": rng.uniform(0.75, 1.45),
             "lift": rng.uniform(-0.45, 0.45), "turn": rng.choice([-1.0, 1.0])}
        if self.formation_s is None or len(self.formation_s["offsets"]) != n:
            self.formation_s = {k: (np.array(v, float) if k == "offsets" else float(v)) for k, v in f.items()}
        s = self.formation_s
        d = (f["offsets"] - s["offsets"] + np.pi) % TWO_PI - np.pi
        s["offsets"] = s["offsets"] + d * 0.01
        for key in ("spread", "lift", "turn"):
            s[key] += (f[key] - s[key]) * 0.01
        return s

    def report(self):
        return {"state": self.state + 1, "states": len(self.centroids),
                "learned": len(self.embs), "axes": [round(float(x), 2) for x in self.comp_s]}

    def close(self):
        self.alive = False
        try:
            self.proc.kill()
        except Exception:
            pass


class Engineer:
    """Auto's ears on its own output: every 2 s it listens to the sound it has
    actually made -- the binaural mix as it leaves for your headphones, next
    to the song as it came in -- and steers the conductor toward a better one,
    the way a mix engineer rides a live mix. No labels, nothing asked of you:
    only measurements of the sound.

      masking   parts with overlapping spectra sitting close together blur
                each other           -> push them apart (separation)
      width     interaural cross-correlation (IACC), the standard measure of
                spaciousness: near 1 is narrow, mono-like; very low falls
                apart                -> spread out or draw in
      dullness  the output's brightness against the input's (sounds behind
                and below lose their top end)  -> bring things forward and up
      motion    how much the level difference between the ears moves: too
                little and the 8D is inaudible, too much and it's seasick
                                     -> more or less movement
      balance   one ear louder for long         -> turn the field back

    Its settings drift back toward neutral slowly, and carry over between songs
    (halfway reset) as its own style."""

    DEFAULTS = {"spread": 1.0, "lift": 0.0, "front": 0.0, "motion": 1.0, "separation": 0.5, "turn": 0.0}
    LIMITS = {"spread": (0.85, 1.2), "lift": (-0.25, 0.25), "front": (0.0, 0.35), "motion": (0.6, 1.8),
              "separation": (0.0, 1.5), "turn": (-0.8, 0.8)}
    WINDOW = 100                        # blocks: 2 s

    def __init__(self):
        self.enabled = True             # the "self-mix" switch
        self.knobs = dict(self.DEFAULTS)
        self.out = []                   # this window's output blocks (stereo)
        self.inp = []                   # and input (mono)
        self.masking = 0.0              # from the conductor, this window
        self.metrics = {}
        self.doing = ""                 # what it last did, in words (for the caption)

    def new_song(self):
        for k, d in self.DEFAULTS.items():
            self.knobs[k] += (d - self.knobs[k]) * 0.5

    @property
    def active(self):
        """The settings in use: its own while switched on, neutral when off."""
        return self.knobs if self.enabled else self.DEFAULTS

    def listen(self, out, mono, masking):
        if not self.enabled:
            self.out, self.inp, self.doing = [], [], ""
            return
        self.out.append(out)
        self.inp.append(mono)
        self.masking = 0.9 * self.masking + 0.1 * masking
        if len(self.out) >= self.WINDOW:
            self._judge(np.concatenate(self.out, axis=1), np.concatenate(self.inp))
            self.out, self.inp = [], []

    @staticmethod
    def _centroid(*chans):
        """Spectral centroid -- of each ear's own spectrum (power-averaged; adding
        the ears first would cancel highs between them and read as dull)."""
        n = min(len(chans[0]), 16384)
        power = sum(np.abs(np.fft.rfft(c[-n:] * np.hanning(n))) ** 2 for c in chans)
        mag = np.sqrt(power)
        f = np.fft.rfftfreq(n, 1 / RATE)
        return float((mag * f).sum() / (mag.sum() + 1e-12))

    def _judge(self, out, inp):
        L, R = out[0], out[1]
        eL, eR = float((L ** 2).sum()), float((R ** 2).sum())
        if eL + eR < 1e-6 or float((inp ** 2).sum()) < 1e-8:
            return                                          # silence: nothing to judge
        # IACC in 500 Hz - 4 kHz (as it's measured in halls: below that, the
        # ears hear nearly the same wave wherever a sound is, and bass would
        # make every mix read as mono)
        n = min(len(L), 32768)
        FL, FR = np.fft.rfft(L[-n:], 2 * n), np.fft.rfft(R[-n:], 2 * n)
        f = np.fft.rfftfreq(2 * n, 1 / RATE)
        band = (f >= 500) & (f <= 4000)
        FL, FR = FL * band, FR * band
        xc = np.fft.irfft(FL * np.conj(FR))
        lag = int(0.001 * RATE)                             # +-1 ms: the ears' range
        norm = np.sqrt(float((np.abs(FL) ** 2).sum()) * float((np.abs(FR) ** 2).sum())) / (2 * n) + 1e-12
        iacc = min(1.0, float(np.max(np.abs(np.concatenate([xc[:lag], xc[-lag:]])))) / norm)
        dull = self._centroid(L, R) / (self._centroid(inp) + 1e-6)
        frames = len(L) // 2400                             # 50 ms
        lf = (L[:frames * 2400].reshape(frames, -1) ** 2).mean(1)
        rf = (R[:frames * 2400].reshape(frames, -1) ** 2).mean(1)
        ild = 10 * np.log10((lf + 1e-10) / (rf + 1e-10))
        motion = float(np.std(ild))
        balance = 10 * np.log10(eL / (eR + 1e-12))
        self.metrics = {"width": round(1 - iacc, 2), "brightness": round(dull, 2), "motion_db": round(motion, 1),
                        "balance_db": round(balance, 1), "masking": round(self.masking, 2)}
        # Each fix has its own lever, so they can't fight: width is made by
        # spacing the parts apart round you (never by sending them further
        # off -- distance dulls them), brightness by bringing them a little
        # closer and up, and toward the front only while it's wide enough.
        k, did = self.knobs, []
        if iacc > 0.75:
            k["separation"] += 0.1; k["front"] -= 0.05; did.append("widening")
        elif iacc < 0.2:
            k["separation"] -= 0.05; did.append("drawing in")
        if dull < 0.75:
            k["spread"] -= 0.02; k["lift"] += 0.02; did.append("brightening")
            if iacc < 0.6:
                k["front"] += 0.03
        elif dull > 1.0:
            k["spread"] += 0.01; k["lift"] -= 0.01; k["front"] -= 0.02
        # (a whole band spread round you moves the ears' difference far less
        # than one sound does: 0.4 dB of swing is already audible motion)
        if motion < 0.4:
            k["motion"] += 0.06; did.append("more motion")
        elif motion > 5.0:
            k["motion"] -= 0.08; did.append("calming")
        if self.masking > 0.35:
            k["separation"] += 0.1; did.append("unmasking")
        if abs(balance) > 1.5:
            k["turn"] += 0.05 * np.sign(balance); did.append("rebalancing")   # louder left: turn the field right
        for name, d in self.DEFAULTS.items():
            k[name] += (d - k[name]) * 0.02                 # and always a little back toward neutral
            lo, hi = self.LIMITS[name]
            k[name] = float(np.clip(k[name], lo, hi))
        self.doing = ", ".join(did)


class Conductor:
    """The "auto" mode: the music itself moves the sound. No scenes, no rules
    for switching -- every instrument is a body in space, pushed and pulled
    by what it's playing, and the motion emerges from that:

      loudness   -> distance   (swells come close, fades fall back)
      brightness -> height     (bright/high rises, dark/low sinks)
      attacks    -> impulses   (a hit shoves it outward; a spring brings it back)
      harmony    -> direction  (the chord, heard live, sits on the circle of
                                fifths: the bass points at its root, the
                                harmony spreads around it -- a chord change
                                swings the band to a new bearing)
      the beat   -> the drums circle at the tempo, a lap per 8 beats
      the voice  -> in front while singing, swaying with the phrase
      energy     -> temperature: the whole space turns faster when intense

      foresight  -> what's coming (the audio already here but not yet heard:
                    the AI's delay, or the cushion): before a drop everything
                    draws in close and tense, and on it bursts outward; the
                    singer moves to the front a moment before coming in
      spotlight  -> whichever part stands out right now (a solo, the hook, a
                    fill: louder than its usual share) comes forward; the rest
                    make room
      bars       -> beat 1 is found from where the hits fall; big changes (a
                    new learned section, the spotlight) wait for the next bar
                    or phrase, like a DJ -- and a drop is always beat 1

    Bodies repel when they crowd each other; critically damped springs keep
    all of it smooth."""

    FPS = RATE / BLOCK            # 50 updates a second
    NOTE_NAMES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")

    def __init__(self, learner_path=None):
        self.learner_path = learner_path
        self.learner = None            # the unsupervised layer: started on first use
        self.song = None
        self.prev_mag = None
        self.onsets = deque(maxlen=int(8 * self.FPS))
        self.bpm = 110.0
        self.beat = 0.0
        self.frames = 0
        self.e_short = self.e_long = 1e-4
        self.spin = 0.0                         # rotation of the whole space
        self.ring = np.zeros(8192)              # recent mix, for the harmony
        self.chroma = np.ones(12) / 12
        self.root, self.minor = 0, False
        self.harm_angle = 0.0                   # bearing of the harmony (radians)
        n = 9                                   # up to: the band, with the drum kit split
        self.n = 5                              # how many are moving now
        self.pos = np.array([[0, 1.2, 0.1], [0, -1, 0.2], [0.3, 0.6, -0.3], [-1, 0.8, 0.1], [1, 0.8, 0.1],
                             [-0.7, -0.8, 0.3], [0.9, -0.7, 0.2], [-1, -0.3, 0.6], [0.3, -1.2, 1.0]], float)
        self.vel = np.zeros((n, 3))
        self.loud = np.zeros(n)                 # per source, relative to its own recent peak
        self.peak = np.full(n, 1e-6)
        self.bright = np.full(n, 0.5)
        self.bright_avg = np.full(n, np.nan)    # each part's usual brightness (height is relative to it)
        self.prev_src_mag = [None] * n
        self.src_flux_avg = np.full(n, 1e-6)
        self.phrase = 0.0
        self.kit_tight = True        # the split kit stands together (switch: "tight")
        # bars and phrases: where beat 1 falls (accumulated hits by beat in the bar)
        self.bar_hits = np.zeros(4)
        self.downbeat = 0.0                     # beat count where bars start (mod 4)
        self.bar_no = 0                         # bars since the start
        self.new_bar = self.new_phrase = False  # this block starts one
        self.shown_state = 0                    # the learned section on show (switches on phrases)
        # foresight
        self.tension = 0.0                      # 0..1: a drop is coming
        self.drop_in = None                     # seconds to it, if one's coming
        self.burst = 0.0                        # set on the drop: everything flies out
        self.voice_soon = 0.0                   # 0..1: the singer's about to come in
        # spotlight
        self.rms = np.zeros(n)
        self.share_long = None                  # each part's usual share of the sound
        self.share_now = None
        self.spot = None                        # the part in the spotlight ("vocals"...)
        self.spot_since = 0.0
        self.spot_pending = None
        # the engineer (see Engineer): its knobs shape all of this
        self.engineer = Engineer()
        self.bands = np.zeros((n, 24))          # each part's spectrum, coarse, for masking
        self.vocal = 0.0
        # major and minor triad templates for chord recognition
        self.templates = []
        for r in range(12):
            for minor in (False, True):
                t = np.zeros(12)
                t[[r, (r + (3 if minor else 4)) % 12, (r + 7) % 12]] = [1.0, 0.8, 0.9]
                self.templates.append((r, minor, t / np.linalg.norm(t)))

    # ---------------- listening ----------------
    def _tempo(self, mono):
        mag = np.abs(np.fft.rfft(mono * np.hanning(len(mono))))
        flux = 0.0 if self.prev_mag is None else float(np.maximum(mag - self.prev_mag, 0).sum())
        self.prev_mag = mag
        self.onsets.append(flux)
        if self.frames % 25 == 0 and len(self.onsets) > 4 * self.FPS:
            o = np.array(self.onsets)[-int(6 * self.FPS):]
            o = o - o.mean()
            lo, hi = int(self.FPS * 60 / 180), int(self.FPS * 60 / 60)
            ac = np.array([np.dot(o[:-lag], o[lag:]) for lag in range(lo, hi + 1)])
            if ac.max() > 0:
                bpm = 60 * self.FPS / (lo + int(ac.argmax()))
                while bpm < 80:
                    bpm *= 2
                while bpm > 170:
                    bpm /= 2
                self.bpm += (bpm - self.bpm) * 0.3
        self.beat += self.bpm / 60 / self.FPS
        frac = self.beat % 1.0
        recent = np.array(self.onsets)[-int(2 * self.FPS):]
        if len(recent) > 10 and flux > recent.mean() + 2 * recent.std() and (frac < 0.2 or frac > 0.8):
            self.beat += (-frac if frac < 0.5 else 1 - frac) * 0.3
        # beat 1: the beat of the bar the strongest hits keep landing on
        # (low end weighted -- kicks mark the bar), remembered over ~30 s
        low = float(mag[:len(mag) // 12].sum())
        self.bar_hits *= 0.9993
        self.bar_hits[int(round(self.beat)) % 4] += low * (1.0 if frac < 0.15 or frac > 0.85 else 0.0)
        if self.frames % 50 == 0 and self.bar_hits.max() > 0:
            self.downbeat = float(int(self.bar_hits.argmax()))
        bar = int(np.floor((self.beat - self.downbeat) / 4))
        self.new_bar = bar != self.bar_no
        self.new_phrase = self.new_bar and bar % 4 == 0          # a phrase: 4 bars
        self.bar_no = bar

    def _harmony(self, mono):
        self.ring = np.concatenate([self.ring[len(mono):], mono])
        if self.frames % 5:
            return                                   # every 0.1 s
        mag = np.abs(np.fft.rfft(self.ring * np.hanning(len(self.ring))))
        f = np.fft.rfftfreq(len(self.ring), 1 / RATE)
        band = (f > 55) & (f < 2000)
        pc = np.round(12 * np.log2(f[band] / 440.0) + 9).astype(int) % 12   # pitch class, C = 0
        ch = np.bincount(pc, weights=mag[band] ** 2, minlength=12)
        if ch.sum() > 0:
            self.chroma += (ch / ch.sum() - self.chroma) * 0.15                 # ~0.7 s memory
        c = self.chroma / (np.linalg.norm(self.chroma) + 1e-9)
        r, minor, _ = max(self.templates, key=lambda x: float(np.dot(c, x[2])))
        self.root, self.minor = r, minor
        # the chord's place on the circle of fifths -- a minor chord at its relative
        # major's (Am with C): related chords sit close, so a song's usual changes
        # move the band gently, and a real key change swings it round
        home = (r + 3) % 12 if minor else r
        target = ((home * 7) % 12) * TWO_PI / 12
        d = (target - self.harm_angle + np.pi) % TWO_PI - np.pi              # shortest way round
        self.harm_angle += d * 0.08

    def _sources(self, sources):
        for k, s in enumerate(sources):
            rms = float(np.sqrt((s ** 2).mean()))
            self.rms[k] = rms
            self.peak[k] = max(self.peak[k] * 0.9995, rms)                   # ~40 s memory
            self.loud[k] += (min(1.0, rms / self.peak[k]) - self.loud[k]) * 0.1
            mag = np.abs(np.fft.rfft(s))
            if self.frames % 5 == 0:                       # coarse spectrum, ~10x a second
                edges = np.geomspace(1, len(mag) - 1, 25).astype(int)
                b = np.add.reduceat(mag ** 2, edges[:-1])
                self.bands[k] += (b / (b.sum() + 1e-12) - self.bands[k]) * 0.2
            f = np.fft.rfftfreq(len(s), 1 / RATE)
            if mag.sum() > 1e-6:
                cen = float((mag * f).sum() / mag.sum())
                b = np.clip((np.log2(max(cen, 100)) - np.log2(200)) / (np.log2(6000) - np.log2(200)), 0, 1)
                self.bright[k] += (b - self.bright[k]) * 0.05
                if np.isnan(self.bright_avg[k]):
                    self.bright_avg[k] = self.bright[k] = b    # starts from what it first hears
                self.bright_avg[k] += (b - self.bright_avg[k]) * 0.004      # ~5 s
            prev = self.prev_src_mag[k]
            flux = 0.0 if prev is None else float(np.maximum(mag - prev, 0).sum())
            self.prev_src_mag[k] = mag
            self.src_flux_avg[k] += (flux - self.src_flux_avg[k]) * 0.05
            if flux > 3 * self.src_flux_avg[k] and flux > 1e-3:
                # an attack: a shove outward from the listener
                out = self.pos[k] / (np.linalg.norm(self.pos[k]) + 1e-6)
                self.vel[k] += out * min(1.0, flux / (self.src_flux_avg[k] + 1e-9) * 0.1) * self.engineer.active["motion"]
        if len(sources) >= 5:
            v, rest = self.loud[0], self.loud[1:len(sources)].mean() + 1e-6
            self.vocal += ((1.0 if v > 0.3 and v / rest > 0.5 else 0.0) - self.vocal) * 0.03

    def new_song(self, key):
        """A different song: the learned structure starts over."""
        if key != self.song:
            self.song = key
            self.engineer.new_song()
            if self.learner is not None:
                with self.learner.lock:
                    self.learner.reset()

    def update(self, mono, stems=None):
        self.frames += 1
        if self.learner is None and self.learner_path:
            self.learner = SongLearner(self.learner_path)
        if self.learner is not None:
            self.learner.feed(mono)
        self._tempo(mono)
        self._harmony(mono)
        self._sources(stems if stems is not None else [mono])
        e = float((mono ** 2).mean())
        self.e_short += (e - self.e_short) * (1 / (0.5 * self.FPS))
        self.e_long += (e - self.e_long) * (1 / (8 * self.FPS))
        intensity = np.clip(self.e_short / (self.e_long + 1e-9), 0.3, 2.5)
        # the space turns with the energy -- and, once learned, faster or slower and
        # either way round as the song moves along its own third direction
        turn, rate = 1.0, 1.0
        if self.learner is not None and self.learner.embs:
            # a newly learned section takes the stage on the next phrase, not
            # mid-bar (or at once on a drop, see foresee)
            if self.new_phrase:
                self.shown_state = self.learner.state
            f = self.learner.formation(self.n, self.shown_state)
            turn = f["turn"]
            rate = float(np.exp(0.35 * self.learner.comp_s[2]))
        self.spin += turn * rate * intensity * 0.15 * self.engineer.active["motion"] / self.FPS
        self.spin += turn * self.tension * 0.6 / self.FPS      # a riser winds the space up
        if stems is not None and len(stems) > 1:
            self._spotlight(len(stems))
        self.phrase += 1 / (4 * 60 / self.bpm) / self.FPS * TWO_PI           # a 4-beat sway

    # ---------------- what stands out, what's coming ----------------
    GROUPS = {"vocals": (0,), "drums": (1, 5, 6, 7, 8), "bass": (2,), "other": (3, 4)}

    def _spotlight(self, n):
        """Which part stands out: its share of the sound now, against its usual
        share. It must stand out clearly, for a moment, and changes land on a
        bar; the singer singing is spotlit by default."""
        energy = self.rms[:n] ** 2
        groups = {g: [k for k in ks if k < n] for g, ks in self.GROUPS.items()}
        share = np.array([energy[ks].sum() for ks in groups.values()]) / (energy.sum() + 1e-12)
        if self.share_long is None:
            self.share_long, self.share_now = share.copy(), share.copy()
        self.share_now += (share - self.share_now) * 0.05             # ~0.4 s
        self.share_long += (share - self.share_long) * 0.002          # ~10 s
        lift = self.share_now / (self.share_long + 0.03)
        names = list(groups)
        best = names[int(lift.argmax())]
        # it has to clearly stand out: twice its usual share, and a fifth of the sound
        standout = lift.max() > 2.0 and self.share_now[names.index(best)] > 0.2
        want = best if standout else None
        if want != self.spot:
            self.spot_pending = want
            self.spot_since += 1 / self.FPS
        else:
            self.spot_pending, self.spot_since = None, 0.0
        # and keep standing out for 1.5 s; then it switches on the next bar --
        # and whatever has the spotlight keeps it at least 4 s
        self.spot_held = getattr(self, "spot_held", 0.0) + 1 / self.FPS
        if self.spot_pending is not None and self.spot_since > 1.5 and self.spot_held > 4.0 and \
                (self.new_bar or self.spot_since > 3.0):
            self.spot, self.spot_pending, self.spot_since, self.spot_held = self.spot_pending, None, 0.0, 0.0

    def foresee(self, future, past, voice_future=None, voice_now=0.0):
        """What's coming. future: loudness of each 20 ms block not yet heard (the
        next few seconds); past: the last few seconds'."""
        self.voice_soon *= 0.97
        if voice_future is not None and len(voice_future):
            peak = max(float(np.max(voice_future)), voice_now, 1e-6)
            if voice_now < 0.25 * peak and float(np.max(voice_future[:50])) > 0.5 * peak and peak > 0.01:
                self.voice_soon = 1.0                          # in within a second: step forward now
        eta = None
        if len(future) >= 10 and len(past) >= 50:
            k = 15                                             # 0.3 s windows
            fut = np.convolve(future, np.ones(k) / k, mode="valid")
            before = float(np.mean(past[-50:])) + 1e-6         # the last second
            usual = float(np.median(past)) + 1e-6
            # a drop: the sound jumps well past the last second's (+3 dB) and
            # above the usual -- the first window where it does
            jump = np.nonzero((fut > 1.4 * before) & (fut > 1.2 * usual))[0]
            if len(jump):
                # then the exact block inside that window where it jumps
                j = int(jump[0])
                hit = np.nonzero(future[j:j + k] > 1.4 * before)[0]
                eta = float(j + (hit[0] if len(hit) else 0)) * BLOCK / RATE
        self.drop_cool = max(0.0, getattr(self, "drop_cool", 0.0) - 1 / self.FPS)
        if eta is not None and eta < 4.0 and self.drop_cool == 0.0:
            if self.drop_in is None or eta < self.drop_in + 0.5:
                self.drop_in = eta
            # beat 1 is the drop: re-phase the bars so it lands on one
            beats_to = eta * self.bpm / 60
            self.downbeat = float(np.round(self.beat + beats_to)) % 4
        elif self.drop_in is not None:
            self.drop_in -= 1 / self.FPS
            if self.drop_in < -0.1:
                self.drop_in = None
        if self.drop_in is not None:
            target = float(np.clip(1 - self.drop_in / 2.5, 0, 1)) ** 1.5      # builds over the last 2.5 s
            if self.drop_in <= 0.03 and self.tension > 0.3:
                self.burst = 1.0                               # now: everything flies out
                self.drop_in = None
                self.drop_cool = 4.0                           # one drop at a time
                if self.learner is not None and self.learner.embs:
                    self.shown_state = self.learner.state      # and the new section shows at once
                target = 0.0
        else:
            target = 0.0
        self.tension += (target - self.tension) * (0.15 if target > self.tension else 0.3)

    # ---------------- moving ----------------
    def _targets(self, n):
        out = []
        for k in range(n):
            r = float(np.interp(self.loud[k], [0, 1], [2.4, 0.75]))
            # height: brighter than this part usually is -> up, darker -> down,
            # around its own level (bass low, the rest at the ears) -- so the
            # space is used above and below, not just overhead
            usual = self.bright[k] if np.isnan(self.bright_avg[k]) else self.bright_avg[k]
            z = float(np.interp(self.bright[k] - usual, [-0.06, 0.06], [-0.8, 0.8])) + \
                {2: -0.35}.get(k, 0.15)
            # and every part breathes up and down with the bars, each in its own time
            z += 0.45 * np.sin(TWO_PI * (self.beat - self.downbeat) / (8 + 2 * k) + 1.7 * k)
            if n == 1:
                az = self.harm_angle + self.spin + TWO_PI * self.beat / 8
            elif k == 0:   # the voice: front while singing (or about to), swaying; out and around when not
                v = max(self.vocal, self.voice_soon)
                az = 0.35 * np.sin(self.phrase) * v + (1 - v) * (self.spin * 2 + np.pi * 0.7)
                r = r * (1 - 0.3 * v)
            elif k == 1:   # the drums (or the kit's centre) ride the beat
                az = TWO_PI * self.beat / 8 + self.spin
            elif k == 2:   # the bass points at the chord's root, low and close
                az = self.harm_angle + self.spin
                r, z = min(r, 1.1), min(z, -0.2)
            elif k in (3, 4):  # the harmony, spread either side of where the chord sits
                az = self.harm_angle + self.spin + (np.pi / 3 if k == 3 else -np.pi / 3) + np.pi
            elif not self.kit_tight:   # the kit loose: each piece its own way round
                az = self.spin + TWO_PI * self.beat * (1, 1, 1, 1, 1, 1 / 8, -1 / 16, -1 / 8, 1 / 32)[k] + k * 1.3
                if k == 7:
                    r, z = min(r, 1.0), max(z, 0.4)    # the hi-hat close, up by the ear
                if k == 8:
                    z = max(z, 0.9)                    # the cymbals overhead
            else:          # the rest of the kit: placed around the kick, below
                az = 0.0
            if self.learner is not None and self.learner.embs:
                # the learned layer: this state's formation (bearings, spread,
                # height), and the song's first two directions of change opening
                # the space out and lifting it
                f = self.learner.formation(n, self.shown_state)
                c = self.learner.comp_s
                az = az + (f["offsets"][k] * 0.5 if n > 1 else 0)
                r = r * f["spread"] * float(np.exp(0.18 * c[0]))
                z = z + f["lift"] + 0.15 * c[1]
            # what's coming: drawn in close and lifted before a drop
            r *= 1 - 0.4 * self.tension
            # the engineer's settings: how wide, how high, how far toward the front
            kn = self.engineer.active
            r *= kn["spread"]
            z += kn["lift"]
            rel = (az + kn["turn"] + np.pi) % TWO_PI - np.pi
            az = rel * (1 - kn["front"])
            # the spotlight: that part to the front, close; the rest make room
            if self.spot is not None and n > 1:
                group = self.GROUPS[self.spot]
                if k in group and (k == 1 or k not in KIT or n == 5 or not self.kit_tight):
                    side = 0.0 if len([g for g in group if g < n and g not in KIT[1:]]) == 1 else (-0.35 if k == 3 else 0.35)
                    az = side + 0.5 * np.sin(self.phrase)
                    r = min(r, 1.0)
                elif k not in group:
                    rel = (az + np.pi) % TWO_PI - np.pi
                    if abs(rel) < 1.0:                           # out of the way of the front
                        az += np.sign(rel if rel else 1) * (1.0 - abs(rel))
                    r *= 1.2
            out.append(np.array([r * np.sin(az), r * np.cos(az), z]))
        if n == 9 and self.kit_tight:
            # the split kit: one kit, its pieces in their places around the
            # drums' spot (the kick's own loudness and brightness set where)
            center = out[1]
            for k in KIT:
                out[k] = kit_around(center, k)
        return out

    def step(self, n):
        """Advance the bodies one block; returns BLOCK x 3 positions per source."""
        self.n = n
        # how much each pair's spectra overlap (0..1), and how badly that's
        # masking now: overlap weighted by how close they are
        B_ = self.bands[:n]
        norm = np.linalg.norm(B_, axis=1) + 1e-12
        self.overlap = np.clip((B_ @ B_.T) / np.outer(norm, norm), 0, 1)
        np.fill_diagonal(self.overlap, 0)
        if n == 9 and self.kit_tight:
            for a in KIT:
                for b in KIT:
                    self.overlap[a, b] = 0.0                 # the kit stands together anyway
        d_ = np.linalg.norm(self.pos[:n, None] - self.pos[None, :n], axis=2)
        loud = np.sqrt(np.outer(self.loud[:n], self.loud[:n]))
        self.masking_now = float((self.overlap * loud * np.clip(1 - d_ / 1.2, 0, 1)).sum() / max(1, n * (n - 1)) * 4)
        targets = self._targets(n)
        before = self.pos[:n].copy()
        if self.burst > 0:
            # the drop: a shove outward for everyone, all at once
            for k in range(n):
                out = self.pos[k] / (np.linalg.norm(self.pos[k]) + 1e-6)
                self.vel[k] += out * 4.0 * self.burst
            self.burst = 0.0
        for k in range(n):
            # a critically damped spring toward where the music puts it
            acc = 9.0 * (targets[k] - self.pos[k]) - 6.0 * self.vel[k]
            # and some room of its own
            for j in range(n):
                if j != k and not (k in KIT and j in KIT and n == 9 and self.kit_tight):   # (the kit stands together)
                    d = self.pos[k] - self.pos[j]
                    dist = np.linalg.norm(d)
                    # parts that share frequencies need more room (masking)
                    room_ = 0.6 + self.engineer.active["separation"] * (0.3 + self.overlap[k, j])
                    if dist < room_:
                        acc += d / (dist + 1e-6) * (room_ - dist) * 8.0
            self.vel[k] += acc / self.FPS
            speed = np.linalg.norm(self.vel[k])
            if speed > 4.0:                                    # no flinging: 4 m/s at most
                self.vel[k] *= 4.0 / speed
            self.pos[k] += self.vel[k] / self.FPS
            dist = np.linalg.norm(self.pos[k])
            if dist > 4.0:                                     # and never past 4 m
                self.pos[k] *= 4.0 / dist
                self.vel[k] *= 0.5
        f = (np.arange(1, BLOCK + 1) / BLOCK)[:, None]
        return [before[k] + (self.pos[k] - before[k]) * f for k in range(n)]

    def report(self):
        name = self.NOTE_NAMES[self.root] + (" minor" if self.minor else " major")
        intensity = float(np.clip(self.e_short / (self.e_long + 1e-9), 0, 3))
        rep = {"bpm": round(self.bpm), "chord": name, "energy": round(intensity, 2),
               "vocal": round(self.vocal, 2), "spotlight": self.spot,
               "drop_in": None if self.drop_in is None else round(self.drop_in, 1),
               "tension": round(self.tension, 2), "beat": int(np.floor(self.beat - self.downbeat)) % 4 + 1,
               "engineer": {"on": self.engineer.enabled, "doing": self.engineer.doing, "heard": self.engineer.metrics,
                            "knobs": {k: round(v, 2) for k, v in self.engineer.knobs.items()}}}
        if self.learner is not None:
            rep.update(self.learner.report())
            rep["listening"] = self.learner.ready
        return rep


class Predictor:
    """Where a head angle will be `lead` seconds from now: a one-euro filter
    (steady when still, quick when moving) plus its velocity, carried forward
    -- but only as far as it's still going (slowing down: to where it stops),
    and hardly at all for tiny movements, so holding still never jitters.
    Tuned on simulated head turns against the real delays (see the head-
    tracking notes in HeadTracker)."""

    def __init__(self, cap=0.8, gate=0.5, min_cut=1.0, beta=4.0, d_cut=6.0, a_cut=3.0):
        self.cap, self.gate, self.min_cut, self.beta, self.d_cut, self.a_cut = cap, gate, min_cut, beta, d_cut, a_cut
        self.t = None

    @staticmethod
    def _alpha(cut, dt):
        r = TWO_PI * cut * dt
        return r / (r + 1)

    def update(self, t, x):
        if self.t is None:
            self.t, self.x, self.v, self.a = t, x, 0.0, 0.0
            return
        dt = max(1e-3, t - self.t)
        v = self.v + self._alpha(self.d_cut, dt) * ((x - self.x) / dt - self.v)
        self.a += self._alpha(self.a_cut, dt) * ((v - self.v) / dt - self.a)
        self.v = v
        self.x += self._alpha(self.min_cut + self.beta * abs(v), dt) * (x - self.x)
        self.t = t

    def predict(self, now, lead):
        if self.t is None:
            return 0.0
        tau, v, a = now - self.t + lead, self.v, self.a
        if v * a < 0:                       # slowing down: not past where it stops
            te = min(tau, -v / a)
            off = v * te + 0.5 * a * te * te
        else:
            off = v * tau
        off *= abs(v) / (abs(v) + self.gate)
        return self.x + float(np.clip(off, -self.cap, self.cap))


class HeadTracker:
    """Which way your head is turned, from the Mac's camera (dromac-head, a
    Vision face tracker): the 8D field turns the other way, so the sound stays
    put in the room while you look around. On only while it's switched on --
    the camera with it. Losing your face (turned too far, or away from the
    desk) holds the last direction for a moment, then eases back to the front."""

    def __init__(self, binary):
        import threading
        import time
        self.time = time
        self.binary = binary
        self.proc = None
        self.raw = (0.0, 0.0)          # latest reading (yaw, pitch), radians
        self.seen = 0.0                # when a face was last seen
        self.yaw = self.pitch = 0.0    # smoothed, relative to the centre
        self.center = (0.0, 0.0)
        self.recenter_n = None
        # looking up/down, from the face's shape (see dromac_head.swift): the
        # nose's height between eyes and mouth, against your own resting value
        self.shape = None
        self.shape_base = []
        self.lock = threading.Lock()
        self.threading = threading
        self.error = None
        self.dir = os.path.dirname(binary)
        self.pred_yaw, self.pred_pitch = Predictor(), Predictor(cap=0.5)
        self.fed = None                 # the last frame given to the predictors
        self.yaw_v = self.pitch_v = 0.0 # the springs' speeds (see _spring)
        self.out = {"device_ms": 50.0, "bluetooth": True}
        self.out_read = 0.0
        self._low_latency(False)

    def _reader(self, proc):
        for line in proc.stdout:
            parts = line.decode(errors="ignore").split()
            if len(parts) >= 2:
                try:
                    y, p = float(parts[0]), float(parts[1])
                    shape = float(parts[2]) if len(parts) > 2 else -1.0
                    frame_age = float(parts[3]) if len(parts) > 3 else 0.05
                except ValueError:
                    continue
                with self.lock:
                    self.raw = (y, p)
                    self.seen = self.time.time() - frame_age      # when the camera took it
                    if shape >= 0:
                        self.shape = shape
                        if len(self.shape_base) < 45:       # ~1.5 s of your resting face
                            self.shape_base.append(shape)

    def _errors(self, proc):
        for line in proc.stderr:
            line = line.decode(errors="ignore").strip()
            if "not allowed" in line or "no camera" in line:
                self.error = line.replace("dromac-head: ", "")

    def set_on(self, on):
        import subprocess
        if on and self.proc is None and os.path.exists(self.binary):
            self.error = None
            self.proc = subprocess.Popen([self.binary], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
            self.threading.Thread(target=self._errors, args=(self.proc,), daemon=True).start()
            self._low_latency(True)
        elif not on and self.proc is not None:
            self._low_latency(False)
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None
            self.seen = 0.0
            self.turns, self.away = 0, False
            self.shape_base = []

    @property
    def on(self):
        return self.proc is not None and self.proc.poll() is None

    # Between this process and your ears: the player's queue, the socket to
    # it, the device, and -- Bluetooth -- the radio link. While tracking, the
    # queue and socket shrink (nothing is dropped: the slack moves upstream,
    # before the 8D, where it doesn't matter), and the rest is predicted.
    QUEUE = {True: 3, False: 5}               # 20 ms buffers in the player
    SOCK = {True: 2048, False: 8192}          # bytes, each end of the socket
    BLUETOOTH_EXTRA = 0.15                    # the radio's share, beyond what macOS reports

    def _low_latency(self, on):
        self.low = on
        self.queue = self.QUEUE[on]
        try:
            tmp = os.path.join(self.dir, "player_cfg.json.tmp")
            with open(tmp, "w") as f:
                json.dump({"queue": self.queue, "rcvbuf": self.SOCK[on]}, f)
            os.replace(tmp, os.path.join(self.dir, "player_cfg.json"))
            import socket
            sock = socket.socket(fileno=os.dup(1))      # our stdout: the socket to the player
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.SOCK[on])
            sock.close()
        except Exception:
            pass

    def lead(self):
        """Seconds from rendering a block to hearing it."""
        now = self.time.time()
        if now - self.out_read > 2.0:
            self.out_read = now
            try:
                with open(os.path.join(self.dir, "output_latency.json")) as f:
                    self.out = json.load(f)
            except Exception:
                pass
        ahead = self.queue * BLOCK / RATE + 2 * self.SOCK[self.low] / (4 * RATE)
        return ahead + self.out.get("device_ms", 50.0) / 1000 + \
            (self.BLUETOOTH_EXTRA if self.out.get("bluetooth") else 0.0)

    def recenter(self, n):
        """"Recenter": wherever you're facing now becomes the front."""
        if n != self.recenter_n:
            if self.recenter_n is not None:
                with self.lock:
                    self.center = self.raw
                    self.shape_base = []         # and your resting face, afresh
                self.turns, self.yaw = 0, 0.0
            self.recenter_n = n

    # The camera only sees your face up to ~70 degrees either way. Turning
    # further -- all the way round, on a chair -- is followed through the part
    # it can't see: the turn carries on at the speed it was going, and when
    # your face comes back (from either side) the count of whole turns is
    # worked out from where it reappears. So the field follows a full 360.
    EDGE = 0.55          # radians: lost past here, you turned away (not left)
    SHAPE_GAIN = 5.0     # radians of pitch per unit change of the nose's height

    # The angle you hear follows the prediction on a critically damped spring:
    # it can't make a sharp corner (the camera's frame-by-frame steps, a sudden
    # stop) -- the motion stays smooth -- and its lag is part of the lead.
    SPRING = 25.0                        # rad/s
    SPRING_LAG = 2 / SPRING              # ~80 ms

    def _spring(self, x, v, target):
        h = BLOCK / RATE / 4
        for _ in range(4):
            v += h * (self.SPRING ** 2 * (target - x) - 2 * self.SPRING * v)
            x += h * v
        return x, v

    def step(self):
        """Advance one block; returns this block's (yaw, pitch) ramps."""
        y0, p0 = self.yaw, self.pitch
        now = self.time.time()
        if self.on:
            with self.lock:
                (ry, rp), seen = self.raw, self.seen
            if not hasattr(self, "hist"):
                self.hist, self.turns, self.away, self.est = deque(maxlen=15), 0, False, 0.0
            ry -= self.center[0]
            age = now - seen
            if age < 0.25:
                if self.away:
                    # back in view: the whole turns that put the reading nearest
                    # to where the estimate had got to
                    self.turns = round((self.est - ry) / TWO_PI)
                    self.away = False
                total = ry + self.turns * TWO_PI
                if not self.hist or self.hist[-1][0] != seen:
                    self.hist.append((seen, total))
                if self.fed != seen:
                    self.pred_yaw.update(seen, total)
                # aimed at where your head will be when this block reaches you
                # (and the spring's own lag, which it then makes up)
                ty, k = self.pred_yaw.predict(now, self.lead() + self.SPRING_LAG), None
            else:
                last = self.hist[-1][1] if self.hist else 0.0
                edge = abs(last - self.turns * TWO_PI) > self.EDGE
                if edge and not self.away:
                    # turned out of view: keep turning the way you were going
                    self.away = True
                    t0, a0 = self.hist[0]
                    v = (last - a0) / max(1e-3, self.hist[-1][0] - t0)
                    v = np.sign(last - self.turns * TWO_PI) * max(1.2, min(4.0, abs(v)))
                    self.away_v, self.est = v, last
                self.pred_yaw.t = None            # (starts afresh when you're back)
                if self.away:
                    # carry on at that speed until you're facing straight back --
                    # the likeliest place to be while the camera can't see you
                    # (keep turning and your face reappears on the other side,
                    # which corrects it)
                    base = self.turns * TWO_PI
                    self.est += self.away_v * BLOCK / RATE
                    lo, hi = sorted((base + np.sign(self.away_v) * self.EDGE,
                                     base + np.sign(self.away_v) * np.pi))
                    self.est = float(np.clip(self.est, lo, hi))
                    ty, k = self.est, 0.3
                elif age > 3.0:
                    ty, k = self.turns * TWO_PI, 0.02    # nobody there: back to the front
                else:
                    ty, k = self.yaw, 0.0                # a blink of lost tracking: hold
            with self.lock:
                shape, base = self.shape, list(self.shape_base)
            if age < 0.25 and shape is not None and len(base) >= 15:
                # nose up toward the eyes = looking up
                tp = float(np.clip((float(np.median(base)) - shape) * self.SHAPE_GAIN, -1.0, 1.0))
                if self.fed != seen:
                    self.pred_pitch.update(seen, tp)
                tp = float(np.clip(self.pred_pitch.predict(now, self.lead() + self.SPRING_LAG), -1.0, 1.0))
            else:
                tp = 0.0
                self.pred_pitch.t = None
            self.fed = seen
            if k is None:
                self.yaw, self.yaw_v = self._spring(self.yaw, self.yaw_v, ty)
            else:
                self.yaw += (ty - self.yaw) * k
                self.yaw_v = 0.0
            if age < 0.25:
                self.pitch, self.pitch_v = self._spring(self.pitch, self.pitch_v, tp)
            else:
                self.pitch += (tp - self.pitch) * 0.03
                self.pitch_v = 0.0
        else:
            self.yaw *= 0.9                 # switched off: ease back to the front
            self.pitch *= 0.9
        f = np.arange(1, BLOCK + 1) / BLOCK
        return y0 + (self.yaw - y0) * f, p0 + (self.pitch - p0) * f


def turn_with_head(pos, yaw, pitch):
    """The world as your turned head hears it: positions (BLOCK x 3, x right,
    y front, z up) rotated by -yaw around the vertical and -pitch around the
    ears' axis."""
    c, s = np.cos(yaw), np.sin(yaw)
    x = pos[:, 0] * c + pos[:, 1] * s           # head turned left: front swings to the right
    y = -pos[:, 0] * s + pos[:, 1] * c
    cp, sp = np.cos(pitch), np.sin(pitch)
    z = pos[:, 2]
    return np.stack([x, y * cp + z * sp, -y * sp + z * cp], axis=1)


class Weather:
    """Rain and thunder, made live -- never a loop, so it never repeats.

    Rain is what it physically is: thousands of drops a second, each a tiny
    impact click and, often, the "plink" of the air bubble it traps -- a
    rising chirp whose pitch is set by the bubble's size (Minnaert; the model
    of van den Doel, "Physically based models for liquid sounds", 2005) --
    each somewhere around you, over a bed of hiss (independent in each ear, so
    it surrounds you) that swells with the gusts. Thunder is real: claps and
    rolls from field recordings, varied every time (see the thunder section).

    Presets set the mix; volume is 0-100."""

    PRESETS = {
        #             drops/s  bed   bright  gusts  bubbles  thunder every (s)   distance (km)  surface
        "drizzle":   (250,    0.10, 1.0,    0.15,  0.35,    None,               None,          "open"),
        "steady":    (1200,   0.22, 1.0,    0.25,  0.30,    None,               None,          "open"),
        "downpour":  (3500,   0.45, 1.1,    0.45,  0.25,    None,               None,          "open"),
        "thunderstorm": (2000, 0.32, 1.0,   0.40,  0.28,    (18, 55),           (0.6, 6.0),    "open"),
        "distant storm": (900, 0.18, 0.9,   0.25,  0.30,    (25, 80),           (4.0, 12.0),   "open"),
        "tin roof":  (900,    0.12, 1.0,    0.25,  0.10,    (60, 150),          (3.0, 10.0),   "tin"),
        "window":    (500,    0.10, 0.45,   0.20,  0.20,    (45, 120),          (3.0, 10.0),   "glass"),
        "forest":    (1500,   0.25, 0.85,   0.30,  0.30,    (70, 180),          (5.0, 12.0),   "leaves"),
    }

    def __init__(self):
        import threading
        self.rng = np.random.default_rng()
        self.preset = "steady"
        self.gain = 0.0                         # follows the volume, smoothly (no clicks on/off)
        self.bed = np.zeros((2, 0))             # hiss, made in overlapping chunks
        self.bed_tail = np.zeros((2, 0))
        self.drops = np.zeros((2, BLOCK))       # drops still ringing past this block
        self.gust = 0.0
        self.gust_v = 0.0
        self.claps = []                          # strikes on their way or sounding (up to two at once)
        self.activity = 0.0                      # the storm's mood, drifting over minutes
        self.burst_left = 0.0                    # seconds left of a cell passing overhead
        self.yaw = 0.0                          # where you're facing (head tracking), radians
        self.flash = 0.0                        # when lightning last struck (for the dashboard's flash)
        self.thunder_gain = 1.0                 # the "thunder" control, against the rain
        self.next_thunder = None                # seconds of rain until it
        self.lock = threading.Lock()
        self.real = RealRain()                  # real rain (when its bank is there)
        self.threading = threading

    # ---------- the hiss ----------
    def _bed_chunk(self, bright):
        """0.34 s of hiss, shaped in the frequency domain: no high end below
        ~300 Hz, a soft peak in the presence range, fading above it."""
        n = 16384
        f = np.fft.rfftfreq(n, 1 / RATE)
        fc = 7000 * bright
        mag = (f / (f + 300)) ** 2 / np.sqrt(1 + (f / fc) ** 4)
        out = np.empty((2, n))
        for ch in range(2):                     # independent ears: it's all around you
            phase = self.rng.uniform(0, TWO_PI, len(f))
            out[ch] = np.fft.irfft(mag * np.exp(1j * phase), n)
        # a sine window: its squares add to exactly one at 50% overlap, so the
        # blend of two unrelated chunks keeps a steady loudness (a Hann window
        # dips 3 dB at every crossover -- a tremolo at ~6 Hz)
        return out / (np.sqrt((out ** 2).mean()) + 1e-12) * np.sin(np.pi * (np.arange(n) + 0.5) / n)

    def _bed_block(self, bright):
        hop = 8192
        while self.bed.shape[1] < BLOCK:
            chunk = self._bed_chunk(bright)
            tail = self.bed_tail if self.bed_tail.shape[1] else np.zeros((2, hop))
            self.bed = np.concatenate([self.bed, chunk[:, :hop] + tail], axis=1)
            self.bed_tail = chunk[:, hop:]
        out, self.bed = self.bed[:, :BLOCK], self.bed[:, BLOCK:]
        return out

    # ---------- the drops ----------
    # Rain as it really falls: sizes from the Marshall-Palmer distribution
    # (lots of tiny drops, the odd big one -- it shifts larger as the rain
    # gets heavier); each drop somewhere in the space around you, near or far
    # (far ones fainter and darker, more of them -- there's more ground out
    # there), reaching one ear a moment before the other. Most just splat --
    # a short burst of noise, duller and longer for a bigger drop; on water,
    # many also trap a bubble that rings ("plink"). A few spots nearby keep
    # their own sound (a puddle, a leaf, a stone), and drips -- a gutter, a
    # branch -- fall in a loose rhythm of their own.
    MAX_DROPS = 90                       # per 20 ms block
    # real rain's level by preset (its pieces are all levelled to -24 dBFS)
    REAL_LEVEL = {"drizzle": 0.45, "steady": 0.8, "downpour": 1.4, "thunderstorm": 1.15, "distant storm": 0.75,
                  "tin roof": 0.55, "window": 0.7, "forest": 0.95}
    # how often (s, on average) a cell passes overhead: busy spells of thunder
    BURST_EVERY = {"thunderstorm": 240, "window": 300, "downpour": 420, "tin roof": 480, "distant storm": 900}
    RAIN_MM_H = {"drizzle": 1.0, "steady": 6.0, "downpour": 35.0, "thunderstorm": 18.0,
                 "distant storm": 5.0, "tin roof": 8.0, "window": 6.0, "forest": 8.0}
    SPOTS = {"open": ("puddle", "stone", "puddle"), "forest": ("leaf", "leaf", "puddle"),
             "tin": ("stone",), "glass": ()}
    DRIPS = {"open": 1, "leaves": 3, "tin": 2, "glass": 1}

    def _places(self, surface):
        """The fixed spots and drips around you for this surface (made once)."""
        key = surface
        if getattr(self, "_places_for", None) == key:
            return
        self._places_for = key
        kinds = self.SPOTS.get("forest" if surface == "leaves" else surface, ("puddle",))
        self.spots = [(k, self.rng.uniform(-np.pi, np.pi), self.rng.uniform(0.8, 2.5)) for k in kinds]
        self.drippers = [{"next": self.rng.uniform(0, 1.5), "every": self.rng.uniform(0.35, 1.6),
                          "pitch": self.rng.uniform(450, 1100), "az": self.rng.uniform(-np.pi, np.pi),
                          "d": self.rng.uniform(1.0, 3.0)} for _ in range(self.DRIPS.get(surface, 1))]

    _env_cache = {}

    @classmethod
    def _envelope(cls, L):
        if L not in cls._env_cache:
            e = np.ones(L)
            a = int(0.0002 * RATE)
            e[:a] = np.linspace(0, 1, a, endpoint=False)
            q = L // 4
            e[-q:] = 0.5 * (1 + np.cos(np.linspace(0, np.pi, q)))
            cls._env_cache[L] = e
        return cls._env_cache[L]

    @staticmethod
    def _smooth(waves, k):
        """Darkens each row: a k-sample moving average (vectorised)."""
        if k <= 1:
            return waves
        cs = np.cumsum(np.pad(waves, ((0, 0), (k, 0))), axis=1)
        return (cs[:, k:] - cs[:, :-k]) / k

    def _drops_block(self, rate, bubbles, surface, bright, wind):
        self._places(surface)
        L = {"tin": 3840}.get(surface, 1440)
        buf = np.zeros((2, BLOCK + L + 64))      # (room for the onset offset and the ears' delay)
        carry = self.drops
        buf[:, :carry.shape[1]] += carry[:, : buf.shape[1]]
        mm_h = self.RAIN_MM_H.get(self.preset, 6.0)
        n = int(self.rng.poisson(rate * BLOCK / RATE))
        # past ~4500 a second you can't tell drops apart: the rest is the wash
        # (the bed), and the time to make a block stays small
        self.overflow = max(0, n - self.MAX_DROPS) / self.MAX_DROPS
        n = min(n, self.MAX_DROPS)
        if n == 0 and not any(d["next"] <= BLOCK / RATE for d in self.drippers):
            for d in self.drippers:
                d["next"] -= BLOCK / RATE
            self.drops = buf[:, BLOCK:]
            return buf[:, :BLOCK]
        t = np.arange(L) / RATE
        # sizes (diameter, mm): Marshall-Palmer, Lambda = 4.1 R^-0.21
        D = np.clip(self.rng.exponential(1 / (4.1 * mm_h ** -0.21), n), 0.3, 5.5)
        # where: around you, more of them far off (uniform over the ground),
        # the wind leaning them a little to one side
        dist = 1.2 + 10.5 * np.sqrt(self.rng.random(n))       # (only the spots come closer)
        az = self.rng.uniform(-np.pi, np.pi, n) + 0.5 * wind
        kind = np.zeros(n, int)                          # 0 open ground, 1 puddle, 2 leaf, 3 stone
        if self.spots and n:
            on_spot = self.rng.random(n) < 0.15          # some fall on the spots near you
            which = self.rng.integers(0, len(self.spots), n)
            for i in np.nonzero(on_spot)[0]:
                k, a, d = self.spots[which[i]]
                kind[i] = {"puddle": 1, "leaf": 2, "stone": 3}[k]
                az[i], dist[i] = a + self.rng.normal(0, 0.08), d + self.rng.normal(0, 0.1)
        # drips: on their own loose clocks
        extra = []
        for d in self.drippers:
            d["next"] -= BLOCK / RATE
            if d["next"] <= 0:
                extra.append(d)
                d["next"] += d["every"] * self.rng.uniform(0.6, 1.5)
        m = n + len(extra)
        if m == 0:
            self.drops = buf[:, BLOCK:]
            return buf[:, :BLOCK]
        D = np.concatenate([D, np.full(len(extra), 4.0)])
        dist = np.concatenate([dist, [d["d"] for d in extra]])
        az = np.concatenate([az, [d["az"] for d in extra]])
        kind = np.concatenate([kind, np.full(len(extra), 4)])     # 4: a drip
        # the splat: noise, longer and duller for bigger drops
        tau = (0.25 + 0.9 * D / 2) / 1000
        noise = self.rng.standard_normal((m, L)) * np.exp(-t[None, :] / tau[:, None])
        waves = np.zeros((m, L))
        for k_dark, sel in ((1, D < 1.0), (3, (D >= 1.0) & (D < 2.2)), (7, D >= 2.2)):
            if sel.any():
                waves[sel] = self._smooth(noise[sel], k_dark)
        if (kind == 2).any():                                            # leaves: a soft papery pat
            leaf = kind == 2
            pat = self._smooth(self.rng.standard_normal((int(leaf.sum()), L)), 5)
            waves[leaf] = 0.5 * pat * np.exp(-t / 0.004)
        # the bubble: often on water (puddles, drips), rarely on the ground
        p_bub = np.select([kind == 1, kind == 4, kind == 2], [0.75, 1.0, 0.05], bubbles * 0.35)
        has = self.rng.random(m) < p_bub
        rb = np.exp(self.rng.uniform(np.log(0.15e-3), np.log(1.1e-3), m))   # bubble radius (m)
        if extra:                                        # a drip's bubble: its own pitch
            rb[n:] = 3.26 / np.array([d["pitch"] * self.rng.uniform(0.95, 1.05) for d in extra])
        f0 = np.minimum(3.26 / rb, 16000)
        dec = 0.13 * f0 + 0.0072 * f0 ** 1.5
        sig = 0.1 * dec * self.rng.uniform(0.5, 1.5, m)
        bub = np.sin(TWO_PI * (f0[:, None] * t + 0.5 * f0[:, None] * sig[:, None] * t ** 2)) * np.exp(-dec[:, None] * t)
        waves += np.where(has[:, None], bub * np.where(kind == 4, 0.6, 0.7)[:, None], 0.0)
        if surface == "tin":
            modes = self.rng.uniform(380, 2600, (m, 4))
            decay = self.rng.uniform(25, 60, (m, 4))
            waves += 0.25 * (np.sin(TWO_PI * modes[:, :, None] * t) * np.exp(-decay[:, :, None] * t)).sum(1)
        if surface == "glass":
            waves = self._smooth(waves, 9)
        # loudness: bigger drops hit harder; further ones fainter -- and darker
        amp = (D / 2) ** 1.2 / dist * self.rng.uniform(0.5, 1.0, m)
        amp = np.where(kind == 4, amp * 0.8, amp)
        far = dist > 5
        if far.any():
            waves[far] = self._smooth(waves[far], 4)
        waves *= amp[:, None] * bright
        # every sound fades in over a fraction of a millisecond and out over
        # the last quarter of its window -- cut off mid-ring, a drip or the tin
        # roof's ping would end in a click: a pop no rain makes
        waves *= self._envelope(L)
        # where you're facing (head tracking): the rain stays put as you turn
        az = az - self.yaw
        behind = np.cos(az) < 0
        if behind.any():                                 # the ears shade sound from behind:
            waves[behind] = self._smooth(waves[behind], 3) * 0.8   # a little duller and softer
        # each ear: level (the head shades the far side) and time (up to 0.6 ms)
        side = np.sin(az)
        gl, gr = np.sqrt(0.5 * (1 - 0.8 * side)), np.sqrt(0.5 * (1 + 0.8 * side))
        itd = np.round(side * 0.0006 * RATE).astype(int)
        onset = self.rng.integers(0, BLOCK, m) + 20
        # all the drops into the block at once (one sum, not one by one)
        span = np.arange(L)
        size = buf.shape[1]
        for ch, start, g in ((0, onset + np.maximum(itd, 0), gl), (1, onset + np.maximum(-itd, 0), gr)):
            idx = (start[:, None] + span).ravel()
            buf[ch] += np.bincount(idx, weights=(waves * g[:, None]).ravel(), minlength=size)[:size]
        self.drops = buf[:, BLOCK:]
        return buf[:, :BLOCK]

    def _weather_moods(self):
        """How hard it's raining just now, and where the wind's from: slow
        random swells on several timescales at once (seconds, half a minute,
        a minute or more), as real rain comes and goes in waves."""
        if not hasattr(self, "moods"):
            self.moods = np.zeros(3)
            self.wind = 0.0
        dt = BLOCK / RATE
        taus = np.array([4.0, 25.0, 80.0])
        self.moods += -self.moods * dt / taus + self.rng.standard_normal(3) * np.sqrt(2 * dt / taus)
        self.wind += -self.wind * dt / 40 + self.rng.standard_normal() * np.sqrt(2 * dt / 40) * 0.8
        swell = float(np.exp(0.22 * self.moods[0] + 0.3 * self.moods[1] + 0.35 * self.moods[2]))
        return swell, float(np.clip(self.wind, -1.5, 1.5))

    # ---------- thunder ----------
    # Real thunder: 40 claps and rolls cut from field recordings (models/
    # thunder, built by tools/make_thunder_pool.py; credits there), never
    # played the same way twice -- a different clap from the last few, a
    # little faster or slower (bigger or smaller sounding), darker and softer
    # the further off the strike is meant to be (the air takes the highs),
    # from a direction of its own that stays put as you turn your head. The
    # flash comes first; the sound ~3 s per kilometre later.
    _pool_cache = None

    @classmethod
    def _pool(cls):
        if cls._pool_cache is None:
            folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "thunder")
            try:
                z = np.load(os.path.join(folder, "pool.npz"))
                meta = json.load(open(os.path.join(folder, "pool.json")))
                cls._pool_cache = [(z[f"c{k}"], meta[k]) for k in range(len(meta))]
            except Exception:
                cls._pool_cache = []
        return cls._pool_cache

    def _make_thunder(self, km, clap):
        """One strike at `km`, from the pool, into `clap` (in the background)."""
        try:
            pool = self._pool()
            if not pool:
                clap["high"] = np.zeros(1)                   # nothing to play
                clap.update(low=np.zeros(1), echo=np.zeros((2, 1)), az=0.0, sweep=0.0)
                return
            rng = np.random.default_rng()
            # a close strike wants a sharp clap, a far one a dull roll: rank the
            # pool by fit, pick among the better half, not one of the last few
            recent = getattr(self, "recent_claps", [])
            want = 1600 / (1 + km)                                   # Hz: the start's brightness
            fit = sorted((abs(np.log((m["sharp_hz"] + 50) / (want + 50))), k) for k, (_, m) in enumerate(pool) if k not in recent)
            k = fit[rng.integers(0, max(1, len(fit) // 2))][1]
            self.recent_claps = (recent + [k])[-6:]
            clip = pool[k][0].astype(np.float64)
            # a little faster or slower: bigger or smaller sounding
            speed = rng.uniform(0.88, 1.1)
            n = int(clip.shape[1] / speed)
            pos = np.arange(n) * speed
            clip = np.stack([np.interp(pos, np.arange(clip.shape[1]), ch) for ch in clip])
            # the further off, the duller (a moving average: a gentle low-pass)
            dull = 1 + int(km * 1.6)
            if dull > 1:
                kern = np.ones(dull) / dull
                clip = np.stack([np.convolve(ch, kern, "same") for ch in clip])
            fade = int(0.3 * RATE)
            clip[:, -fade:] *= np.linspace(1, 0, fade)
            # level by distance -- its own scale, not the rain's: at its
            # loudest moment a close strike (0.7 km) reaches about the level
            # of loud music (-6 dBFS), 2 km -12, 5 km -17, 12 km -22: a rumble
            # under the music. (The rain's volume only nudges it -- see render.)
            fr = clip.mean(0)[: n // 4800 * 4800].reshape(-1, 4800)
            target = 10 ** ((-6.0 - 13.0 * np.log10(km / 0.7)) / 20)
            clip *= target / (float(np.sqrt((fr ** 2).mean(1)).max()) + 1e-12)
            mid = clip.mean(0)
            side = (clip[0] - clip[1]) / 2                             # the recording's own space
            # below ~150 Hz the head shades nothing: the deep end comes into
            # both ears alike (and is felt more than placed); above it, the
            # clap has a direction
            f = np.fft.rfftfreq(len(mid), 1 / RATE)
            M = np.fft.rfft(mid)
            split = 1 / np.sqrt(1 + (f / 150) ** 4)
            low = np.fft.irfft(M * split, len(mid))
            high = mid - low
            low *= 10 ** (7 / 20)                     # the deep end you feel: +7 dB
            # the sky's echo, from all round: two different (so it's wide),
            # darker tails
            m2 = 1 << int(np.ceil(np.log2(len(mid) + 3 * RATE)))
            sky = []
            for _ in range(2):
                ir = rng.standard_normal(3 * RATE) * np.exp(-np.arange(3 * RATE) / (1.1 * RATE))
                ir = np.convolve(ir, np.ones(16) / 16, "same")
                sky.append(np.fft.irfft(np.fft.rfft(mid, m2) * np.fft.rfft(ir, m2), m2)[:len(mid)])
            sky = np.array(sky)
            sky *= 0.3 * np.abs(mid).max() / (np.abs(sky).max() + 1e-12)
            echo = np.stack([side, -side]) + sky
            # the roll travels across the sky as it goes
            az0 = rng.uniform(-np.pi, np.pi)
            sweep = rng.choice([-1, 1]) * rng.uniform(0.4, 1.3)
            clap.update(low=low, echo=echo, az=az0, sweep=sweep)
            clap["high"] = high                              # (last: it's what says "ready")
        except Exception:
            clap["high"] = np.zeros(1)
            clap.update(low=np.zeros(1), echo=np.zeros((2, 1)), az=0.0, sweep=0.0)

    # When it strikes. Real storms aren't a metronome: their activity drifts
    # over minutes, and every few minutes a cell passes overhead -- a busy
    # spell of a minute or two with strikes every few seconds, closer, rolls
    # overlapping -- then it eases off again. Gaps are drawn from a gamma
    # distribution (uneven, like real lightning, never closer than 2.5 s).
    # Each strike is made the moment the lightning flashes, at its own
    # distance; the sound's travel time hides the making.
    def _strike_gap(self, every):
        mean = float(np.sqrt(every[0] * every[1]))
        if self.burst_left > 0:
            mean = self.rng.uniform(5, 9)                      # a cell overhead
        else:
            mean /= float(np.exp(0.5 * self.activity))
        return max(2.5, float(self.rng.gamma(2.2, mean / 2.2)))

    def _thunder_block(self, every, km_range):
        out = np.zeros((2, BLOCK))
        dt = BLOCK / RATE
        # play what's sounding (up to two rolls at once)
        still = []
        for clap in self.claps:
            if clap.get("high") is None:                    # still being made: wait
                still.append(clap)
                continue
            at = clap["at"]
            if at < 0:                                       # the flash has been; the sound's on its way
                clap["at"] = at + BLOCK
                still.append(clap)
                continue
            n = len(clap["high"])
            high = clap["high"][at:at + BLOCK]
            k = len(high)
            # its direction now (the roll moves), as heard from where you face
            az = clap["az"] + clap["sweep"] * (at / n) - self.yaw
            side = np.sin(az)
            gl, gr = np.sqrt(0.5 * (1 - 0.8 * side)), np.sqrt(0.5 * (1 + 0.8 * side))
            low = clap["low"][at:at + BLOCK] * np.sqrt(0.5)
            out[:, :k] += clap["echo"][:, at:at + BLOCK] + np.stack([high * gl + low, high * gr + low])
            clap["at"] = at + BLOCK
            if at + BLOCK < n:
                still.append(clap)
        self.claps = still
        if every is None:
            self.next_thunder = None
            self.burst_left = 0.0
            return out
        # the storm's mood: activity drifting over minutes, and the busy spells
        self.activity += -self.activity * dt / 120 + self.rng.standard_normal() * np.sqrt(2 * dt / 120)
        if self.burst_left > 0:
            self.burst_left -= dt
        elif self.rng.random() < dt / self.BURST_EVERY.get(self.preset, 300):
            self.burst_left = self.rng.uniform(60, 150)
        if self.next_thunder is None:
            self.next_thunder = self.rng.uniform(4, every[0] * 0.5)          # the first one soon
        self.next_thunder -= dt
        if self.next_thunder <= 0 and len(self.claps) < 2:
            # closer while a cell is overhead
            lo, hi = km_range
            if self.burst_left > 0:
                hi = float(np.sqrt(lo * hi))
            km = float(np.exp(self.rng.uniform(np.log(lo), np.log(hi))))
            clap = {"at": -int(min(12.0, km * 1000 / 343 * self.rng.uniform(0.9, 1.1)) * RATE), "high": None}
            self.claps.append(clap)
            self.flash = __import__("time").time()               # lightning: now
            self.threading.Thread(target=self._make_thunder, args=(km, clap), daemon=True).start()
            self.next_thunder = self._strike_gap(every)
        return out

    def render(self, preset, volume, yaw=0.0, thunder_db=0.0):
        """One block of weather (stereo), at `volume` (0-100); `yaw`: where
        you're facing (head tracking), so it all stays put as you turn;
        `thunder_db`: thunder against the rain (-12..+12)."""
        self.yaw = yaw
        self.thunder_gain = 10 ** (max(-12.0, min(12.0, thunder_db)) / 20)
        if hasattr(self, "cycle"):
            for e in self.cycle.engines:
                e.yaw, e.thunder_gain = yaw, self.thunder_gain
        target = (max(0.0, min(100.0, volume)) / 100) ** 2 * 0.6
        if target == 0 and self.gain < 1e-4:
            self.gain = 0.0
            return None
        if preset == "storm cycle":
            if not hasattr(self, "cycle"):
                self.cycle = StormCycle()
            rain, thunder = self.cycle.core()
        else:
            rain, thunder = self.core(preset)
        ramp = self.gain + (target - self.gain) * np.clip(np.arange(1, BLOCK + 1) / (0.5 * RATE), 0, 1)
        self.gain = float(ramp[-1])
        # thunder on its own scale: the rain's volume only nudges it (14% ->
        # -4 dB), the thunder control sets it
        on = np.clip(ramp / 0.6, 0, 1) ** 0.25
        thunder = thunder * on * self.thunder_gain
        # (the rain stays as it is: thunder plays over it, louder)
        return rain * ramp + thunder

    def core(self, preset, thunder=None):
        """One block of a preset, before the volume (`thunder`: (every, km) instead
        of the preset's own)."""
        if preset != self.preset and preset in self.PRESETS:
            self.preset = preset
            self.next_thunder = None
        rate, bed_level, bright, gusts, bubbles, every, km, surface = self.PRESETS[self.preset]
        if thunder is not None:
            every, km = thunder                         # (the storm cycle sets its own)
        # how hard it's raining just now, and the wind
        swell, wind = self._weather_moods()
        swell = 1 + (swell - 1) * (0.5 + gusts)
        real = self.real.render(self.preset)
        if real is not None:
            # real rain carries the sound (its own swells, gently followed);
            # the made drops stay on top, quieter, for the close-up 3D detail
            bed = real * self.REAL_LEVEL.get(self.preset, 1.0) * swell ** 0.5
        else:
            bed = self._bed_block(bright) * bed_level * swell
        # the wind leans the wash to one side (as heard from where you face)
        lean = 0.18 * np.tanh(wind - self.yaw)
        bed = bed * np.array([[1 - lean], [1 + lean]])
        if surface == "glass":
            bed *= 0.6
        drops = self._drops_block(rate * swell, bubbles, surface, bright, wind) * (0.18 if real is not None else 0.35)
        if getattr(self, "overflow", 0) > 0:
            bed = bed * (1 + 0.5 * self.overflow)        # the drops past the cap, as wash
        return bed + drops, self._thunder_block(every, km)


class RealRain:
    """Real rain, from field recordings (models/rain, built by
    tools/make_rain_bank.py; credits there), that never loops: pieces of the
    right kind for the preset (light, heavy, on a window, in a forest), each
    blending into another every 20-60 s over 10 s -- and a second one under
    it, mirrored left for right, so it's denser and wider than any one
    recording. The pieces are levelled by how loud they sound (K-weighted),
    and a slow leveller (+-4 dB, over seconds) smooths what's left between
    them, leaving the rain's own swells."""

    KINDS = {"drizzle": ("light",), "steady": ("light", "heavy"), "downpour": ("heavy",),
             "thunderstorm": ("heavy",), "distant storm": ("light", "heavy"), "tin roof": ("light",),
             "window": ("window",), "forest": ("forest",)}
    BLEND = 10.0
    _bank = None

    @classmethod
    def bank(cls):
        if cls._bank is None:
            folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "rain")
            try:
                z = np.load(os.path.join(folder, "bank.npz"))
                meta = json.load(open(os.path.join(folder, "bank.json")))
                cls._bank = [(z[f"p{k}"], m["kind"]) for k, m in enumerate(meta)]
            except Exception:
                cls._bank = []
        return cls._bank

    def __init__(self):
        self.rng = np.random.default_rng()
        self.streams = [{"now": None, "old": None}, {"now": None, "old": None}]

    def _pick(self, kinds, avoid):
        bank = self.bank()
        fits = [k for k, (_, kind) in enumerate(bank) if kind in kinds and k not in avoid]
        if not fits:
            fits = [k for k in range(len(bank)) if k not in avoid] or list(range(len(bank)))
        return int(self.rng.choice(fits))

    def _start(self, stream, kinds, avoid):
        k = self._pick(kinds, avoid)
        piece = self.bank()[k][0]
        span = piece.shape[1]
        blend = int(self.BLEND * RATE)
        # somewhere into it, and its turn over well before its end: it always
        # leaves with a blend, never a cut back to its own start
        at = int(self.rng.integers(0, max(1, span - 2 * blend - 15 * RATE)))
        left = min(self.rng.uniform(20, 60) * RATE, span - at - 2 * blend)
        stream["old"] = stream["now"]
        stream["now"] = {"k": k, "at": at, "left": max(left, blend)}
        stream["blend"] = 0 if stream["old"] is not None else int(self.BLEND * RATE)

    def _read(self, s):
        piece = self.bank()[s["k"]][0]
        a = s["at"] % piece.shape[1]
        out = piece[:, a:a + BLOCK].astype(np.float64)
        if out.shape[1] < BLOCK:                                       # off the end: round to the start
            out = np.concatenate([out, piece[:, : BLOCK - out.shape[1]].astype(np.float64)], axis=1)
        s["at"] += BLOCK
        s["left"] -= BLOCK
        return out

    def render(self, preset):
        if not self.bank():
            return None
        kinds = self.KINDS.get(preset, ("light", "heavy"))
        out = np.zeros((2, BLOCK))
        for i, st in enumerate(self.streams):
            other = self.streams[1 - i]["now"]
            avoid = {st["now"]["k"]} if st["now"] else set()
            if other:
                avoid.add(other["k"])
            if st["now"] is None or st["now"]["left"] <= 0 or self.bank()[st["now"]["k"]][1] not in kinds:
                self._start(st, kinds, avoid)
            x = self._read(st["now"])
            blend = int(self.BLEND * RATE)
            if st["old"] is not None and st.get("blend", blend) < blend:
                a = np.clip((st["blend"] + np.arange(1, BLOCK + 1)) / blend, 0, 1)
                x = x * np.sin(a * np.pi / 2) + self._read(st["old"]) * np.cos(a * np.pi / 2)
                st["blend"] += BLOCK
                if st["blend"] >= blend:
                    st["old"] = None
            if i == 1:
                x = x[::-1] * 0.55                       # the second, under it: mirrored, quieter
            out += x
        # the slow leveller: what's left of the difference between pieces,
        # smoothed out over seconds (never more than +-4 dB)
        e = float((out ** 2).mean())
        self.level = getattr(self, "level", e) * 0.995 + e * 0.005              # ~4 s
        ref = getattr(self, "ref", None)
        if ref is None:
            ref = self.ref = self.level
        self.ref = ref * 0.9998 + self.level * 0.0002                          # the long-run level (~2 min)
        want = float(np.clip(np.sqrt(self.ref / (self.level + 1e-12)), 10 ** (-4 / 20), 10 ** (4 / 20)))
        g0 = getattr(self, "agc", 1.0)
        self.agc = g0 + (want - g0) * 0.02
        return out * (g0 + (self.agc - g0) * np.arange(1, BLOCK + 1) / BLOCK)


class StormCycle:
    """All the presets, as one storm's life (~half an hour, then again):
    it starts as a drizzle, the first thunder far off beyond the trees,
    builds to steady rain and a downpour as the thunder comes closer, breaks
    overhead as a thunderstorm, you shelter at a window while it rages, then
    hear its last on a tin roof; it moves off, rumbling further away, and the
    forest drips. Each stage runs a different length every time
    round, and one blends into the next over ~25 s (both playing at once, the
    old one fading out) -- so no two storms are the same."""

    # (preset, minutes, thunder: (every s, km) -- far, then closer, overhead,
    # then moving off; None for none)
    STAGES = (("drizzle", 3.0, None),
              ("forest", 3.0, ((40, 90), (9.0, 14.0))),          # far off, beyond the trees
              ("steady", 4.0, ((30, 70), (6.0, 11.0))),          # coming closer
              ("downpour", 3.0, ((20, 45), (2.5, 7.0))),
              ("thunderstorm", 5.0, ((12, 35), (0.5, 4.0))),     # overhead
              ("window", 4.0, ((18, 45), (1.5, 6.0))),           # sheltering while it rages
              ("tin roof", 3.0, ((30, 70), (4.0, 9.0))),         # passing
              ("distant storm", 4.0, ((35, 80), (8.0, 14.0))),   # moving off
              ("forest", 2.5, None))
    BLEND = 25.0                                        # seconds

    def __init__(self):
        self.rng = np.random.default_rng()
        self.engines = [Weather(), Weather()]           # this stage's, and the next's while blending
        self.stage = 0
        self.left = self._length(0)
        self.blending = 0.0                             # seconds into a blend (0: none)

    def _length(self, i):
        return self.STAGES[i][1] * 60 * self.rng.uniform(0.7, 1.3)

    def _play(self, engine, i):
        name, _, thunder = self.STAGES[i]
        return engine.core(name, thunder if thunder is not None else (None, None))

    @property
    def name(self):
        return self.STAGES[self.stage][0]

    def core(self):
        self.left -= BLOCK / RATE
        now = self._play(self.engines[0], self.stage)
        if self.left > self.BLEND:
            return now
        # the last BLEND seconds: the next stage comes in, this one goes out
        nxt = (self.stage + 1) % len(self.STAGES)
        coming = self._play(self.engines[1], nxt)
        x = 1 - max(0.0, self.left) / self.BLEND
        a = np.clip(x + (np.arange(1, BLOCK + 1) / BLOCK) * (BLOCK / RATE / self.BLEND), 0, 1)
        mixed = tuple(x * np.cos(a * np.pi / 2) + y * np.sin(a * np.pi / 2) for x, y in zip(now, coming))
        if self.left <= 0:
            self.stage = nxt
            self.engines.reverse()                      # the incoming one is now the one playing
            self.engines[1] = Weather()                 # and a fresh one waits for the next blend
            self.left = self._length(nxt)
        return mixed


class Effects:
    def __init__(self, sep_worker=None):
        self.pitch = [PitchShifter(), PitchShifter()]
        self.room = Reverb(make_reverb_ir(2.2, 0.6, 1))      # 8D: roomy, fairly bright
        self.hall = Reverb(make_reverb_ir(4.0, 0.85, 2))     # slowed: huge, dark
        self.bass = BassBoost()
        self.weather = Weather()            # rain and thunder ("ambience"), under everything
        self.head = HeadTracker(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dromac-head"))
        self._head_now = (np.zeros(BLOCK), np.zeros(BLOCK))
        self.speakers = [Spatializer(), Spatializer()]      # head tracking without 8D
        self.self_mix = True                # auto's engineer listening to its own output (switch)
        self.sources = [Spatializer() for _ in range(9)]     # the song, or each instrument (and drum)
        self.lorenz = Lorenz()
        self.rossler = Rossler()
        self.u = 0.0            # path phase
        self.path = None        # current preset name
        self.mode = "8d"
        self.prev_pos = [None] * 9
        self.glide = [None] * 9
        self.last_names = []
        self.speed = 1.0
        self.level_in = self.level_out = 0.0  # RMS, for diagnosing silence
        self.live_split = LiveSplit()
        self.sep_worker = sep_worker
        self.ai = None
        # The cushion every mode but ai split plays from (see CONT). Leaving ai
        # split, what you haven't heard yet goes into it, and the phone is paused
        # in the background while it plays down (catching_up) -- no jump forward.
        self.cont = np.zeros((2, 0))
        self.cont_ready = False
        self._first_fill = True
        self.catching_up = False
        self._skip_silence = 0
        self._was_held = False
        self._skip_n = None
        self._ai_idle = 0
        self.conductor = Conductor(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dromac_mood.py"))
        self.last_positions = []      # what the sound is doing now (for the dashboard)
        # foresight: the loudness of every block received, newest last -- the
        # newest `delay` worth of it hasn't been heard yet: that's the future
        self.env = deque(maxlen=1500)
        self._voice_future, self._voice_now, self._voice_check = None, 0.0, 0
        self.capture_seq = 0          # captures received so far (one per route)

    def positions(self, preset, speed):
        du = TWO_PI / LAP_SECONDS / RATE * speed
        if preset in ("lorenz", "rossler"):
            system, rate = (self.lorenz, 0.18) if preset == "lorenz" else (self.rossler, 0.9)
            start = getattr(self, f"_{preset}_last", None)
            end = np.array(system.advance(rate * speed * BLOCK / RATE))
            if start is None:
                start = end
            setattr(self, f"_{preset}_last", end)
            f = (np.arange(1, BLOCK + 1) / BLOCK)[:, None]
            xyz = start + (end - start) * f
            return xyz[:, 0], xyz[:, 1], xyz[:, 2]
        u = self.u + du * np.arange(BLOCK)
        self.u += du * BLOCK
        if preset == "stage":
            return 0 * u, 1.5 + 0 * u, 0 * u
        return PATHS[preset](u)

    def _glided(self, k, pos, key):
        """Glide source k from where it was when the layout changes (0.6 s)."""
        if key != getattr(self, "_glide_key", [None] * 9)[k]:
            if not hasattr(self, "_glide_key"):
                self._glide_key = [None] * 9
            if self.prev_pos[k] is not None:
                self.glide[k] = (self.prev_pos[k].copy(), int(0.6 * RATE))
            self._glide_key[k] = key
        if self.glide[k]:
            frm, left = self.glide[k]
            total = int(0.6 * RATE)
            t = np.clip((total - left + np.arange(BLOCK)) / total, 0, 1)
            t = t * t * (3 - 2 * t)  # smoothstep
            pos = frm + (pos - frm) * t[:, None]
            left -= BLOCK
            self.glide[k] = (frm, left) if left > 0 else None
        self.prev_pos[k] = pos[-1]
        return pos

    # which part of the band each source is: vocals, drums (or the kit), bass, other
    PART = ("vocals", "drums", "bass", "other", "other", "drums", "drums", "drums", "drums")

    # where the two virtual speakers stand: a stereo pair 30 degrees either side
    # of the front, at 1.2 m -- like studio monitors on a desk
    SPEAKERS = (np.array([-0.6, 1.04, 0.0]), np.array([0.6, 1.04, 0.0]))

    def _speakers(self, stereo, room=True):
        """The song as it is, but from two speakers in front of you that stay
        put while you turn your head (head tracking with 8D off or "still")."""
        out = np.zeros((2, BLOCK))
        room_in = np.zeros(BLOCK)
        for ch in range(2):
            pos = turn_with_head(np.tile(self.SPEAKERS[ch], (BLOCK, 1)), *self._head_now)
            direct, dry, r = self.speakers[ch].render(stereo[ch], pos)
            out += direct
            room_in += dry * 0.15
        return 1.25 * out + (self.room.process(room_in) if room else 0)

    def _levels(self, n, hear):
        """Each source's level this block, faded over ~0.2 s when it changes:
        `hear` says which parts of the band you're listening to."""
        if not hasattr(self, "_gain"):
            self._gain = np.ones(9)
        if n == 1:
            return [np.ones(BLOCK)]
        target = np.array([1.0 if hear.get(self.PART[k], True) else 0.0 for k in range(n)])
        start = self._gain[:n].copy()
        f = np.clip(np.arange(1, BLOCK + 1) / (0.2 * RATE), 0, 1)
        ramps = [start[k] + (target[k] - start[k]) * f for k in range(n)]
        self._gain[:n] = target
        return ramps

    def _still(self, sources, hear):
        """The instruments back in their places in the original stereo mix.
        (ai split: vocals + drums + bass + the rest L/R add up exactly to the
        song, so with vocals on this is the song; off, the song minus the voice.)"""
        g = self._levels(len(sources), hear)
        src = [x * gk for x, gk in zip(sources, g)]
        voc, drm, bas, oth_l, oth_r = src[:5]
        centre = voc + drm + bas + sum(src[5:])   # (+ the rest of the kit)
        return np.stack([centre + oth_l, centre + oth_r])

    def stem_positions(self, preset, speed, n=5):
        """n independent positions (BLOCK x 3 each), one per instrument."""
        du = TWO_PI / LAP_SECONDS / RATE * speed
        if preset in ("lorenz", "rossler"):
            # a chaotic system per instrument, each started a little differently:
            # they drift apart and wander on their own
            systems = getattr(self, f"_stems_{preset}", None)
            if systems is None:
                cls = Lorenz if preset == "lorenz" else Rossler
                systems = []
                for k in range(9):
                    sysk = cls()
                    sysk.s = sysk.s + np.array([0.7 * k, -0.4 * k, 0.5 * k])
                    systems.append(sysk)
                setattr(self, f"_stems_{preset}", systems)
                setattr(self, f"_stems_{preset}_last", [None] * 9)
            rate = 0.18 if preset == "lorenz" else 0.9
            lasts = getattr(self, f"_stems_{preset}_last")
            out = []
            f = (np.arange(1, BLOCK + 1) / BLOCK)[:, None]
            for k, sysk in enumerate(systems):
                end = np.array(sysk.advance(rate * speed * STEM_SPEED[k] * BLOCK / RATE))
                start = end if lasts[k] is None else lasts[k]
                lasts[k] = end
                out.append(start + (end - start) * f)
            return out[:n]
        ub = self.u + du * np.arange(BLOCK)
        self.u += du * BLOCK
        out = []
        for k in range(n):
            x, y, z = PATHS[preset](ub * STEM_SPEED[k] + STEM_PHASE[k])
            out.append(np.stack([np.broadcast_to(v, (BLOCK,)) for v in (x, y, z)], axis=1).astype(float))
        return out

    def _foresee(self, n):
        """Hands the conductor what's coming: the loudness of the audio already
        here but not yet heard (all of the AI's delay, or the cushion) and,
        with the AI split, the singer's part of what's separated ahead."""
        env = np.array(self.env)
        ahead = min(len(env), int(self.delay_samples // BLOCK))
        future = env[len(env) - ahead:] if ahead else env[:0]
        past = env[max(0, len(env) - ahead - 250):len(env) - ahead]
        if self.mode == "ai" and self.ai is not None and n > 1:
            self._voice_check = (self._voice_check + 1) % 5
            if self._voice_check == 0:
                start = max(self.ai.rd, 0)
                m = min(self.ai.done_upto - start, 3 * RATE)
                if m >= BLOCK:
                    with self.ai.lock:
                        voc = self.ai._ring_read(self.ai.stems[0:1], start, m - m % BLOCK)[0]
                    rms = np.sqrt((voc.reshape(-1, BLOCK).astype(np.float64) ** 2).mean(1))
                    self._voice_now, self._voice_future = float(rms[:5].mean()), rms[5:]
                else:
                    self._voice_future = None
        else:
            self._voice_future = None
        self.conductor.foresee(future, past, self._voice_future, self._voice_now)

    def _place(self, sources, preset, speed, hear=None, room=True, kit_tight=True, kit_boost=True):
        """sources: list of mono signals. One source follows the path; several
        (instruments) either stand on the stage or travel the path each in its
        own way. "auto": the conductor listens and decides."""
        auto = preset == "auto"
        self.conductor.kit_tight = kit_tight
        self.conductor.engineer.enabled = self.self_mix
        if auto:
            mono = sources[0] if len(sources) == 1 else \
                sum(sources[:3]) + 0.5 * (sources[3] + sources[4]) + sum(sources[5:])
            self._foresee(len(sources))
            self.conductor.update(mono, sources if len(sources) > 1 else None)
            stems_at = self.conductor.step(len(sources))
            base = stems_at[0]
        elif len(sources) == 1:
            x, y, z = self.positions(preset, speed)
            base = np.stack([np.broadcast_to(v, (BLOCK,)) for v in (x, y, z)], axis=1).astype(float)
        elif preset != "stage":
            stems_at = self.stem_positions(preset, speed, len(sources))
        out = np.zeros((2, BLOCK))
        room_in = np.zeros(BLOCK)
        names = NAMES_KIT if len(sources) == 9 else NAMES if len(sources) == 5 else ("song",)
        self.last_names = list(names)
        # the parts you've left out (split modes): faded out over ~0.2 s
        levels = self._levels(len(sources), hear or {})
        # where each source is this block: the path (or the stage), then -- in
        # auto -- shaped by what the music's doing, all five together
        positions = []
        for k in range(len(sources)):
            if len(sources) == 1:
                positions.append(base)
            elif preset == "stage" and not auto:
                positions.append(np.tile(np.array(STAGE[names[k]], float), (BLOCK, 1)))
            else:
                positions.append(stems_at[k])
        if len(sources) == 9 and not auto and kit_tight:
            # the split kit travels as one kit, where the drums would be
            center = np.tile(np.array(STAGE["drums"], float), (BLOCK, 1)) if preset == "stage" else positions[1]
            for k in KIT:
                positions[k] = kit_around(center, k)
        self.last_positions = [p[-1].tolist() for p in positions]
        for k, sig in enumerate(sources):
            sig = sig * levels[k] * (KIT_BOOST if kit_boost and len(sources) == 9 and k in KIT else 1.0)
            # (auto moves on springs already: no glide needed)
            pos = positions[k] if auto else self._glided(k, positions[k], (preset, len(sources)))
            pos = turn_with_head(pos, *self._head_now)    # stays put in the room as you turn
            direct, dry, r = self.sources[k].render(sig, pos)
            out += direct
            room_in += dry * (0.2 + 0.03 * r)
        # the room's reverb stays put while the direct sound fades with distance
        # (and can be switched off: "room")
        mix = 1.25 * out + (self.room.process(room_in) if room else 0)
        if auto:
            self.conductor.engineer.listen(mix, mono, getattr(self.conductor, "masking_now", 0.0))
        return mix

    # --- input and output are separate: push() takes each block the phone sends,
    # render() makes each block the player asks for. The player (the Mac's audio
    # clock) sets the pace, so when the input stops for a moment -- the cable
    # comes out and the capture moves to Wi-Fi -- playback carries on from the
    # cushion instead of stopping.

    def _mode(self, fx):
        """8D (motion) and the split (none / live / ai) are independent: with
        8D off but a split on, the instruments stay where the song has them
        ("still") -- e.g. for vocals off on their own."""
        split = fx.get("mode", "8d") if fx.get("mode") in ("dsp", "ai") else "8d"
        if fx.get("8d_on", True) and fx.get("8d") in PRESETS_8D:
            return fx.get("8d"), split
        if split != "8d":
            return "still", split
        return None, "8d"

    def push(self, stereo, fx):
        """One block from the phone."""
        preset, mode = self._mode(fx)
        self.level_in = float(np.sqrt((stereo ** 2).mean()))
        quiet = float(np.abs(stereo).max()) < 1e-4
        # The phone's silence while it's paused (by you, or for a route change or
        # a catch-up) never goes into a buffer: that's what makes pause/resume and
        # route changes seamless -- the song carries on from exactly where it was.
        if quiet and (fx.get("hold") or self.catching_up or self._skip_silence > 0):
            if self._skip_silence > 0:
                self._skip_silence -= 1
            return
        self._skip_silence = 0
        self.env.append(float(np.sqrt((stereo ** 2).mean())))     # for foresight
        if mode == "ai" and self.ai is not None and self.mode == "ai":
            self.ai.push(stereo)
        else:
            self.cont = np.concatenate([self.cont, stereo], axis=1)

    def _switch(self, mode):
        """Changing between ai split (its own ~6 s buffer) and the rest (the ~2 s
        cushion) without skipping or repeating a note."""
        if self.mode == "ai" and mode != "ai" and self.ai is not None:
            with self.ai.lock:
                frm = max(self.ai.rd, self.ai.floor, 0)   # never the old song's tail after a skip
                idx = np.arange(frm, self.ai.t) % self.ai.RING
                backlog = self.ai.inp[:, idx].astype(np.float64)
            self.cont = np.concatenate([backlog, self.cont], axis=1)
            self.ai.rd = self.ai.t
            self.cont_ready = True
            # more than the cushion's worth queued: pause the phone (unheard) while
            # it plays down, then resume it -- no jump forward
            if self.cont.shape[1] > CONT + RATE // 2:
                self.catching_up = True
                _tell_server("/api/audio/catchup?phase=pause")
        elif self.mode != "ai" and mode == "ai":
            if self.ai is None and self.sep_worker:
                self.ai = AISplit(self.sep_worker)
            if self.ai is not None:
                # carry on from exactly where you are: the AI gets what's still in
                # the cushion and separates from there (a few seconds' wait, no skip)
                self.ai.restart_from(self.cont.astype(np.float32))
            if self.catching_up:
                self.catching_up = False
                _tell_server("/api/audio/catchup?phase=resume")
            self.cont = np.zeros((2, 0))
            self.cont_ready = False
        self.mode = mode

    def render(self, fx):
        """One block for the player."""
        preset, mode = self._mode(fx)
        if fx.get("song"):
            self.conductor.new_song(fx["song"])
        slowed = bool(fx.get("slowed"))
        speed = float(np.clip(fx.get("speed", 1.0), 0.1, 4.0))
        self.speed = speed
        if mode != self.mode:
            self._switch(mode)

        # an idle AI is let go after a minute out of ai split (GPU and memory)
        if mode == "ai":
            self._ai_idle = 0
        elif self.ai is not None:
            self._ai_idle += 1
            if self._ai_idle > 50 * 60:
                self.ai.close()
                self.ai = None

        # paused: silence at once, and every buffer frozen exactly where you are
        if fx.get("hold"):
            self._was_held = True
            self.level_out = 0.0
            return np.zeros((2, BLOCK))
        if self._was_held:
            self._was_held = False
            self._skip_silence = 100   # up to 2 s while the phone starts playing again

        skip = fx.get("skip")
        if skip and skip.get("n") != self._skip_n:
            first = self._skip_n is None
            self._skip_n = skip.get("n")
            if not first and mode == "ai" and self.ai is not None:
                # where the new song starts in the input: a button press here is
                # ~0.4 s ahead of the phone switching; a skip made on the phone was
                # noticed a little after it began
                self.ai.skip_to(max(0, self.ai.t + int(skip.get("offset_ms", 0) * RATE / 1000)))

        # --- the raw block to play ---
        if mode == "ai" and self.ai is not None:
            self.ai.set_accel(float(fx.get("accel", 50)))
            # the slider moved to less delay: pause the phone (unheard) while the
            # buffer plays down to it, then resume -- no skip
            if self.ai.ready and not self.ai.warming:
                excess = self.ai.delay_samples - self.ai.delay
                if not self.catching_up and excess > RATE // 2:
                    self.catching_up = True
                    _tell_server("/api/audio/catchup?phase=pause")
                elif self.catching_up and excess <= RATE // 2 - RATE // 5:
                    self.catching_up = False
                    _tell_server("/api/audio/catchup?phase=resume")
            # A pause (route change) used up some of the delay. Win it back in
            # silence -- a gap between songs, a silent moment -- by holding back
            # a block there, so nobody hears it; meanwhile nothing stops.
            if self.ai.ready and self.ai.rd >= 0 and not self.catching_up and \
                    self.ai.delay_samples < self.ai.delay - RATE // 5 and self.ai.next_is_silent():
                stems = np.zeros((10, BLOCK))
            else:
                stems = self.ai.pull()
            self.ai.kit = bool(fx.get("kit"))
            # the drum kit, where it's been split: the kick in the drums' place,
            # the other pieces after the band; elsewhere the drums as one
            if self.ai.kit_here and self.ai.kit:
                stems = stems[[0, 5, 2, 3, 4, 6, 7, 8, 9]]
            else:
                stems = stems[:5]
            raw = None
        else:
            stems = None
            have = self.cont.shape[1]
            if self.catching_up and have <= CONT + RATE // 4:
                self.catching_up = False      # down to the cushion: let the phone play on
                _tell_server("/api/audio/catchup?phase=resume")
            if not self.cont_ready and have >= (CONT if self._first_fill else CONT // 4):
                self.cont_ready = True        # filled (the first time: the whole cushion)
                self._first_fill = False
            if self.cont_ready and have >= BLOCK and have < CONT - RATE // 5 and \
                    float(np.abs(self.cont[:, :BLOCK]).max()) < 0.002:
                raw = np.zeros((2, BLOCK))    # short of the cushion: rebuild it in silence
            elif self.cont_ready and have >= BLOCK:
                raw, self.cont = self.cont[:, :BLOCK], self.cont[:, BLOCK:]
            else:
                raw = np.zeros((2, BLOCK))    # ran dry: wait for a little to build up again
                if self.cont_ready and have < BLOCK:
                    self.cont_ready = False
            # very slow drift between the phone's clock and the Mac's (~0.2 s an
            # hour): a second past the cushion, drop one block of real silence
            # (the gap between songs) -- never a block of music
            if self.cont.shape[1] > CONT + RATE and not self.catching_up and \
                    float(np.abs(self.cont[:, :BLOCK]).max()) < 0.002:
                self.cont = self.cont[:, BLOCK:]

        # --- what moves where ---
        room = fx.get("room", True)
        self.self_mix = fx.get("self_mix", True)
        self.head.set_on(bool(fx.get("head")))
        self.head.recenter(fx.get("head_center", 0))
        self._head_now = self.head.step()
        hear = dict(fx.get("hear") or {})
        hear.setdefault("vocals", fx.get("vocals", True))
        if preset and mode in ("dsp", "ai"):
            if mode == "dsp":
                stems = self.live_split.process(raw)
            if preset == "still":
                out = self._still(list(stems), hear)
                if self.head.on:
                    out = self._speakers(out, room)
            else:
                out = self._place(list(stems), preset, speed, hear=hear, room=room,
                                  kit_tight=fx.get("kit_tight", True), kit_boost=fx.get("kit_boost", True))
            self.path = preset
        elif preset:
            self.live_split.process(raw)   # kept warm, for switching to live split instantly
            if preset == "still":
                out = self._speakers(raw, room) if self.head.on else raw
            else:
                out = self._place([0.5 * (raw[0] + raw[1])], preset, speed, room=room)
            self.path = preset
        else:
            self.path = None
            out = self._speakers(raw, room) if self.head.on else raw

        # slowed + reverb goes last -- after any buffering -- so switching it on or
        # off is heard straight away in every mode
        if slowed:
            ratio = 2 ** (-fx.get("semitones", 3) / 12)
            out = np.stack([self.pitch[0].process(out[0], ratio), self.pitch[1].process(out[1], ratio)])
            wet = self.hall.process(0.5 * (out[0] + out[1]))
            out = 0.85 * out + 0.45 * wet

        out = self.bass.process(out, float(fx.get("bass", 0) or 0))
        rain = fx.get("rain") or {}
        weather = self.weather.render(rain.get("preset", "steady"),
                                      float(rain.get("volume", 0)) if rain.get("on") and not rain.get("paused") else 0.0,
                                      yaw=float(self._head_now[0][-1]), thunder_db=float(rain.get("thunder", 0)))
        if weather is not None:
            out = out + weather

        # soft limit instead of hard clipping
        out = np.tanh(out * 0.9) / np.tanh(0.9)
        self.level_out = float(np.sqrt((out ** 2).mean()))
        return out

    @property
    def delay_samples(self):
        """How far behind the phone you're hearing it (for lyrics and progress)."""
        if self.mode == "ai" and self.ai is not None:
            return self.ai.delay_samples
        return self.cont.shape[1]


class BassBoost:
    """A low shelf: the song plus its own lows (a 2nd-order Butterworth
    lowpass at 100 Hz), turned up by `db`. The lowpass runs as the filter's
    impulse response by FFT (overlap-save, both channels), so it adds no delay;
    gain changes ramp across a block, so moving the slider never clicks."""
    TAPS = 2048

    def __init__(self, freq=100.0):
        w = np.tan(np.pi * freq / RATE)                 # bilinear Butterworth
        k = 1 / (1 + np.sqrt(2) * w + w * w)
        b = np.array([w * w, 2 * w * w, w * w]) * k
        a1, a2 = 2 * (w * w - 1) * k, (1 - np.sqrt(2) * w + w * w) * k
        h = np.zeros(self.TAPS)
        x1 = x2 = y1 = y2 = 0.0
        for n in range(self.TAPS):
            x0 = 1.0 if n == 0 else 0.0
            y0 = b[0] * x0 + b[1] * x1 + b[2] * x2 - a1 * y1 - a2 * y2
            h[n], x2, x1, y2, y1 = y0, x1, x0, y1, y0
        self.n = 4096
        self.H = np.fft.rfft(h, self.n)
        self.hist = np.zeros((2, self.TAPS - 1))
        self.gain = 0.0                                  # extra gain on the lows (linear - 1)

    def process(self, x, db):
        target = 10 ** (max(0.0, min(12.0, db)) / 20) - 1
        buf = np.concatenate([self.hist, x], axis=1)
        self.hist = buf[:, -(self.TAPS - 1):]
        if target == 0 and self.gain == 0:
            return x
        low = np.fft.irfft(np.fft.rfft(buf, self.n, axis=1) * self.H, self.n, axis=1)
        low = low[:, self.TAPS - 1:self.TAPS - 1 + x.shape[1]]
        g = self.gain + (target - self.gain) * np.arange(1, x.shape[1] + 1) / x.shape[1]
        self.gain = target
        # a third of the boost taken off overall, so the lows don't slam the limiter
        return (x + g * low) * (1 + g) ** (-1 / 3)


def read_wav_header(f):
    """Skips the RIFF header up to the 'data' chunk (scrcpy's wav may carry extra chunks)."""
    if f.read(12)[:4] != b"RIFF":
        raise ValueError("not a WAV stream")
    while True:
        head = f.read(8)
        if len(head) < 8:
            raise ValueError("no data chunk")
        cid, size = head[:4], int.from_bytes(head[4:], "little")
        if cid == b"data":
            return
        f.read(size + (size & 1))


def write_position(path, effects):
    """Where the sound is right now, for the dashboard's live 3D view."""
    try:
        data = {"t": __import__("time").time(), "preset": effects.path, "u": effects.u,
                "level_in": effects.level_in, "level_out": effects.level_out, "mode": effects.mode,
                "capture_seq": effects.capture_seq,
                "positions": effects.last_positions, "names": effects.last_names,
                # lightning: seconds since the last strike (its thunder follows)
                "flash_ago": round(__import__("time").time() - max(
                    [effects.weather.flash] + [e.flash for e in getattr(getattr(effects.weather, "cycle", None), "engines", [])]), 2),
                "head": {"on": effects.head.on, "yaw": round(effects.head.yaw, 3),
                         "pitch": round(effects.head.pitch, 3),
                         "shape": effects.head.shape, "raw_yaw": round(effects.head.raw[0], 4),
                         "raw_pitch": round(effects.head.raw[1], 4), "face": effects.head.on and
                         __import__("time").time() - effects.head.seen < 0.6, "error": effects.head.error},
                "auto": effects.conductor.report() if effects.path == "auto" else None,
                "ai_target_ms": round(effects.ai.delay / RATE * 1000) if effects.ai is not None else None,
                "ai_call_ms": round(effects.ai.call_time * 1000) if effects.ai is not None else None,
                # how far behind the phone you're hearing it (ai split buffers ~9.5 s),
                # so the dashboard can keep lyrics and the progress bar in step
                "delay_ms": round(effects.delay_samples / RATE * 1000),
                "ai": None if effects.ai is None else ("loading" if not effects.ai.ready else
                                                        "separating" if effects.ai.warming else "playing"),
                "speed": effects.speed, "lorenz": list(effects.lorenz.s), "rossler": list(effects.rossler.s),
                "stems_lorenz": [list(x.s) for x in getattr(effects, "_stems_lorenz", None) or []],
                "stems_rossler": [list(x.s) for x in getattr(effects, "_stems_rossler", None) or []]}
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        pass


def main(src, settings_path):
    # The audio loop shares the interpreter with background threads (the AI
    # split's receiver, the song learner). By default a thread waiting for its
    # turn can be held off ~5 ms each time -- and a block makes hundreds of
    # small numpy calls, each giving the turn up and waiting to get it back:
    # that piled up into 100-350 ms blocks, heard as stutters. 0.5 ms keeps the
    # audio loop in front.
    sys.setswitchinterval(0.0005)
    # And to macOS this is a background process: when the Mac is busy (a
    # browser, a build) it would park this loop on an efficiency core or pause
    # it outright for 100-300 ms -- each one a stutter. Marked user-interactive,
    # like the UI of the app in front, it gets the performance cores first.
    try:
        import ctypes
        libc = ctypes.CDLL("/usr/lib/libSystem.dylib")
        QOS_CLASS_USER_INTERACTIVE = 0x21
        libc.pthread_set_qos_class_self_np(QOS_CLASS_USER_INTERACTIVE, 0)
    except Exception:
        pass
    fx = {}
    pos_path = os.path.join(os.path.dirname(settings_path), "fx_pos.json")
    fx_mtime = None
    blocks_since_check = 0
    effects = Effects(sep_worker=os.path.join(os.path.dirname(os.path.abspath(__file__)), "dromac_sep.py"))
    # Building the reverbs takes a few seconds; the server waits for this line
    # before starting scrcpy, so that time doesn't become permanent delay.
    sys.stderr.write("ready\n")
    sys.stderr.flush()
    out = sys.stdout.buffer
    # The pipe is opened once, without blocking, and kept: captures come and go
    # on it (cable -> Wi-Fi), each starting with its own WAV header. Every turn
    # of the loop takes whatever input has arrived, then hands the player one
    # block -- and writing that block waits until the player has room, so the
    # Mac's audio clock sets the pace and playback never stops just because the
    # input paused.
    fd = os.open(src, os.O_RDONLY | os.O_NONBLOCK)
    need = BLOCK * 4
    pending = b""
    header = b""
    in_header = True
    # a slow block -- longer to make than the player's cushion can cover -- is
    # what you hear as a stutter: logged with what took the time
    import time as _time
    slow_log = os.path.join(os.path.dirname(settings_path), "fx_slow.log")
    while True:
        t_loop = _time.perf_counter()
        while True:
            try:
                data = os.read(fd, 1 << 16)
            except BlockingIOError:
                break                     # nothing more right now
            if not data:                  # no capture connected (between captures)
                # keep the whole blocks already read (played below); only an
                # incomplete fragment at the very end is dropped
                pending = pending[:len(pending) - len(pending) % need]
                in_header, header = True, b""
                break
            if in_header:
                header += data
                k = header.find(b"data")
                if k >= 0 and len(header) >= k + 8:
                    pending, header, in_header = pending + header[k + 8:], b"", False
                    # a new capture (route change): the phone is still restarting
                    # its playback for a moment -- that silence isn't the song
                    effects._skip_silence = 150
                    # and it's live: the phone's audio now goes to it, so it's safe
                    # for the server to resume the phone (see resume_after_route_change)
                    effects.capture_seq += 1
                    write_position(pos_path, effects)
                continue
            pending += data
        try:  # settings: checked every block (~20 ms), so pause/play is instant
            m = os.stat(settings_path).st_mtime
            if m != fx_mtime:
                fx_mtime = m
                with open(settings_path) as s:
                    fx = json.load(s)
        except Exception:
            pass
        while len(pending) >= need:
            frame, pending = pending[:need], pending[need:]
            effects.push(np.frombuffer(frame, "<i2").astype(np.float64).reshape(-1, 2).T / 32768, fx)
        if blocks_since_check == 5:
            write_position(pos_path, effects)
        blocks_since_check = (blocks_since_check + 1) % 10  # every ~200 ms
        t_render = _time.perf_counter()
        y = effects.render(fx)
        t_done = _time.perf_counter()
        if t_done - t_loop > 0.03:
            try:
                with open(slow_log, "a") as f:
                    f.write(f"{_time.strftime('%H:%M:%S')} block took {1000 * (t_done - t_loop):.0f} ms "
                            f"(input+settings {1000 * (t_render - t_loop):.0f}, render {1000 * (t_done - t_render):.0f}) "
                            f"mode={effects.mode} path={effects.path} n={len(effects.last_names)}\n")
            except Exception:
                pass
        out.write((np.clip(y.T, -1, 1) * 32767).astype("<i2").tobytes())
        out.flush()

if __name__ == "__main__":
    try:
        main(sys.argv[1], sys.argv[2])
    except (BrokenPipeError, KeyboardInterrupt):
        pass
