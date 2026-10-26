"""Remove the background music from an audio file.

Uses the BandIt Plus model (speech / music / effects), keeps speech + effects and drops the music.
Run with the .venv-audio interpreter:

    separate.py --input in.wav --output no_music.wav [--stems-dir DIR]

Input must be 44.1 kHz PCM wav (the app makes it with ffmpeg). Prints "PROGRESS <percent>" lines.
"""
import argparse
import ctypes
import json
import math
import os
import sys
import types
import wave

import numpy as np
import torch
import torch.nn.functional as F

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPO = os.path.join(ROOT, "tools", "sep")  # ZFTurbo/Music-Source-Separation-Training (MIT)


def install_stand_ins():
    """The BandIt package imports several training-only libraries at module level. Inference never uses
    them, so empty stand-ins keep this runner light (and working on very new Python versions)."""
    for name in ("pytorch_lightning", "torchmetrics", "asteroid", "pedalboard", "torch_audiomentations", "pyloudnorm"):
        sys.modules[name] = types.ModuleType(name)
    sys.modules["pytorch_lightning"].LightningModule = torch.nn.Module  # the model only needs a plain nn.Module

    def hz_to_midi(f):
        return 12.0 * (np.log2(f) - math.log2(440.0)) + 69.0

    def midi_to_hz(m):
        return 440.0 * (2.0 ** ((np.asanyarray(m) - 69.0) / 12.0))

    librosa = types.ModuleType("librosa")
    librosa.hz_to_midi, librosa.midi_to_hz = hz_to_midi, midi_to_hz
    sys.modules["librosa"] = librosa

    def unsupported(*_a, **_k):
        raise RuntimeError("this band layout is not needed for the music-removal model")

    spafe = types.ModuleType("spafe")
    fbanks = types.ModuleType("spafe.fbanks")
    fbanks.bark_fbanks = types.SimpleNamespace(bark_filter_banks=unsupported)
    utils = types.ModuleType("spafe.utils")
    conv = types.ModuleType("spafe.utils.converters")
    conv.erb2hz = conv.hz2bark = conv.hz2erb = unsupported
    spafe.fbanks, spafe.utils, utils.converters = fbanks, utils, conv
    sys.modules.update({"spafe": spafe, "spafe.fbanks": fbanks, "spafe.utils": utils, "spafe.utils.converters": conv})

    # skip models/bandit/core/__init__.py (training code) but still allow importing its submodules
    sys.path.insert(0, REPO)
    core = types.ModuleType("models.bandit.core")
    core.__path__ = [os.path.join(REPO, "models", "bandit", "core")]
    sys.modules["models.bandit.core"] = core


def load_model(config_path, ckpt_path):
    install_stand_ins()
    from models.bandit.core.model import MultiMaskMultiSourceBandSplitRNNSimple

    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    model = MultiMaskMultiSourceBandSplitRNNSimple(**cfg["model"])
    state = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    for key in ("state", "state_dict", "model_state_dict"):
        if isinstance(state, dict) and key in state:
            state = state[key]
    model.load_state_dict(state)
    model.train(False)  # inference mode
    return model, cfg


def read_wav(path):
    with wave.open(path, "rb") as w:
        ch, rate, width = w.getnchannels(), w.getframerate(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("expected 16-bit PCM wav")
    x = torch.frombuffer(bytearray(raw), dtype=torch.int16).float() / 32768.0
    return x.view(-1, ch).t().contiguous(), rate


def write_wav(path, x, rate):
    pcm = (x.clamp(-1, 1) * 32767).round().to(torch.int16).t().contiguous()
    data = ctypes.string_at(pcm.data_ptr(), pcm.numel() * 2)
    with wave.open(path, "wb") as w:
        w.setnchannels(x.shape[0])
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)


@torch.inference_mode()
def separate(model, cfg, mix, device=torch.device("cpu")):
    """Chunked inference with cross-faded overlaps. mix: (channels, samples) -> (stems, channels, samples)."""
    chunk = cfg["audio"]["chunk_size"]
    overlap = cfg["inference"]["num_overlap"]
    step, fade = chunk // overlap, chunk // 10
    border = chunk - step
    n_stems = len(cfg["model"]["stems"])
    length0 = mix.shape[-1]
    if length0 > 2 * border and border > 0:
        mix = F.pad(mix, (border, border), mode="reflect")
    total = mix.shape[-1]

    window = torch.ones(chunk)
    window[:fade] = torch.linspace(0, 1, fade)
    window[-fade:] = torch.linspace(1, 0, fade)

    result = torch.zeros(n_stems, *mix.shape)
    counter = torch.zeros(total)
    i, last_pct = 0, -1
    while i < total:
        part = mix[:, i:i + chunk]
        n = part.shape[-1]
        part = F.pad(part, (0, chunk - n), mode="reflect" if n > chunk // 2 else "constant")
        out = model(part.unsqueeze(0).to(device))[0].float().cpu()  # (stems, channels, chunk)
        w = window.clone()
        if i == 0:
            w[:fade] = 1
        elif i + step >= total:
            w[-fade:] = 1
        result[..., i:i + n] += out[..., :n] * w[:n]
        counter[i:i + n] += w[:n]
        i += step
        pct = min(99, int(i * 100 / total))
        if pct != last_pct:
            print(f"PROGRESS {pct}", flush=True)
            last_pct = pct
    result = result / counter.clamp_min(1e-8)
    if length0 > 2 * border and border > 0:
        result = result[..., border:-border]
    return torch.nan_to_num(result)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True, help="wav with speech + effects (music removed)")
    ap.add_argument("--stems-dir", help="also write the three separated stems here")
    ap.add_argument("--config", default=os.path.join(ROOT, "models", "config_dnr_bandit.json"))
    ap.add_argument("--checkpoint", default=os.path.join(ROOT, "models", "model_bandit_plus_dnr.chpt"))
    args = ap.parse_args()

    torch.set_num_threads(max(1, os.cpu_count() or 1))
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"DEVICE {device.type}", flush=True)
    model, cfg = load_model(args.config, args.checkpoint)
    model.to(device)
    mix, rate = read_wav(args.input)
    if rate != cfg["audio"]["sample_rate"]:
        sys.exit(f"expected {cfg['audio']['sample_rate']} Hz audio, got {rate}")
    if mix.shape[0] == 1:  # the model is trained on stereo
        mix = mix.repeat(2, 1)

    stems = separate(model, cfg, mix, device)
    names = cfg["model"]["stems"]
    keep = [k for k, n in enumerate(names) if n != "music"]
    write_wav(args.output, stems[keep].sum(dim=0), rate)
    if args.stems_dir:
        os.makedirs(args.stems_dir, exist_ok=True)
        for k, n in enumerate(names):
            write_wav(os.path.join(args.stems_dir, n + ".wav"), stems[k], rate)
    print("PROGRESS 100", flush=True)


if __name__ == "__main__":
    main()
