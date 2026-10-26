"""Read burned-in Chinese captions from a video (OCR) and translate them to English.

Run with the bili-audio interpreter:

    captions.py --video in.mp4 --y0 0.86 --y1 0.98 --out cues.json     whole video, timed cues
    captions.py --image frame.jpg --y0 0.86 --y1 0.98 --out cues.json   a single frame (for previews)
    captions.py --prefetch                                              download the translation model

y0 / y1 are the top and bottom of the caption strip as fractions of the frame height.
Prints "STAGE <text>" and "PROGRESS <percent>" lines.
"""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys

AUDIO_HOME = os.environ.get("BILI_AUDIO_HOME") or os.path.join(os.path.expanduser("~"), "bili-audio")
os.environ.setdefault("HF_HOME", os.path.join(AUDIO_HOME, "hf"))  # where the translation model is cached

import cv2  # noqa: E402
import numpy as np  # noqa: E402

CJK = re.compile(r"[一-鿿]")
MODEL = "Helsinki-NLP/opus-mt-zh-en"
SAMPLE_FPS = 4  # caption strip samples per second


def say(kind, value):
    print(f"{kind} {value}", flush=True)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height:format=duration", "-of", "json", path],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout
    d = json.loads(out)
    return d["streams"][0]["width"], d["streams"][0]["height"], float(d["format"].get("duration") or 0)


def read_text(engine, img):
    """Chinese text found in a BGR image, top line first. Lines without Chinese characters are ignored."""
    result, _ = engine(img)
    lines = []
    for box, text, score in result or []:
        if float(score) < 0.55 or not CJK.search(text):
            continue
        lines.append((sum(p[1] for p in box) / 4, min(p[0] for p in box), text.strip()))
    lines.sort()
    return " ".join(t for _, _, t in lines)


def strip_geometry(W, H, y0, y1):
    cy = int(H * y0) // 2 * 2
    ch = max(16, int(H * (y1 - y0)) // 2 * 2)
    ch = min(ch, (H - cy) // 2 * 2)
    sw = min(W, 1280) // 2 * 2  # large videos are shrunk: faster, and plenty for text
    sh = max(16, round(ch * sw / W) // 2 * 2)
    return cy, ch, sw, sh


def strip_frames(video, W, H, y0, y1):
    cy, ch, sw, sh = strip_geometry(W, H, y0, y1)
    cmd = ["ffmpeg", "-v", "error", "-i", video, "-vf", f"fps={SAMPLE_FPS},crop={W}:{ch}:0:{cy},scale={sw}:{sh}",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    size = sw * sh * 3
    i = 0
    while True:
        buf = proc.stdout.read(size)
        if len(buf) < size:
            break
        yield i / SAMPLE_FPS, np.frombuffer(buf, np.uint8).reshape(sh, sw, 3)
        i += 1
    proc.stdout.close()
    proc.wait()


Y_FLOOR = 0.55  # captions are searched for in the lower 45% of the picture


def chinese_lines(engine, img):
    """Chinese text lines in a BGR image as (top, bottom, centre_x, width, text), all but text as image fractions."""
    h, w = img.shape[:2]
    result, _ = engine(img)
    lines = []
    for box, text, score in result or []:
        if float(score) < 0.6 or not CJK.search(text):
            continue
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        lines.append((min(ys) / h, max(ys) / h, (min(xs) + max(xs)) / 2 / w, (max(xs) - min(xs)) / w, text.strip()))
    return lines


def centred(lines):
    """Captions are centred on screen; this drops scattered text such as signs and watermarks."""
    return [l for l in lines if 0.35 <= l[2] <= 0.65 and l[3] >= 0.10]


def to_band(lines, y_floor):
    """Absolute (y0, y1) frame fractions covering the given strip-relative lines, with a little margin."""
    return (y_floor + min(l[0] for l in lines) * (1 - y_floor), y_floor + max(l[1] for l in lines) * (1 - y_floor))


def grab_lower(video, t, W, H):
    cy = int(H * Y_FLOOR) // 2 * 2
    ch = (H - cy) // 2 * 2
    sw = min(W, 1280) // 2 * 2
    sh = max(16, round(ch * sw / W) // 2 * 2)
    cmd = ["ffmpeg", "-v", "error", "-ss", str(t), "-i", video, "-frames:v", "1", "-vf",
           f"crop={W}:{ch}:0:{cy},scale={sw}:{sh}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    data = subprocess.run(cmd, capture_output=True).stdout
    if len(data) < sw * sh * 3:
        return None
    return np.frombuffer(data[:sw * sh * 3], np.uint8).reshape(sh, sw, 3)


def detect_band(engine, video, W, H, duration, samples=14):
    """Find where the captions sit in this video: OCR the lower part of frames spread across it and take the
    centred Chinese lines. Returns (y0, y1) or None if no captions were seen."""
    tops, bottoms, hits = [], [], 0
    for i in range(samples):
        frame = grab_lower(video, duration * (i + 0.5) / samples, W, H)
        if frame is None:
            continue
        lines = centred(chinese_lines(engine, frame))
        if lines:
            y0, y1 = to_band(lines, Y_FLOOR)
            tops.append(y0)
            bottoms.append(y1)
            hits += 1
        say("PROGRESS", int(6 * (i + 1) / samples))
    if hits < 3:
        return None
    tops.sort()
    bottoms.sort()
    y0 = tops[int(len(tops) * 0.15)] - 0.012      # a line or two of margin above the highest caption line
    y1 = bottoms[min(len(bottoms) - 1, int(len(bottoms) * 0.9))] + 0.02
    return max(0.5, y0), min(1.0, y1)


def norm(s):
    return re.sub(r"[\s\W_]+", "", s)


def build_cues(samples):
    """samples: [(time, text)] -> [{start, end, zh}] merging repeats of the same caption."""
    cues, cur, gap = [], None, 0
    step = 1.0 / SAMPLE_FPS

    def close(c):
        if c and c["end"] - c["start"] >= 0.5:  # shorter blips are OCR noise
            best = max(set(c["texts"]), key=c["texts"].count)
            cues.append({"start": round(c["start"], 2), "end": round(c["end"], 2), "zh": best})

    for t, text in samples:
        key = norm(text)
        if cur and key and difflib.SequenceMatcher(None, key, norm(cur["texts"][0])).ratio() >= 0.7:
            cur["texts"].append(text)
            cur["end"] = t + step
            gap = 0
        elif key:
            close(cur)
            cur, gap = {"start": t, "end": t + step, "texts": [text]}, 0
        else:
            gap += 1
            if gap >= 2:  # half a second with no text: the caption is gone
                close(cur)
                cur = None
    close(cur)
    # a cue should not overlap the next one
    for a, b in zip(cues, cues[1:]):
        a["end"] = min(a["end"], b["start"])
    return cues


def translate(texts):
    if not texts:
        return []
    import torch
    from transformers import MarianMTModel, MarianTokenizer

    say("STAGE", "Loading translation model")
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    tok = MarianTokenizer.from_pretrained(MODEL)
    model = MarianMTModel.from_pretrained(MODEL).to(dev)
    model.train(False)
    out = []
    say("STAGE", "Translating captions")
    with torch.inference_mode():
        for i in range(0, len(texts), 16):
            batch = tok(texts[i:i + 16], return_tensors="pt", padding=True, truncation=True, max_length=128).to(dev)
            gen = model.generate(**batch, num_beams=4, max_new_tokens=96)
            out += tok.batch_decode(gen, skip_special_tokens=True)
            say("PROGRESS", 70 + int(30 * min(1, (i + 16) / len(texts))))
    clean = []
    for t in out:
        t = re.sub(r"\s+", " ", t).strip()
        clean.append(t[:1].upper() + t[1:] if t else t)
    return clean


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video")
    ap.add_argument("--image")
    ap.add_argument("--y0", type=float, default=0.84)
    ap.add_argument("--y1", type=float, default=1.0)
    ap.add_argument("--out")
    ap.add_argument("--auto", action="store_true", help="find the caption position in the video itself")
    ap.add_argument("--prefetch", action="store_true")
    args = ap.parse_args()

    if args.prefetch:
        translate(["你好"])
        say("PROGRESS", 100)
        return
    if not (args.video or args.image) or not args.out:
        sys.exit("need --video or --image, and --out")
    if not 0 <= args.y0 < args.y1 <= 1:
        sys.exit("y0 must be smaller than y1, both between 0 and 1")

    say("STAGE", "Reading captions")
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()
    band, detected = (args.y0, args.y1), False
    if args.image:
        img = cv2.imread(args.image)
        H, W = img.shape[:2]
        text = ""
        if args.auto:  # one frame: the caption lines and their position come from the same pass
            fy = int(H * Y_FLOOR)
            lines = centred(chinese_lines(engine, img[fy:, :]))
            if lines:
                band, detected = to_band(lines, Y_FLOOR), True
                text = " ".join(l[4] for l in sorted(lines))
        if not detected:
            cy = int(H * args.y0)
            text = read_text(engine, img[cy:int(H * args.y1), :])
        cues = [{"start": 0.0, "end": 1.0, "zh": text}] if text else []
        W_, H_ = W, H
    else:
        W_, H_, duration = probe(args.video)
        if args.auto and duration > 2:
            say("STAGE", "Finding the captions")
            found = detect_band(engine, args.video, W_, H_, duration)
            if found:
                band, detected = found, True
        say("STAGE", "Reading captions")
        samples, prev, prev_text = [], None, ""
        for t, frame in strip_frames(args.video, W_, H_, band[0], band[1]):
            small = cv2.cvtColor(cv2.resize(frame, (64, 16)), cv2.COLOR_BGR2GRAY)
            if prev is None or float(np.mean(cv2.absdiff(small, prev))) > 3.0:  # unchanged strip: reuse the last text
                prev_text = read_text(engine, frame)
            prev = small
            samples.append((t, prev_text))
            if duration:
                say("PROGRESS", 6 + min(64, int(64 * t / duration)))
        cues = build_cues(samples)

    texts = translate([c["zh"] for c in cues])
    for c, en in zip(cues, texts):
        c["en"] = en
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"width": W_, "height": H_, "band": [round(band[0], 4), round(band[1], 4)], "detected": detected,
                   "cues": cues}, f, ensure_ascii=False, indent=1)
    say("PROGRESS", 100)


if __name__ == "__main__":
    main()
