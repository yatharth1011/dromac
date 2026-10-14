"""Splits a drum track into its kit -- kick, snare, toms, hi-hat, cymbals --
for Dromac's "drum kit" switch (ai split). Used by dromac_sep.py on the drum
stem Demucs gives it.

The network is LarsNet (Mezza, Giampiccolo, Bernardini, Sarti: "Toward deep
drum source separation", Pattern Recognition Letters 183, 2024): one U-Net
per kit piece, each predicting a soft mask over the drums' spectrogram
(STFT 4096 / hop 1024, 44.1 kHz, the lowest 2048 bins). This is a fresh
implementation of that published architecture, laid out to load the
authors' pretrained checkpoints (CC BY-NC 4.0, models/larsnet/).

The five masks are normalised to sum to one (Wiener-style), so the pieces
add back up to exactly the drum track -- nothing lost, nothing doubled.
"""
from pathlib import Path

import torch
import torch.nn as nn

PIECES = ("kick", "snare", "toms", "hihat", "cymbals")
SR = 44100
N_FFT, HOP, BINS = 4096, 1024, 2048


class _Down(nn.Module):
    def __init__(self, cin, cout):
        super().__init__()
        self.conv = nn.Conv2d(cin, cout, 5, stride=2, padding=2)
        self.bn = nn.BatchNorm2d(cout)
        self.act = nn.LeakyReLU(0.2)

    def forward(self, x):
        c = self.conv(x)
        return self.act(self.bn(c)), c          # next layer's input, and the skip (pre-norm)


class _Up(nn.Module):
    def __init__(self, cin, cout):
        super().__init__()
        self.conv_trans = nn.ConvTranspose2d(cin, cout, 5, stride=2, padding=2, output_padding=1)
        self.bn = nn.BatchNorm2d(cout)

    def forward(self, x):
        return self.bn(torch.relu(self.conv_trans(x)))


class PieceNet(nn.Module):
    """One kit piece: magnitude (B, 2, 2048, T) -> mask, same shape. T must be
    a multiple of 64 (six halvings)."""

    def __init__(self):
        super().__init__()
        self.input_norm = nn.BatchNorm2d(BINS)
        widths = (2, 16, 32, 64, 128, 256, 512)
        for i in range(6):
            setattr(self, f"enc{i + 1}", _Down(widths[i], widths[i + 1]))
        ups = ((512, 256), (512, 128), (256, 64), (128, 32), (64, 16), (32, 2))
        for i, (a, b) in enumerate(ups):
            setattr(self, f"dec{i + 1}", _Up(a, b))
        self.mask_layer = nn.Sequential(nn.Conv2d(2, 2, 4, dilation=2, padding=3), nn.Sigmoid())

    def forward(self, mag):
        x = self.input_norm(mag.transpose(1, 2)).transpose(1, 2)   # normalised per frequency bin
        skips = []
        for i in range(6):
            x, c = getattr(self, f"enc{i + 1}")(x)
            skips.append(c)
        u = self.dec1(skips[5])
        for i in range(2, 7):
            u = getattr(self, f"dec{i}")(torch.cat([skips[6 - i], u], 1))
        return self.mask_layer(u)


class DrumKit:
    def __init__(self, folder, device):
        self.device = device
        self.nets = []
        for piece in PIECES:
            net = PieceNet()
            ckpt = torch.load(str(Path(folder) / piece / f"pretrained_{piece}_unet.pth"),
                              map_location="cpu", weights_only=True)
            net.load_state_dict(ckpt["model_state_dict"])
            self.nets.append(net.eval().to(device))
        self.window = torch.hann_window(N_FFT)

    @torch.inference_mode()
    def split(self, drums):
        """drums: (2, n) at 44.1 kHz on the CPU -> (5, 2, n), in PIECES order."""
        n = drums.shape[-1]
        spec = torch.stft(drums, N_FFT, HOP, window=self.window, center=True, return_complex=True)
        frames = spec.shape[-1]
        pad = -frames % 64
        mag = torch.nn.functional.pad(spec.abs()[:, :BINS], (0, pad))[None].to(self.device)
        masks = torch.stack([net(mag)[0, :, :, :frames] for net in self.nets]).float().cpu()
        masks = masks / (masks.sum(0, keepdim=True) + 1e-7)             # the pieces add up to the drums
        masks = torch.nn.functional.pad(masks, (0, 0, 0, spec.shape[1] - BINS))
        masks[:, :, BINS:] = 1 / len(PIECES)                            # the top bin: shared out evenly
        pieces = spec[None] * masks
        return torch.istft(pieces.reshape(-1, *spec.shape[1:]), N_FFT, HOP, window=self.window,
                           center=True, length=n).reshape(len(PIECES), 2, n)
