#!/usr/bin/env python3
"""Listens to the music for Dromac's "auto" mode (a worker process).

CLAP (laion/clap-htsat-unfused) turns each few seconds of audio into a
512-number fingerprint of how it sounds and feels. Only its listening half is
used -- no words, no labels, no categories decided in advance: what the
fingerprints mean is learned live from the song itself, in dromac_fx.py
(SongLearner), with no supervision.

Protocol, little-endian: request = uint32 frames + frames float32 (mono,
48 kHz); reply = 512 float32 (unit length). "ready <device>" on stderr once
loaded.
"""
import os
import struct
import sys

import numpy as np
import torch
import torchaudio.functional as AF
from transformers import ClapModel, ClapProcessor


def read_exact(f, n):
    data = b""
    while len(data) < n:
        chunk = f.read(n - len(data))
        if not chunk:
            raise EOFError
        data += chunk
    return data


def main():
    # Background work: the GPU does the heavy lifting, and its CPU side keeps
    # to two threads at a lower priority -- left to itself PyTorch takes every
    # core for each pass, and the real-time audio loop (dromac_fx.py) stutters.
    torch.set_num_threads(2)
    os.nice(10)
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    name = "laion/clap-htsat-unfused"
    model = ClapModel.from_pretrained(name).to(device).eval()
    proc = ClapProcessor.from_pretrained(name)
    sr = proc.feature_extractor.sampling_rate
    sys.stderr.write(f"ready {device}\n")
    sys.stderr.flush()
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    while True:
        try:
            (n,) = struct.unpack("<I", read_exact(inp, 4))
            audio = torch.from_numpy(np.frombuffer(read_exact(inp, n * 4), "<f4").copy())
        except EOFError:
            return
        with torch.inference_mode():
            if sr != 48000:
                audio = AF.resample(audio, 48000, sr)
            feats = proc(audio=audio.numpy(), sampling_rate=sr, return_tensors="pt")
            emb = model.get_audio_features(**{k: v.to(device) for k, v in feats.items()})
            if not torch.is_tensor(emb):        # newer transformers wrap it in an output object
                emb = emb.pooler_output
            emb = (emb / emb.norm(dim=-1, keepdim=True))[0].float().cpu().numpy()
        out.write(emb.astype("<f4").tobytes())
        out.flush()


if __name__ == "__main__":
    main()
