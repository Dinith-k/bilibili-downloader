"""Replace the spoken voice of a video with an English voice (Kokoro TTS).

Run with the bili-audio interpreter:

    dub.py --cues cues.json --stems DIR --out dub.wav --voice am_michael [--keep-music] [--speed 1.0]
    dub.py --sample sample.wav --voice am_michael

DIR holds speech.wav, music.wav and effects.wav (44.1 kHz, from separate.py --stems-dir). Each cue's English line is
spoken at its subtitle time; the original speech is muted only while a cue is being spoken, so anything the
captions do not cover is left alone. Lines that are song lyrics (marked with a music note) are never replaced.
Prints "STAGE <text>" and "PROGRESS <percent>" lines.
"""
import argparse
import ctypes
import json
import os
import re
import sys
import wave

import numpy as np
import torch
import torchaudio

HERE = os.path.dirname(os.path.abspath(__file__))
TTS_DIR = os.path.join(os.environ.get("BILI_AUDIO_HOME") or os.path.join(os.path.expanduser("~"), "bili-audio"), "tts")
RATE = 44100
SAMPLE_TEXT = "Hello! This is how the English narration of your videos will sound."


def say(kind, value):
    print(f"{kind} {value}", flush=True)


def read_wav(path):
    with wave.open(path, "rb") as w:
        ch, rate = w.getnchannels(), w.getframerate()
        raw = w.readframes(w.getnframes())
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


def load_tts():
    from kokoro_onnx import Kokoro

    model, voices = os.path.join(TTS_DIR, "kokoro-v1.0.onnx"), os.path.join(TTS_DIR, "voices-v1.0.bin")
    if not (os.path.isfile(model) and os.path.isfile(voices)):
        sys.exit("the voice model files are missing from " + TTS_DIR)
    return Kokoro(model, voices)


def clean_text(text):
    text = re.sub(r"[♪♫♩♬]", " ", text or "")       # music notes
    text = re.sub(r"[\U0001F000-\U0001FFFF☀-➿]", " ", text)   # emoji and symbols
    text = re.sub(r"[\[\]{}<>*_#|~^`]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def speak(tts, text, voice, speed):
    """Mono float32 audio at 24 kHz for one line."""
    lang = "en-gb" if voice.startswith("b") else "en-us"
    samples, sr = tts.create(text, voice=voice, speed=float(np.clip(speed, 0.6, 1.6)), lang=lang)
    return torch.from_numpy(np.asarray(samples, dtype=np.float32)), sr


def to_stereo_44k(mono, sr):
    return torchaudio.functional.resample(mono.unsqueeze(0), sr, RATE).repeat(2, 1)


def rms(x):
    return float(x.pow(2).mean().sqrt()) if x.numel() else 0.0


PITCH_SR = 16000


def median_f0(x, fmin=70.0, fmax=400.0):
    """Median pitch (Hz) of the voiced parts of mono audio x at PITCH_SR, and how many voiced 10 ms steps there were."""
    frame, hop = int(0.04 * PITCH_SR), int(0.01 * PITCH_SR)
    if x.shape[-1] < frame * 2:
        return 0.0, 0
    x = x.numpy().astype(np.float64)
    frames = np.lib.stride_tricks.sliding_window_view(x, frame)[::hop]
    frames = frames - frames.mean(axis=1, keepdims=True)
    energy = frames.std(axis=1)
    loud = energy > max(0.004, 0.2 * np.percentile(energy, 90))
    spec = np.fft.rfft(frames * np.hanning(frame), 2 * frame, axis=1)
    ac = np.fft.irfft(np.abs(spec) ** 2, axis=1)[:, :frame]
    ac = ac / np.maximum(ac[:, :1], 1e-9)  # 1.0 at lag 0
    lo, hi = int(PITCH_SR / fmax), int(PITCH_SR / fmin)
    lag = lo + ac[:, lo:hi].argmax(axis=1)
    peak = ac[np.arange(len(ac)), lag]
    voiced = loud & (peak > 0.55)
    if not voiced.any():
        return 0.0, 0
    return float(np.median(PITCH_SR / lag[voiced])), int(voiced.sum())


MALE_BELOW, FEMALE_ABOVE = 150.0, 180.0  # adult male voices sit roughly at 85-155 Hz, female at 165-255 Hz


def line_voice(mono, cue, mfcc):
    """Pitch and timbre of the voice in one caption line: (median pitch in Hz, voiced steps, timbre vector or None).
    Pitch jumps when a speaker raises his voice or plays a character; timbre (the shape of the voice) does not."""
    seg = mono[int(max(0.0, float(cue["start"])) * PITCH_SR):int(float(cue["end"]) * PITCH_SR)]
    f0, voiced = median_f0(seg)
    if voiced < 12 or seg.numel() < PITCH_SR // 2:
        return f0, voiced, None
    m = mfcc(seg.unsqueeze(0))[0]               # (coefficients, frames)
    loud = m[0] >= m[0].median()                 # coefficient 0 follows loudness: keep the louder half, the voice itself
    return f0, voiced, m[1:13, loud].mean(dim=1).numpy()


def two_speakers(X):
    """Split timbre vectors into two groups (2-means). Returns (group per row, separation): the gap between the groups
    compared with how spread out each group is. Around 1 or less means it is really one speaker."""
    d = np.linalg.norm(X[:, None] - X[None], axis=2)
    a, b = np.unravel_index(d.argmax(), d.shape)
    centres = np.stack([X[a], X[b]])
    for _ in range(15):
        grp = np.linalg.norm(X[:, None] - centres[None], axis=2).argmin(axis=1)
        if len(set(grp)) < 2:
            return grp, 0.0
        centres = np.stack([X[grp == k].mean(axis=0) for k in (0, 1)])
    grp = np.linalg.norm(X[:, None] - centres[None], axis=2).argmin(axis=1)
    if len(set(grp)) < 2:
        return grp, 0.0
    spread = float(np.mean([np.linalg.norm(X[i] - centres[grp[i]]) for i in range(len(X))]))
    return grp, float(np.linalg.norm(centres[0] - centres[1]) / max(spread, 1e-6))


def speaker_genders(speech, cues, mode="auto"):
    """('m' | 'f' for each cue, pitches, summary text).

    mode 'male' / 'female' uses that voice for every line. 'auto' is male-first: lines are grouped by voice timbre; one
    speaker is male unless the whole voice sits clearly high AND no clearly low-pitched line was heard, so a man who
    raises his pitch is never given the female voice. Two timbre-distinct speakers each get their own gender."""
    n = len(cues)
    if mode in ("male", "female"):
        return [mode[0]] * n, [], f"every line uses the {mode} voice"
    mono = torchaudio.functional.resample(speech.mean(dim=0, keepdim=True), RATE, PITCH_SR)[0]
    mfcc = torchaudio.transforms.MFCC(sample_rate=PITCH_SR, n_mfcc=20, melkwargs={"n_fft": 512, "hop_length": 160, "n_mels": 40})
    info = [line_voice(mono, c, mfcc) for c in cues]
    pitches = [round(f) for f, _, _ in info]
    good = [i for i, (_, _, v) in enumerate(info) if v is not None]
    if not good:
        return ["m"] * n, pitches, "no clear speech found: every line uses the male voice"

    f0 = np.array([info[i][0] for i in good])
    low = int((f0 < MALE_BELOW).sum())
    male_heard = low >= 2 or low >= 0.2 * len(good)  # a clearly low voice speaks somewhere in this video
    speakers, sep, group = 1, 0.0, np.zeros(len(good), dtype=int)
    if len(good) >= 4:
        X = np.stack([info[i][2] for i in good])
        X = (X - X.mean(axis=0)) / np.maximum(X.std(axis=0), 1e-6)
        group, sep = two_speakers(X)
        if sep >= 1.6 and min(int((group == k).sum()) for k in (0, 1)) >= 2:
            speakers = 2
        else:
            group = np.zeros(len(good), dtype=int)

    gender = {}
    for k in range(speakers):
        pitch = float(np.median(f0[group == k]))
        if speakers == 1:
            gender[k] = "m" if (male_heard or pitch < FEMALE_ABOVE) else "f"
        else:
            gender[k] = "f" if pitch > FEMALE_ABOVE else "m"
    labels = [None] * n
    for pos, i in enumerate(good):
        labels[i] = gender[int(group[pos])]
    for i in range(n):  # lines without clear speech: same speaker as the line before...
        if labels[i] is None and i and labels[i - 1]:
            labels[i] = labels[i - 1]
    for i in range(n - 1, -1, -1):  # ...or the next clear line
        if labels[i] is None and i + 1 < n and labels[i + 1]:
            labels[i] = labels[i + 1]
    labels = [l or "m" for l in labels]
    summary = (f"{speakers} speaker{'s' if speakers > 1 else ''} by voice timbre (separation {sep:.1f}); "
               f"{labels.count('m')} male and {labels.count('f')} female lines; pitch in Hz: {pitches}")
    return labels, pitches, summary


def fade_curve(total, windows, fade=0.08):
    """1.0 everywhere except inside the (start, end) windows (seconds), with short fades at the edges."""
    g = torch.ones(total)
    f = int(fade * RATE)
    for a, b in windows:
        i, j = max(0, int(a * RATE)), min(total, int(b * RATE))
        if j <= i:
            continue
        g[i:j] = 0
        lo = max(0, i - f)
        g[lo:i] = torch.minimum(g[lo:i], torch.linspace(1, 0, i - lo))
        hi = min(total, j + f)
        g[j:hi] = torch.minimum(g[j:hi], torch.linspace(0, 1, hi - j))
    return g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cues")
    ap.add_argument("--stems")
    ap.add_argument("--out")
    ap.add_argument("--voice", default="am_michael", help="voice for male speakers (and for everyone when not matching)")
    ap.add_argument("--voice-female", default="af_heart", help="voice for female speakers")
    ap.add_argument("--mode", choices=["auto", "male", "female"], default="male",
                    help="auto: male voice for men, female only when the speakers are women; male / female: always that voice")
    ap.add_argument("--match-gender", action="store_true", help="same as --mode auto")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--keep-music", action="store_true")
    ap.add_argument("--sample", help="just speak a sample sentence into this wav file")
    args = ap.parse_args()

    say("STAGE", "Loading the English voice")
    tts = load_tts()
    if args.sample:
        audio, sr = speak(tts, SAMPLE_TEXT, args.voice, args.speed)
        write_wav(args.sample, audio.unsqueeze(0), sr)
        say("PROGRESS", 100)
        return
    if not (args.cues and args.stems and args.out):
        sys.exit("need --cues, --stems and --out (or --sample)")

    with open(args.cues, encoding="utf-8") as f:
        cues = [c for c in json.load(f)["cues"] if clean_text(c.get("en")) and "♪" not in (c.get("en") or "")]
    speech, _ = read_wav(os.path.join(args.stems, "speech.wav"))
    effects, _ = read_wav(os.path.join(args.stems, "effects.wav"))
    music, _ = read_wav(os.path.join(args.stems, "music.wav"))
    total = speech.shape[-1]

    mode = "auto" if args.match_gender else args.mode
    voices = [args.voice] * len(cues)
    if cues:
        say("STAGE", "Listening to who is speaking")
        genders, _, summary = speaker_genders(speech, cues, mode)
        voices = [args.voice_female if g == "f" else args.voice for g in genders]
        say("INFO", summary)

    say("STAGE", "Recording the English voice")
    track = torch.zeros(2, total + RATE * 8)  # room for lines that run past the end
    cursor, windows, placed = 0.0, [], 0
    for n, c in enumerate(cues):
        text = clean_text(c["en"])
        start = max(float(c["start"]), cursor)
        nxt = float(cues[n + 1]["start"]) if n + 1 < len(cues) else float(c["end"]) + 2.0
        slot = max(0.5, (float(c["end"]) - float(c["start"])) + min(0.6, max(0.0, nxt - float(c["end"]))))
        audio, sr = speak(tts, text, voices[n], args.speed)
        dur = audio.numel() / sr
        if dur > slot:  # too long for its subtitle time: speak faster, up to a natural limit
            audio, sr = speak(tts, text, voices[n], min(1.45, args.speed * dur / slot))
        clip = to_stereo_44k(audio, sr)
        i = int(start * RATE)
        if i + clip.shape[-1] > track.shape[-1]:
            continue
        track[:, i:i + clip.shape[-1]] += clip
        cursor = start + clip.shape[-1] / RATE + 0.08
        windows.append((float(c["start"]) - 0.15, max(float(c["end"]), cursor) + 0.3))
        placed += 1
        say("PROGRESS", int(90 * (n + 1) / len(cues)))

    if not placed:
        sys.exit("no caption lines to speak")
    say("STAGE", "Mixing the sound")
    voice = track[:, :total]
    # level: match the loudness of the original speaker where there was one, otherwise a sensible default
    inside = torch.cat([speech[:, int(a * RATE):int(b * RATE)] for a, b in windows if b > a], dim=1) if windows else speech
    target = float(np.clip(rms(inside), 0.04, 0.2)) if rms(inside) > 0.01 else 0.1
    live = voice[:, voice.abs().amax(dim=0) > 1e-4]
    gain = float(np.clip(target / max(rms(live), 1e-6), 0.3, 4.0))
    voice = voice * gain

    bed = effects + (music if args.keep_music else 0) + speech * fade_curve(total, windows)
    mix = bed + voice
    peak = float(mix.abs().max())
    if peak > 0.97:
        mix = mix * (0.97 / peak)
    write_wav(args.out, mix, RATE)
    say("PROGRESS", 100)


if __name__ == "__main__":
    main()
