#!/usr/bin/env python3
"""AI stem separation for Dromac's "ai split" mode (Demucs, htdemucs).

A worker process, kept apart from dromac_fx.py because PyTorch is heavy and
would otherwise stall the real-time audio loop. Protocol on stdin/stdout,
little-endian:
  request : uint32 frames (top bit set: also split the drum kit), then
            frames x 2 float32 (stereo, 48 kHz, interleaved)
  reply   : uint32 frames, uint32 count, then count x frames float32 (mono
            stems: STEMS, then -- for a kit request -- the kit's PIECES)
Writes "ready <device>" to stderr once the model is loaded.

Runs on the Mac's GPU (Metal) when it can; a 9 s window separates in well
under its own length there, which is what lets the stream keep up.

"""
import os
import struct
import sys
from pathlib import Path

import numpy as np
import torch
import torchaudio.functional as AF
from demucs.apply import apply_model
from demucs.pretrained import get_model

from dromac_drums import DrumKit

STEMS = ("vocals", "drums", "bass", "other")
RATE = 48000
KIT = 0x80000000

KIT_MODELS = Path(__file__).resolve().parent / "models" / "larsnet"


def read_exact(f, n):
    data = b""
    while len(data) < n:
        chunk = f.read(n - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


# Wherever, moment to moment and band by band, the voice is what's sounding,
# the other parts' share of that band is handed to the voice: what Demucs
# missed of the singer (breaths, the tails of words, harmonies) stops leaking
# into the band -- heard with vocals off, or one instrument alone. Each part
# only gives up as much as the voice dominates it there (a soft mask), and the
# parts still add up exactly to the song.
RECLAIM = 0.85


def reclaim_voice(stems, v):
    """stems: (4, 2, n) at the model's rate; v: the vocals' index."""
    n = stems.shape[-1]
    win = torch.hann_window(2048)
    spec = torch.stft(stems.reshape(-1, n), 2048, 512, window=win, return_complex=True)
    spec = spec.reshape(stems.shape[0], 2, *spec.shape[-2:])
    voice = spec[v].abs() ** 2
    for k in range(stems.shape[0]):
        if k == v:
            continue
        mask = RECLAIM * voice / (voice + spec[k].abs() ** 2 + 1e-9)
        moved = spec[k] * mask
        spec[k] = spec[k] - moved
        spec[v] = spec[v] + moved
    out = torch.istft(spec.reshape(-1, *spec.shape[-2:]), 2048, 512, window=win, length=n)
    return out.reshape(stems.shape)


def separate(model, wav, device):
    """(2, n) at 44.1 k -> (sources, 2, n), the model's sources in its order."""
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    x = ((wav - mean) / std)[None].to(device)
    stems = apply_model(model, x, split=True, overlap=0.1, progress=False, device=device)[0]
    return (stems * std + mean).cpu()


def main():
    # Background work: the GPU does the heavy lifting, and its CPU side keeps
    # to two threads at a lower priority -- left to itself PyTorch takes every
    # core for each pass, and the real-time audio loop (dromac_fx.py) stutters.
    torch.set_num_threads(2)
    os.nice(10)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model = get_model("htdemucs")
    model.eval().to(device)
    order = [model.sources.index(s) for s in STEMS]
    kit = None                       # the drum-kit models: loaded on first use
    sys.stderr.write(f"ready {device}\n")
    sys.stderr.flush()
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    with torch.inference_mode():
        while True:
            try:
                (frames,) = struct.unpack("<I", read_exact(inp, 4))
                want_kit, frames = bool(frames & KIT), frames & ~KIT
                pcm = np.frombuffer(read_exact(inp, frames * 8), "<f4").reshape(-1, 2).T.copy()
            except EOFError:
                return
            wav = torch.from_numpy(pcm)
            wav = AF.resample(wav, RATE, model.samplerate)            # 48 k -> 44.1 k
            stems = separate(model, wav, device)
            stems = reclaim_voice(stems, model.sources.index("vocals"))
            mono = stems[order].mean(1)                               # (4, n) mono stems
            if want_kit and KIT_MODELS.exists():
                if kit is None:
                    kit = DrumKit(KIT_MODELS, device)
                drums = stems[model.sources.index("drums")]           # stereo, 44.1 k
                mono = torch.cat([mono, kit.split(drums).mean(1)])     # + kick, snare, toms, hihat, cymbals
            mono = AF.resample(mono, model.samplerate, RATE)          # back to 48 k
            n = min(mono.shape[1], frames)
            buf = np.zeros((mono.shape[0], frames), "<f4")
            buf[:, :n] = mono[:, :n].numpy()
            out.write(struct.pack("<II", frames, buf.shape[0]) + buf.tobytes())
            out.flush()


if __name__ == "__main__":
    main()
