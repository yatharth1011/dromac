#!/usr/bin/env python3
"""Builds Dromac's real-rain bank (models/rain/bank.npz) from field recordings.

The recordings (Wikimedia Commons; credits in models/rain/CREDITS.txt) go in
models/rain_src. Each is cut into 10 s windows (5 s apart); CLAP listens to
each and keeps only clear rain -- no voices, cars, dogs, birds, music, and no
thunder (that plays separately, from the thunder pool) -- and says which kind:
light, heavy, on a window, in a forest. Runs of good windows become pieces of
15-90 s, levelled to the same loudness; the best of each kind are kept.

Run with the Python that runs Dromac (it has torch and transformers):
    python3 make_rain_bank.py [models folder]

The recordings (into models/rain_src under these names, with a sources.json
of title/artist/license/page/file; Commons serves an MP3 of each Ogg):
  r00.wav: "Light Rain Distant Thunder July 5th 2016.wav" by https://freesound.org/people/kvgarlic/ (CC0)
        https://commons.wikimedia.org/wiki/File:Light_Rain_Distant_Thunder_July_5th_2016.wav
  r01.mp3: "Falling Rain SFX 1 2016-11-19.oga" by valvalion (CC BY 3.0)
        https://commons.wikimedia.org/wiki/File:Falling_Rain_SFX_1_2016-11-19.oga
  r02.mp3: "Heavy rain in Glenshaw, PA.ogg" by Sage Ross (CC BY-SA 3.0)
        https://commons.wikimedia.org/wiki/File:Heavy_rain_in_Glenshaw,_PA.ogg
  r03.mp3: "October rain in Nepal.ogg" by Nirmal Dulal (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:October_rain_in_Nepal.ogg
  r04.wav: "Rain and wind in London 2026 08 28.wav" by Acabashi (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:Rain_and_wind_in_London_2026_08_28.wav
  r05.mp3: "Bourne woods rain 2020-05-10 0800.mp3" by Robert EA Harvey (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:Bourne_woods_rain_2020-05-10_0800.mp3
  r06.mp3: "Bourne woods rain 2020-05-10 0757.mp3" by Robert EA Harvey (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:Bourne_woods_rain_2020-05-10_0757.mp3
  r07.mp3: "Bourne woods rain 2020-05-10 0804.mp3" by Robert EA Harvey (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:Bourne_woods_rain_2020-05-10_0804.mp3
  r08.mp3: "Rain against the window.ogg" by cori (Public domain)
        https://commons.wikimedia.org/wiki/File:Rain_against_the_window.ogg
  r09.mp3: "Heavy Rain in Summer.ogg" by Fiver, der Hellseher (CC BY-SA 3.0)
        https://commons.wikimedia.org/wiki/File:Heavy_Rain_in_Summer.ogg
  r10.mp3: "Rain in Rome 27.ogg" by Nicholas Gemini (CC BY-SA 3.0)
        https://commons.wikimedia.org/wiki/File:Rain_in_Rome_27.ogg
  r11.mp3: "Sound of light rainfall.ogg" by Mijesty (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:Sound_of_light_rainfall.ogg
  r12.mp3: "Rain (1).ogg" by ezwa (Public domain)
        https://commons.wikimedia.org/wiki/File:Rain_(1).ogg
  r13.mp3: "Rain.ogg" by ジダネ (Public domain)
        https://commons.wikimedia.org/wiki/File:Rain.ogg
  r14.mp3: "RAIN-1.ogg" by Shirleen.Kay (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:RAIN-1.ogg
  big0.wav: "Rain on Concrete during storm with thunder.wav" by Dakendzor (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:Rain_on_Concrete_during_storm_with_thunder.wav
  big1.wav: "Calm rain.wav" by Zuvji (CC BY-SA 4.0)
        https://commons.wikimedia.org/wiki/File:Calm_rain.wav
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import torch
from transformers import ClapModel, ClapProcessor

R = 48000
ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Library/Application Support/Dromac/models"
KINDS = {"light": "light gentle rain falling", "heavy": "heavy rain downpour",
         "window": "rain hitting a window pane", "forest": "rain dripping on leaves in a forest"}
BAD = ["people talking", "a car driving past", "a dog barking", "birds singing", "music", "thunder",
       "wind blowing into a microphone", "footsteps"]
MINUTES_PER_KIND = 6.0
PIECE = 45                                   # seconds, at most


def load(path):
    """Any format macOS reads -> 48 kHz stereo float (via afconvert)."""
    tmp = Path("/tmp") / (path.stem + "_48k.wav")
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEF32@48000", str(path), str(tmp)], check=True)
    raw = tmp.read_bytes()
    tmp.unlink()
    i = raw.find(b"fmt ")
    ch = int.from_bytes(raw[i + 10:i + 12], "little")
    j = raw.find(b"data")
    n = int.from_bytes(raw[j + 4:j + 8], "little")
    if n == 0 or j + 8 + n > len(raw):              # (a huge file's size field can be unset)
        n = (len(raw) - j - 8) // (4 * ch) * 4 * ch
    x = np.frombuffer(raw[j + 8:j + 8 + n], "<f4").reshape(-1, ch).T
    return x[:2] if ch >= 2 else np.vstack([x, x])


def main():
    name = "laion/clap-htsat-unfused"
    model = ClapModel.from_pretrained(name).eval()
    proc = ClapProcessor.from_pretrained(name)

    def unit(e):
        e = e if torch.is_tensor(e) else e.pooler_output
        return e / e.norm(dim=-1, keepdim=True)

    labels = list(KINDS.values()) + BAD
    with torch.no_grad():
        text = unit(model.get_text_features(**proc(text=labels, return_tensors="pt", padding=True)))
    pieces = {}
    sources = json.loads((ROOT / "rain_src" / "sources.json").read_text())
    for src in sources:
        path = ROOT / "rain_src" / src.get("file", "")
        if not path.exists():
            continue
        x = load(path)
        win, hop = 10 * R, 5 * R
        good = []
        for s in range(0, max(1, x.shape[1] - win), hop):
            seg = x[:, s:s + win]
            with torch.no_grad():
                a = unit(model.get_audio_features(**proc(audio=seg.mean(0), sampling_rate=R, return_tensors="pt")))
            p = torch.softmax(100 * a @ text.T, dim=-1)[0].numpy()
            rain, bad = p[:len(KINDS)], p[len(KINDS):]
            # (kind: window or forest when the scene says so; light vs heavy is
            # decided later, relatively -- CLAP hears nearly all rain as "heavy")
            k = int(rain.argmax())
            heaviness = float(np.log((rain[1] + 1e-6) / (rain[0] + 1e-6)))
            good.append((s, float(rain.sum()), k, float(bad.max()), heaviness))
        # runs of clear rain (rain >= 85%, nothing else above 10%)
        run = []
        for s, rain, kind, bad, heavy in good + [(None, 0, 0, 1, 0)]:
            if s is not None and rain >= 0.85 and bad <= 0.10:
                run.append((s, kind, rain, heavy))
                continue
            if len(run) >= 2:
                start, end = run[0][0], run[-1][0] + win
                # the scene from the recording itself (its title says where it
                # was made: CLAP's guess at that was unreliable); plain rain is
                # split into light and heavy below
                title = src["title"].lower()
                kind = "forest" if ("wood" in title or "forest" in title) else \
                       "window" if "window" in title else "rain"
                score = float(np.mean([r for _, _, r, _ in run]))
                heavy = float(np.mean([h for _, _, _, h in run]))
                for a in range(start, end - 15 * R + 1, PIECE * R):
                    b = min(end, a + PIECE * R)
                    if b - a >= 15 * R:
                        pieces.setdefault(kind, []).append((score, src["file"], a, b, x[:, a:b].copy(), heavy))
            run = []
        print(f"{src['file']}: {sum(1 for g in good if g[1] >= 0.85 and g[3] <= 0.10)} of {len(good)} windows clear rain", flush=True)
    # plain rain: the lighter half "light", the heavier half "heavy"
    plain = sorted(pieces.pop("rain", []), key=lambda p: p[5])
    pieces["light"], pieces["heavy"] = plain[: len(plain) // 2], plain[len(plain) // 2:]
    bank, meta = {}, []
    for kind, ps in pieces.items():
        if not ps:
            continue
        # the best first, but taking turns between recordings (variety)
        by_src = {}
        for p in sorted(ps, key=lambda p: -p[0]):
            by_src.setdefault(p[1], []).append(p)
        order = []
        while any(by_src.values()):
            for f in list(by_src):
                if by_src[f]:
                    order.append(by_src[f].pop(0))
        total = 0
        for score, f, a, b, seg, _ in order:
            if total > MINUTES_PER_KIND * 60 * R:
                break
            seg = seg - seg.mean(1, keepdims=True)
            seg *= 10 ** (-24 / 20) / (np.sqrt((seg ** 2).mean()) + 1e-12)       # all at -24 dBFS
            fade = int(0.05 * R)
            seg[:, :fade] *= np.linspace(0, 1, fade)
            seg[:, -fade:] *= np.linspace(1, 0, fade)
            bank[f"p{len(meta)}"] = np.clip(seg, -1, 1).astype(np.float16)
            meta.append({"kind": kind, "src": f, "at_s": round(a / R, 1), "seconds": round((b - a) / R, 1), "sure": round(score, 2)})
            total += b - a
        print(f"{kind}: {total / R / 60:.1f} min in {sum(1 for m in meta if m['kind'] == kind)} pieces")
    out = ROOT / "rain"
    out.mkdir(exist_ok=True)
    np.savez(out / "bank.npz", **bank)
    (out / "bank.json").write_text(json.dumps(meta, indent=1))
    lines = ["Rain recordings used by Dromac, from Wikimedia Commons; cut into pieces, levelled.", ""]
    lines += [f'{s.get("file")}: "{s["title"]}" by {s["artist"] or "unknown"} -- {s["license"]} -- {s["page"]}' for s in sources]
    (out / "CREDITS.txt").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
