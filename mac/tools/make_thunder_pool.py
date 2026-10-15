#!/usr/bin/env python3
"""Builds Dromac's thunder pool (models/thunder/pool.npz) from real recordings.

The recordings (Wikimedia Commons, credits in models/thunder/CREDITS.txt) are
in models/thunder_src; convert them first (the WAVs are deleted afterwards --
~500 MB): for f in t*.*; do afconvert -f WAVE -d LEF32@48000 "$f" "wav/${f%.*}.wav"; done Thunder events are found where
the low band (30-300 Hz) rises 10 dB over the background; CLAP then listens
to each and keeps only what it's sure is thunder (not wind on the microphone,
a car, rain alone...). Each kept event is then
  - de-rained: the rain it was recorded in is measured from that recording's
    thunder-free stretches (a "fingerprint", band by band) and subtracted,
    smoothly (so no watery artifacts), leaving the thunder;
  - bass-boosted: the deep end small recorders miss is lifted, and a sub-bass
    layer added that follows the clap's own rumble, swelling and fading with it;
  - trimmed (max 15 s), faded, and levelled to the same loudest moment.

Run with the Python that runs Dromac (it has torch and transformers).

The recordings (download each into models/thunder_src as t00, t01, ...;
Commons serves an MP3 of each Ogg, which macOS can read; t10 is the 96 kHz
storm the rain bank uses too):
  t00: "Booming Thunder 2023-10-09 2-07-34 am.ogg" by KTerPalmers (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:Booming_Thunder_2023-10-09_2-07-34_am.ogg
  t01: "Lightning Recorded In Groningen 2018-05-13 01.wav" by A. Whyborn (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:Lightning_Recorded_In_Groningen_2018-05-13_01.wav
  t02: "Medium shower accompanied by thunders.ogg" by CutSomeSlackenstein (CC BY 4.0)
        https://commons.wikimedia.org/wiki/File:Medium_shower_accompanied_by_thunders.ogg
  t03: "Rhumphries - rbh-thunder-storm.ogg" by RHumphries (CC BY 3.0)
        https://commons.wikimedia.org/wiki/File:Rhumphries_-_rbh-thunder-storm.ogg
  t04: "Thunder.ogg" by Bidgee (CC BY 3.0)
        https://commons.wikimedia.org/wiki/File:Thunder.ogg
  t05: "Thunder.wav" by Subhashish Panigrahi (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:Thunder.wav
  t06: "Thunder 01.ogg" by Amuzujoe (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:Thunder_01.ogg
  t07: "Thunder Claps.ogg" by Jonathan Hunt (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:Thunder_Claps.ogg
  t08: "Thunder crack 2B.wav" by Jud McCranie (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:Thunder_crack_2B.wav
  t09: "Very close thunder cracks.flac" by Delorean76 (CC BY-SA 3.0)
        https://commons.wikimedia.org/wiki/File:Very_close_thunder_cracks.flac
  t10: "Rain on Concrete during storm with thunder.wav" by Dakendzor (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:Rain_on_Concrete_during_storm_with_thunder.wav
"""
import json
import sys
from pathlib import Path

import numpy as np
import torch
from transformers import ClapModel, ClapProcessor

R = 48000
ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Library/Application Support/Dromac/models"


def load(p):
    raw = p.read_bytes()
    i = raw.find(b"fmt ")
    ch = int.from_bytes(raw[i + 10:i + 12], "little")
    j = raw.find(b"data")
    n = int.from_bytes(raw[j + 4:j + 8], "little")
    x = np.frombuffer(raw[j + 8:j + 8 + n], "<f4").reshape(-1, ch).T
    return x if ch == 2 else np.vstack([x, x])


N_FFT, HOP = 2048, 512


def stft(x):
    w = np.hanning(N_FFT)
    n = 1 + (len(x) - N_FFT) // HOP
    frames = np.lib.stride_tricks.sliding_window_view(x, N_FFT)[::HOP][:n] * w
    return np.fft.rfft(frames, axis=1)


def istft(X, length):
    w = np.hanning(N_FFT)
    frames = np.fft.irfft(X, N_FFT, axis=1) * w
    out = np.zeros(length + N_FFT)
    norm = np.zeros(length + N_FFT)
    for i, f in enumerate(frames):
        out[i * HOP:i * HOP + N_FFT] += f
        norm[i * HOP:i * HOP + N_FFT] += w ** 2
    return (out / np.maximum(norm, 1e-6))[:length]


def rain_fingerprint(mono):
    """The rain's spectrum in this recording: the median of its quietest
    stretches (where the low band -- thunder -- is at its background)."""
    X = np.abs(stft(mono))
    f = np.fft.rfftfreq(N_FFT, 1 / R)
    low = X[:, (f > 30) & (f < 300)].sum(1)
    quiet = low <= np.percentile(low, 30)
    return np.median(X[quiet], axis=0)


def derain(seg, finger):
    """Spectral subtraction, smoothed in time and frequency."""
    out = []
    for ch in seg:
        X = stft(ch)
        mag = np.abs(X) + 1e-12
        gain = np.clip(1 - 4.0 * (finger / mag) ** 2, 0.03, 1.0)
        k = np.ones(5) / 5                                   # across frequency
        gain = np.apply_along_axis(lambda g: np.convolve(g, k, "same"), 1, gain)
        k = np.ones(3) / 3                                   # and time
        gain = np.apply_along_axis(lambda g: np.convolve(g, k, "same"), 0, gain)
        out.append(istft(X * gain, len(ch)))
    return np.array(out)


def bass_boost(seg, rng):
    """+6 dB below ~100 Hz, and a sub-bass layer (25-70 Hz) riding the clap's
    own low-band envelope."""
    n = seg.shape[1]
    f = np.fft.rfftfreq(n, 1 / R)
    shelf = 1 + 1.0 / np.sqrt(1 + (f / 100) ** 4)                 # x2 (+6 dB) in the deep end
    seg = np.array([np.fft.irfft(np.fft.rfft(ch) * shelf, n) for ch in seg])
    mono = seg.mean(0)
    low = np.fft.irfft(np.fft.rfft(mono) * ((f > 30) & (f < 200)), n)
    win = int(0.03 * R)
    env = np.sqrt(np.convolve(low ** 2, np.ones(win) / win, "same"))
    sub = np.fft.irfft(np.fft.rfft(rng.standard_normal(n)) * ((f > 25) & (f < 70)), n)
    sub *= env / (np.sqrt((sub ** 2).mean()) + 1e-12)
    return seg + 0.8 * sub[None, :]


def find_events(x):
    mono = x.mean(0)
    f = np.fft.rfftfreq(len(mono), 1 / R)
    low = np.fft.irfft(np.fft.rfft(mono) * ((f > 30) & (f < 300)), len(mono))
    fr = 2400
    m = len(low) // fr
    env = np.sqrt((low[: m * fr].reshape(m, fr) ** 2).mean(1)) + 1e-9
    db = 20 * np.log10(env / np.percentile(env, 30))
    out, i = [], 0
    while i < m:
        if db[i] > 10:
            s, e = i, i
            while s > 0 and db[s - 1] > 3:
                s -= 1
            while e < m - 1 and (db[e] > 3 or db[e + 1] > 3):
                e += 1
            if (e - s) * fr / R > 1.5:
                out.append((max(0, s * fr - R // 3), min(len(mono), (e + 6) * fr), float(db[s:e + 1].max())))
            i = e + 1
        else:
            i += 1
    return out


def main():
    name = "laion/clap-htsat-unfused"
    model = ClapModel.from_pretrained(name).eval()
    proc = ClapProcessor.from_pretrained(name)
    labels = ["thunder", "a thunderclap", "rolling thunder in the distance",
              "wind blowing into a microphone", "a microphone being bumped or handled", "a car or truck driving past",
              "an airplane flying overhead", "rain falling", "people talking", "a dog barking", "music", "a door closing"]

    def unit(e):
        e = e if torch.is_tensor(e) else e.pooler_output
        return e / e.norm(dim=-1, keepdim=True)

    with torch.no_grad():
        text = unit(model.get_text_features(**proc(text=labels, return_tensors="pt", padding=True)))
    clips, meta = [], []
    rng = np.random.default_rng(1)
    for p in sorted((ROOT / "thunder_src" / "wav").glob("t*.wav")):
        x = load(p)
        finger = rain_fingerprint(x.mean(0))
        for start, end, peak in find_events(x):
            seg = x[:, start:min(end, start + 15 * R)].copy()
            with torch.no_grad():
                a = unit(model.get_audio_features(**proc(audio=seg.mean(0)[: 10 * R], sampling_rate=R, return_tensors="pt")))
            prob = torch.softmax(100 * a @ text.T, dim=-1)[0].numpy()
            sure = float(prob[:3].sum())
            if sure < 0.8:
                continue
            # the rain right before this clap (rain changes through a storm):
            # its fingerprint, and a stretch of it to check how much comes out
            pre = x[:, max(0, start - 8 * R):start]
            local = rain_fingerprint(pre.mean(0)) if pre.shape[1] > 2 * R else finger
            seg = bass_boost(derain(seg, local), rng)
            if pre.shape[1] > R:
                test = pre[:, -R:]
                removed = 10 * np.log10(((test ** 2).mean() + 1e-12) / ((derain(test, local) ** 2).mean() + 1e-12))
            else:
                removed = float("nan")
            if removed < 6:
                continue                      # the rain wouldn't come out of this one: leave it
            n = seg.shape[1]
            seg[:, : int(0.02 * R)] *= np.linspace(0, 1, int(0.02 * R))
            seg[:, -int(0.4 * R):] *= np.linspace(1, 0, int(0.4 * R))
            fr = seg[0][: n // 4800 * 4800].reshape(-1, 4800)
            seg /= np.sqrt((fr ** 2).mean(1)).max() + 1e-9          # the loudest moment at 1.0
            # how sharp its start is (a close crack) vs a dull roll
            head = seg.mean(0)[: R // 2]
            spec = np.abs(np.fft.rfft(head))
            sharp = float((spec * np.fft.rfftfreq(len(head), 1 / R)).sum() / (spec.sum() + 1e-9))
            clips.append(seg.astype(np.float32))
            meta.append({"src": p.name, "at_s": round(start / R, 1), "sure": round(sure, 2), "peak_db": round(peak, 1),
                         "sharp_hz": round(sharp), "rain_removed_db": round(float(removed), 1)})
    # the best first, but taking turns between recordings, so no one storm
    # dominates the pool
    by_src = {}
    for i in sorted(range(len(clips)), key=lambda i: -(meta[i]["sure"] + meta[i]["peak_db"] / 50)):
        by_src.setdefault(meta[i]["src"], []).append(i)
    order = []
    while len(order) < 48 and any(by_src.values()):
        for f in list(by_src):
            if by_src[f] and len(order) < 48:
                order.append(by_src[f].pop(0))
    out = ROOT / "thunder"
    out.mkdir(exist_ok=True)
    np.savez(out / "pool.npz", **{f"c{k}": clips[i] for k, i in enumerate(order)})
    (out / "pool.json").write_text(json.dumps([meta[i] for i in order], indent=1))
    sources = json.loads((ROOT / "thunder_src" / "sources.json").read_text())
    lines = ["Thunder recordings used by Dromac, from Wikimedia Commons. Cut into clips, de-rained,",
             "bass-boosted, varied in playback; used under the licences below.", ""]
    lines += [f't{i:02d}: "{o["title"]}" by {o["artist"] or "unknown"} -- {o["license"]} -- {o["page"]}'
              for i, o in enumerate(sources)]
    (out / "CREDITS.txt").write_text("\n".join(lines) + "\n")
    print(f"{len(order)} thunder clips in the pool ({sum(c.shape[1] for c in clips) / R / 60:.0f} min considered); "
          f"rain removed: median {np.nanmedian([meta[i]['rain_removed_db'] for i in order]):.1f} dB")


if __name__ == "__main__":
    main()
