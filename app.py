import copy
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, jsonify, render_template, request, send_file
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadCancelled, DownloadError, download_range_func

try:  # Bilibili's new empty video pages: get the details from its data service instead (see bili_patch.py)
    import bili_patch
    bili_patch.install()
except Exception as _e:  # never stop the app over this
    print("note: could not install the Bilibili page workaround:", _e)

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE, "data")
WORK_DIR = os.path.join(DATA_DIR, "tmp")
DB_PATH = os.path.join(DATA_DIR, "app.db")
DEFAULT_DL = os.path.join(BASE, "downloads")
os.makedirs(WORK_DIR, exist_ok=True)

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # always serve the latest static/app.js

BILIBILI_RE = re.compile(r"^https?://([\w-]+\.)?(bilibili\.com|b23\.tv)/", re.I)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
HAS_FFMPEG = shutil.which("ffmpeg") is not None


# ---------------------------------------------------------------- helpers

def valid_url(url):
    return bool(url and BILIBILI_RE.match(url))


def clean(e):
    return ANSI_RE.sub("", str(e)).replace("ERROR: ", "").strip()


def safe_name(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")[:80] or "Unknown"


_cookie_cache = {"t": 0, "path": None}


def cookies_file():
    """Optional Netscape cookies.txt exported from a logged-in Bilibili session (set in Settings)."""
    if time.time() - _cookie_cache["t"] > 30:
        try:
            rows = q("SELECT value FROM settings WHERE key='cookies_file'")
            path = json.loads(rows[0]["value"]) if rows else None
        except Exception:  # database not ready yet
            path = None
        _cookie_cache.update(t=time.time(), path=path if path and os.path.isfile(path) else None)
    return _cookie_cache["path"]


VPN_RE = r"TAP|TUN|Wintun|WireGuard|OpenVPN|VPN|Tailscale|ZeroTier|NordLynx|Proton|Fortinet|AnyConnect|SoftEther"
VIRTUAL_RE = VPN_RE + r"|Virtual|Hyper-V|VMware|Loopback|WSL|Bluetooth"
_net = {"t": 0.0, "vpn": False, "ip": None}


def net_state():
    """Is a VPN adapter up, and what is the address of the real (non-VPN) network connection?"""
    if os.name != "nt":
        return {"vpn": False, "ip": None}
    if time.time() - _net["t"] > 60:
        ps = ("$v = @(Get-NetAdapter | Where-Object { $_.Status -eq 'Up' -and $_.InterfaceDescription -match '%s' }).Count -gt 0;"
              "$p = Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' "
              "-and $_.InterfaceDescription -notmatch '%s' } | Select-Object -First 1;"
              "$ip = if ($p) { @($p.IPv4Address.IPAddress)[0] } else { $null };"
              "@{vpn=$v; ip=$ip} | ConvertTo-Json -Compress") % (VPN_RE, VIRTUAL_RE)
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                                 timeout=25, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            d = json.loads(out)
            _net.update(vpn=bool(d.get("vpn")), ip=d.get("ip"))
        except Exception:
            _net.update(vpn=False, ip=None)
        _net["t"] = time.time()
    return {"vpn": _net["vpn"], "ip": _net["ip"]}


def lookup_address():
    """Local address to send Bilibili lookups from. Bilibili blocks most VPN exit addresses (HTTP 412), so with a
    VPN on we ask from the real connection instead. The video data itself still goes through the VPN, which is
    usually much faster to Bilibili's servers."""
    try:
        rows = q("SELECT value FROM settings WHERE key='lookup_mode'")
        mode = json.loads(rows[0]["value"]) if rows else "auto"
    except Exception:  # database not ready yet
        mode = "auto"
    if mode != "auto":
        return None
    ns = net_state()
    return ns["ip"] if ns["vpn"] and ns["ip"] else None


_proxy_cache = {"t": 0.0, "dead": False}


def dead_local_proxy():
    """True when Windows points at a proxy on this PC that is not running. A VPN or proxy tool that closes (or crashes)
    can leave its address behind, and then every request fails with 'Unable to connect to proxy'."""
    now = time.time()
    if now - _proxy_cache["t"] > 10:
        dead = False
        try:
            for url in set(urllib.request.getproxies().values()):
                p = urllib.parse.urlparse(url if "://" in url else "http://" + url)
                if p.hostname in ("127.0.0.1", "localhost", "::1") and p.port:
                    try:
                        socket.create_connection((p.hostname, p.port), timeout=0.5).close()
                    except OSError:
                        dead = True
        except Exception:
            pass
        _proxy_cache.update(t=now, dead=dead)
    return _proxy_cache["dead"]


def base_opts(media=False):
    """yt-dlp options. media=True is for the actual video download (uses the system connection / VPN);
    otherwise this is a lookup, which may be sent from the non-VPN address."""
    opts = _base_opts()
    if dead_local_proxy():
        opts["proxy"] = ""  # empty = connect directly instead of through the dead proxy
    if cookies_file():
        opts["cookiefile"] = cookies_file()
    if not media:
        addr = lookup_address()
        if addr:
            opts["source_address"] = addr
    return opts


def _base_opts():
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 20,
        "fragment_retries": 20,
        "file_access_retries": 5,
        "socket_timeout": 30,
        "http_chunk_size": 4 * 1024 * 1024,  # small ranged requests survive dropped connections
        "concurrent_fragment_downloads": 4,
        "format_sort": ["res", "vcodec:h264", "acodec:aac"],  # H.264/AAC plays everywhere, AV1 often doesn't
        "http_headers": {"Referer": "https://www.bilibili.com/"},
    }


# ---------------------------------------------------------------- database

_db_lock = threading.Lock()


def q(sql, args=()):
    """Run one statement, return rows as dicts."""
    with _db_lock:
        con = sqlite3.connect(DB_PATH)
        try:
            con.row_factory = sqlite3.Row
            cur = con.execute(sql, args)
            rows = [dict(r) for r in cur.fetchall()]
            con.commit()
            return rows
        finally:
            con.close()


def qmany(sql, seq):
    with _db_lock:
        con = sqlite3.connect(DB_PATH)
        try:
            con.executemany(sql, seq)
            con.commit()
        finally:
            con.close()


def init_db():
    with _db_lock:
        con = sqlite3.connect(DB_PATH)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS channels(
                id INTEGER PRIMARY KEY, mid TEXT UNIQUE, name TEXT, added_at REAL, synced_at REAL);
            CREATE TABLE IF NOT EXISTS videos(
                bvid TEXT PRIMARY KEY, channel_id INTEGER, url TEXT, title TEXT, thumbnail TEXT,
                duration REAL, upload_date TEXT, position INTEGER, detail INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS downloads(
                id INTEGER PRIMARY KEY, bvid TEXT, title TEXT, path TEXT, size INTEGER, quality TEXT,
                crop TEXT, channel_id INTEGER, created_at REAL);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);
            CREATE INDEX IF NOT EXISTS idx_downloads_bvid ON downloads(bvid);
        """)
        for alter in ("ALTER TABLE downloads ADD COLUMN nomusic INTEGER DEFAULT 0",  # music was removed
                      "ALTER TABLE channels ADD COLUMN settings TEXT"):  # per-channel overrides (JSON)
            try:
                con.execute(alter)
            except sqlite3.OperationalError:
                pass  # column already exists
        con.commit()
        con.close()


DEFAULT_SETTINGS = {"quality": "best", "crop": None, "download_dir": DEFAULT_DL, "preview_url": "", "cookies_file": "",
                    "remove_music": False, "lookup_mode": "auto", "captions": None, "dub": None}


def get_settings():
    s = dict(DEFAULT_SETTINGS)
    for r in q("SELECT key, value FROM settings"):
        s[r["key"]] = json.loads(r["value"])
    return s


CHANNEL_KEYS = ("quality", "crop", "remove_music", "captions", "dub")  # settings a channel can override


def effective_settings(channel_id=None):
    """Global settings, with the channel's own overrides applied on top (if it has any)."""
    s = get_settings()
    if channel_id is not None:
        rows = q("SELECT settings FROM channels WHERE id=?", (channel_id,))
        if rows and rows[0]["settings"]:
            s = {**s, **json.loads(rows[0]["settings"])}
    return s


def downloaded_bvids():
    """bvids the app has downloaded. This is a history, so a video stays marked as downloaded even after its file is
    deleted from disk (people delete files to free space). Only 'Forget' in the Library takes the mark away."""
    return {r["bvid"] for r in q("SELECT DISTINCT bvid FROM downloads")}


def bvids_with_file():
    """The subset of downloaded bvids whose file is still on disk."""
    return {r["bvid"] for r in q("SELECT bvid, path FROM downloads") if r["path"] and os.path.exists(r["path"])}


# ---------------------------------------------------------------- Bilibili lookups

AKAMAI_HOST_RE = re.compile(r"^https?://[^/]*akamaized\.net/", re.I)
# Bilibili's Akamai mirror often truncates transfers; the same files are served by these nodes.
MIRRORS = ["upos-sz-mirrorhw.bilivideo.com", "upos-sz-mirrorcos.bilivideo.com",
           "upos-sz-mirrorali.bilivideo.com", "upos-sz-mirrorcosb.bilivideo.com", None]  # None = original host


def with_mirror(url, host):
    if not host or not AKAMAI_HOST_RE.match(url):
        return url
    return re.sub(r"^(https?://)[^/]+", rf"\g<1>{host}", url, count=1)


INFO_TTL = 600  # seconds; stream URLs stay valid far longer than this
_info_cache = {}  # url -> (timestamp, raw info)


def extract_with_retry(ydl, url, tries=6):
    """Bilibili sometimes answers with an empty format list or resets the connection; back off and retry."""
    for attempt in range(tries):
        try:
            info = ydl.extract_info(url, download=False, process=False)
            if info.get("formats"):
                return info
            raise DownloadError("No video formats found!")
        except DownloadError:
            if attempt == tries - 1:
                raise
            time.sleep(2 * (attempt + 1))


def get_info(url):
    """One Bilibili lookup shared by Fetch and Download (fewer requests = fewer empty answers)."""
    hit = _info_cache.get(url)
    if hit and time.time() - hit[0] < INFO_TTL:
        return copy.deepcopy(hit[1])
    with YoutubeDL({**base_opts(), "skip_download": True}) as ydl:
        info = extract_with_retry(ydl, url)
    _info_cache[url] = (time.time(), info)
    return copy.deepcopy(info)


def bvid_of(info):
    m = re.match(r"BV[0-9A-Za-z]+", info.get("id") or "")
    return m.group(0) if m else info.get("id")


# ---------------------------------------------------------------- crop

def normalize_crop(c):
    """Validate a crop request. Either {ratio, size, cx, cy} (resolved per video) or a fixed box {x,y,w,h}."""
    if not c:
        return None
    try:
        if "ratio" in c:
            r = "orig" if c["ratio"] == "orig" else float(c["ratio"])
            size, cx, cy = float(c.get("size", 1)), float(c.get("cx", .5)), float(c.get("cy", .5))
            if (r != "orig" and not .1 <= r <= 10) or not .05 <= size <= 1 or not (0 <= cx <= 1 and 0 <= cy <= 1):
                raise ValueError
            if r == "orig" and size >= .999:
                return None
            return {"ratio": r, "size": size, "cx": cx, "cy": cy}
        box = {k: float(c[k]) for k in ("x", "y", "w", "h")}
        if not (0 <= box["x"] <= 1 and 0 <= box["y"] <= 1 and .02 <= box["w"] <= 1 and .02 <= box["h"] <= 1
                and box["x"] + box["w"] <= 1.001 and box["y"] + box["h"] <= 1.001):
            raise ValueError
        return None if box["w"] >= .999 and box["h"] >= .999 else box
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid crop area.")


def resolve_box(crop, width, height):
    if "ratio" not in crop:
        return crop
    v = width / height
    r = v if crop["ratio"] == "orig" else crop["ratio"]
    mw, mh = (1, v / r) if r >= v else (r / v, 1)
    w, h = mw * crop["size"], mh * crop["size"]
    return {"w": w, "h": h, "x": min(max(crop["cx"] - w / 2, 0), 1 - w), "y": min(max(crop["cy"] - h / 2, 0), 1 - h)}


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
         "-of", "json", path], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout
    data = json.loads(out)
    return data["streams"][0]["width"], data["streams"][0]["height"], float(data["format"].get("duration") or 0)


def caption_filter(height, y0, y1, ass_name="captions.ass"):
    """Blur the caption strip (y0..y1 of the frame height), darken it a little, then draw the subtitle file on it."""
    cy = int(height * y0) // 2 * 2
    ch = max(2, int(height * (y1 - y0)) // 2 * 2)
    ch = min(ch, height - cy)
    return (f"split[full][strip];[strip]crop=iw:{ch}:0:{cy},gblur=sigma=24,eq=brightness=-0.28[blur];"
            f"[full][blur]overlay=0:{cy},subtitles={ass_name}")


def render_video(job, src, crop=None, caps=None):
    """One ffmpeg pass: optional English captions over a blurred strip, then optional crop. Reports progress and
    returns the final file path (same name as src). caps = {"ass": file name in the job folder, "y0", "y1"}."""
    width, height, duration = probe(src)
    filters = []
    if caps:
        filters.append(caption_filter(height, caps["y0"], caps["y1"], caps["ass"]))
    if crop:
        box = resolve_box(crop, width, height)
        cw = max(2, int(width * box["w"]) // 2 * 2)
        ch = max(2, int(height * box["h"]) // 2 * 2)
        cx = min(int(width * box["x"]) // 2 * 2, width - cw)
        cy = min(int(height * box["y"]) // 2 * 2, height - ch)
        filters.append(f"crop={cw}:{ch}:{cx}:{cy}")
    stem = os.path.splitext(src)[0]
    out = stem + ".rendered.mp4"
    cmd = ["ffmpeg", "-y", "-i", src, "-vf", ",".join(filters), "-c:v", "libx264", "-preset", "veryfast",
           "-crf", "20", "-c:a", "copy", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", out]
    job.update(stage="Adding English captions" if caps else "Cropping", percent=0, speed=0, eta=None, downloaded=0, total=0)
    # cwd = the job folder, so the subtitle file can be named without a drive letter (which ffmpeg filters mishandle)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=os.path.dirname(os.path.abspath(src)),
                            text=True, encoding="utf-8", errors="replace")
    tail = []
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("out_time_us=") and duration:
            try:
                job["percent"] = min(99.9, round(int(line.split("=")[1]) / 1e6 * 100 / duration, 1))
            except ValueError:
                pass
            if job.get("cancel"):
                proc.kill()
        else:
            tail = (tail + [line])[-5:]
    if proc.wait() != 0:
        if job.get("cancel"):
            raise DownloadCancelled()
        raise RuntimeError("Rendering failed: " + " | ".join(tail))
    os.remove(src)
    final = stem + ".mp4"
    os.replace(out, final)
    return final


# ---------------------------------------------------------------- background-music removal
# The BandIt Plus model (speech / music / effects) runs in its own Python environment (.venv-audio) so
# PyTorch stays out of this app. We keep speech + effects and drop the music stem.

# The environment lives outside the project: this folder is synced by OneDrive and PyTorch is several GB.
AUDIO_HOME = os.environ.get("BILI_AUDIO_HOME") or os.path.join(os.path.expanduser("~"), "bili-audio")
AUDIO_PY = next((p for p in (os.path.join(AUDIO_HOME, "venv", "Scripts", "python.exe"),
                             os.path.join(BASE, ".venv-audio", "Scripts", "python.exe")) if os.path.isfile(p)),
                os.path.join(AUDIO_HOME, "venv", "Scripts", "python.exe"))
SEPARATE_PY = os.path.join(BASE, "audio", "separate.py")
MODEL_CKPT = os.path.join(BASE, "models", "model_bandit_plus_dnr.chpt")
MODEL_CFG = os.path.join(BASE, "models", "config_dnr_bandit.json")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_music_ok = {"t": 0.0, "ok": False}


def tool_ready(state, files_ok, import_cmd):
    """Is an optional AI tool usable? The (slow) import test only has to pass once; after that we just make sure its
    files are still there, so a busy moment can never make a working tool look uninstalled."""
    now = time.time()
    if state["ok"]:
        if now - state["t"] > 60:
            state.update(t=now, ok=bool(files_ok()))
        return state["ok"]
    if now - state["t"] > 15:
        ok = bool(files_ok())
        for attempt in range(2 if ok else 0):
            try:
                ok = subprocess.run([AUDIO_PY, "-c", import_cmd], capture_output=True, timeout=120,
                                    creationflags=NO_WINDOW).returncode == 0
            except Exception:
                ok = False
            if ok:
                break
            time.sleep(1.5)
        state.update(t=now, ok=ok)
    return state["ok"]


def music_available():
    """True once the audio environment (PyTorch) and the model files are in place."""
    return tool_ready(_music_ok, lambda: (os.path.isfile(AUDIO_PY) and os.path.isfile(MODEL_CFG) and os.path.isfile(MODEL_CKPT)
                                          and os.path.getsize(MODEL_CKPT) > 148_000_000 and HAS_FFMPEG),
                      "import torch, torchaudio, numpy")


def run_ffmpeg(cmd):
    r = subprocess.run(["ffmpeg", "-y", "-v", "error", *cmd], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg failed: " + (r.stderr or "").strip()[-300:])


def run_separation(job, wav_in, wav_out, stems_dir=None):
    """Split wav_in into speech / music / effects. wav_out gets speech + effects; stems_dir (optional) gets all three."""
    cmd = [AUDIO_PY, SEPARATE_PY, "--input", wav_in, "--output", wav_out] + (["--stems-dir", stems_dir] if stems_dir else [])
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    started, tail = time.time(), []
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("PROGRESS "):
            pct = float(line.split()[1])
            job.update(percent=pct, eta=(time.time() - started) * (100 - pct) / pct if pct >= 2 else None)
            if job.get("cancel"):
                proc.kill()
        elif line:
            tail = (tail + [line])[-6:]
    rc = proc.wait()
    if job.get("cancel"):
        raise DownloadCancelled()
    if rc != 0:
        raise RuntimeError("Music removal failed: " + " | ".join(tail))


def rebuild_audio(job, path, audio_only=False, dub=None, cues_path=None, no_music=True):
    """New audio track for `path`. no_music drops the background music; dub = {voice, speed} also replaces the spoken
    voice (inside the subtitle times in cues_path) with an English one. Returns the final file path."""
    if not music_available():
        raise RuntimeError("Music removal is not installed yet.")
    work = job["dir"]
    wav_in, wav_out = os.path.join(work, "music_in.wav"), os.path.join(work, "music_out.wav")
    stems = os.path.join(work, "stems")
    job.update(stage="Preparing the audio", percent=0, speed=0, eta=None, downloaded=0, total=0)
    run_ffmpeg(["-i", path, "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", wav_in])
    job.update(stage="Separating voice, music and effects" if dub else "Removing music", percent=0)
    run_separation(job, wav_in, wav_out, stems if dub else None)
    if dub:
        dubbed = os.path.join(work, "dubbed.wav")
        job.update(stage="Recording the English voice", percent=0, eta=None)
        run_audio_script(job, DUB_PY, ["--cues", cues_path, "--stems", stems, "--out", dubbed, "--voice", dub["voice"],
                                       "--voice-female", dub["voice_f"], "--mode", dub["mode"],
                                       "--speed", str(dub["speed"])] + ([] if no_music else ["--keep-music"]))
        wav_out = dubbed
    job.update(stage="Rebuilding the file", percent=99.5, eta=None)
    stem = os.path.splitext(path)[0]
    # a generated voice can come out quieter than the speaker it replaces: even the loudness out (YouTube's norm is -14)
    level = ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"] if dub else []
    if audio_only:
        tmp, final = stem + ".newaudio.mp3", stem + ".mp3"
        run_ffmpeg(["-i", wav_out, *level, "-c:a", "libmp3lame", "-q:a", "2", tmp])
    else:
        tmp, final = stem + ".newaudio.mp4", stem + ".mp4"
        run_ffmpeg(["-i", path, "-i", wav_out, "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *level,
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", tmp])
    os.remove(path)
    os.replace(tmp, final)
    for f in (wav_in, os.path.join(work, "music_out.wav"), os.path.join(work, "dubbed.wav")):
        if os.path.exists(f):
            os.remove(f)
    shutil.rmtree(stems, ignore_errors=True)
    return final


def run_audio_script(job, script, args):
    """Run one of the audio/*.py scripts in the audio environment, following its STAGE / PROGRESS lines."""
    proc = subprocess.Popen([AUDIO_PY, script, *args], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    started, tail = time.time(), []
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("STAGE "):
            job.update(stage=line[6:])
        elif line.startswith("PROGRESS "):
            pct = float(line.split()[1])
            job.update(percent=pct, eta=(time.time() - started) * (100 - pct) / pct if pct >= 3 else None)
            if job.get("cancel"):
                proc.kill()
        elif line.startswith("INFO "):
            job["note"] = line[5:]  # e.g. how many lines went to the male and female voice
        elif line and "Warning" not in line:
            tail = (tail + [line])[-6:]
    rc = proc.wait()
    if job.get("cancel"):
        raise DownloadCancelled()
    if rc != 0:
        raise RuntimeError(f"{os.path.basename(script)} failed: " + " | ".join(tail[-3:]))


# ---------------------------------------------------------------- Chinese -> English captions
# audio/captions.py (run in the bili-audio environment) reads the burned-in Chinese captions with OCR and translates
# them. Here we turn the result into a subtitle file and blur the old strip (see render_video / caption_filter).

CAPTIONS_PY = os.path.join(BASE, "audio", "captions.py")
DEFAULT_CAPTION_REGION = (0.84, 1.0)  # top / bottom of the caption strip, as fractions of the frame height
_caps_ok = {"t": 0.0, "ok": False}


def captions_available():
    """True once the OCR and translation libraries are installed in the audio environment."""
    return tool_ready(_caps_ok, lambda: os.path.isfile(AUDIO_PY) and os.path.isfile(CAPTIONS_PY) and HAS_FFMPEG,
                      "import rapidocr_onnxruntime, transformers, cv2, sentencepiece")


def clean_captions(c):
    """Validate the captions setting {on, region: {y0, y1}, ui}. Returns the value to store (or None)."""
    if not c:
        return None
    try:
        y0, y1 = float(c["region"]["y0"]), float(c["region"]["y1"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("Invalid caption area.")
    if not (0 <= y0 < y1 <= 1 and y1 - y0 >= 0.03):
        raise ValueError("The caption area must be a band of at least 3% of the frame height.")
    return {"on": bool(c.get("on")), "auto": bool(c.get("auto", True)), "region": {"y0": y0, "y1": y1}, "ui": c.get("ui")}


def captions_cfg(c):
    """{y0, y1, auto} when captions are switched on in setting c, else None. y0/y1 is the manual strip, used when
    auto-detection is off or finds no captions."""
    if not c or not c.get("on"):
        return None
    r = c.get("region") or {}
    return {"y0": float(r.get("y0", DEFAULT_CAPTION_REGION[0])), "y1": float(r.get("y1", DEFAULT_CAPTION_REGION[1])),
            "auto": bool(c.get("auto", True))}


def ass_time(t):
    cs = int(round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def build_ass(path, width, height, cues, y0, y1):
    """Write an .ass subtitle file with the English cues, centred in the caption strip."""
    strip_h = (y1 - y0) * height
    size = max(14, int(min(strip_h * 0.5, height * 0.05)))
    margin_v = int(height * (1 - y1) + strip_h * 0.15)
    side = int(width * 0.06)
    head = ("[Script Info]\nScriptType: v4.00+\nWrapStyle: 0\nScaledBorderAndShadow: yes\n"
            f"PlayResX: {width}\nPlayResY: {height}\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
            "MarginR, MarginV, Encoding\n"
            f"Style: Cap,Arial,{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,"
            f"{max(1.5, size / 14):.1f},0,2,{side},{side},{margin_v},1\n\n[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    lines = []
    for c in cues:
        text = (c.get("en") or "").replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ").strip()
        if text:
            lines.append(f"Dialogue: 0,{ass_time(c['start'])},{ass_time(c['end'])},Cap,,0,0,0,,{text}")
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n".join(lines) + "\n")
    return len(lines)


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_srt(path, cues):
    """English line plus the original Chinese, so the translation can be reviewed and corrected."""
    with open(path, "w", encoding="utf-8") as f:
        for i, c in enumerate((c for c in cues if (c.get("en") or "").strip()), 1):
            f.write(f"{i}\n{srt_time(c['start'])} --> {srt_time(c['end'])}\n{c['en'].strip()}\n{c.get('zh', '')}\n\n")


def run_captions_tool(job, args, cancelable=True):
    """Run audio/captions.py, mapping its STAGE / PROGRESS lines onto the job. Returns nothing; raises on failure."""
    proc = subprocess.Popen([AUDIO_PY, CAPTIONS_PY, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    err_lines = []
    threading.Thread(target=lambda: err_lines.extend(proc.stderr.read().splitlines()[-8:]), daemon=True).start()
    started = time.time()
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("STAGE "):
            job.update(stage=line[6:])
        elif line.startswith("PROGRESS "):
            pct = float(line.split()[1])
            job.update(percent=pct, eta=(time.time() - started) * (100 - pct) / pct if pct >= 3 else None)
        if cancelable and job.get("cancel"):
            proc.kill()
    rc = proc.wait()
    if job.get("cancel"):
        raise DownloadCancelled()
    if rc != 0:
        tail = [l for l in err_lines if l.strip() and "UserWarning" not in l and "warnings.warn" not in l]
        raise RuntimeError("Caption step failed: " + " | ".join(tail[-3:]))


def read_captions(job, path, cfg):
    """Read + translate the burned-in captions of `path` (needed for both the English captions and the English voice).
    cfg = {y0, y1, auto}. Returns {data, band, cues_path}, or None if the video has no Chinese captions."""
    if not captions_available():
        raise RuntimeError("Caption replacement is not installed yet.")
    out_json = os.path.join(job["dir"], "cues.json")
    job.update(stage="Reading captions", percent=0, speed=0, eta=None, downloaded=0, total=0)
    args = ["--video", path, "--y0", str(cfg["y0"]), "--y1", str(cfg["y1"]), "--out", out_json]
    run_captions_tool(job, args + (["--auto"] if cfg.get("auto") else []))
    with open(out_json, encoding="utf-8") as f:
        data = json.load(f)
    if not any((c.get("en") or "").strip() for c in data["cues"]):
        job["note"] = "No Chinese captions were found in this video, so its captions and voice were left as they are."
        return None
    write_srt(os.path.join(job["dir"], "captions.en.srt"), data["cues"])
    y0, y1 = data.get("band") or (cfg["y0"], cfg["y1"])  # where the captions really are in this video
    return {"data": data, "band": (y0, y1), "cues_path": out_json}


def caption_overlay(job, info):
    """Subtitle file for render_video, from read_captions()'s result."""
    y0, y1 = info["band"]
    d = info["data"]
    build_ass(os.path.join(job["dir"], "captions.ass"), d["width"], d["height"], d["cues"], y0, y1)
    return {"ass": "captions.ass", "y0": y0, "y1": y1}


# ---------------------------------------------------------------- English voice (replaces the spoken Chinese)
# audio/dub.py (Kokoro TTS) speaks each translated caption at its subtitle time over the effects (and music) from the
# separation step. The original speech is muted only while a line is being spoken.

DUB_PY = os.path.join(BASE, "audio", "dub.py")
TTS_DIR = os.path.join(AUDIO_HOME, "tts")
VOICES = {  # male voices of the Kokoro model: id -> label
    "am_michael": "Michael (US, warm and clear)", "am_fenrir": "Fenrir (US, deep)", "am_adam": "Adam (US, deep)",
    "am_eric": "Eric (US)", "am_liam": "Liam (US)", "am_onyx": "Onyx (US, low)", "am_puck": "Puck (US, light)",
    "am_echo": "Echo (US)", "bm_george": "George (UK, classic)", "bm_daniel": "Daniel (UK)",
    "bm_lewis": "Lewis (UK)", "bm_fable": "Fable (UK, storyteller)",
}
VOICES_F = {  # female voices of the same model, used for lines the original female speaker says
    "af_heart": "Heart (US, warm)", "af_bella": "Bella (US)", "af_nicole": "Nicole (US, soft)", "af_sarah": "Sarah (US)",
    "af_sky": "Sky (US)", "af_nova": "Nova (US)", "af_river": "River (US)", "af_aoede": "Aoede (US)",
    "af_kore": "Kore (US)", "af_jessica": "Jessica (US)", "af_alloy": "Alloy (US)", "bf_emma": "Emma (UK)",
    "bf_isabella": "Isabella (UK)", "bf_alice": "Alice (UK)", "bf_lily": "Lily (UK)",
}
ALL_VOICES = {**VOICES, **VOICES_F}
_dub_ok = {"t": 0.0, "ok": False}


def dub_available():
    """True once the voice library and model files are installed (dubbing also needs captions + music tools)."""
    model, voices = os.path.join(TTS_DIR, "kokoro-v1.0.onnx"), os.path.join(TTS_DIR, "voices-v1.0.bin")
    return tool_ready(_dub_ok, lambda: (os.path.isfile(AUDIO_PY) and os.path.isfile(DUB_PY) and os.path.isfile(voices)
                                        and os.path.isfile(model) and os.path.getsize(model) > 300_000_000 and HAS_FFMPEG),
                      "import kokoro_onnx, torchaudio")


def clean_dub(c):
    """Validate the dub setting {on, voice, speed}; returns the value to store (or None)."""
    if not c:
        return None
    voice, voice_f = c.get("voice") or "am_michael", c.get("voice_f") or "af_heart"
    try:
        speed = float(c.get("speed", 1.0))
    except (TypeError, ValueError):
        raise ValueError("Invalid voice speed.")
    mode = dub_mode(c)
    if voice not in VOICES or voice_f not in VOICES_F or not 0.7 <= speed <= 1.3:
        raise ValueError("Invalid voice or speed.")
    return {"on": bool(c.get("on")), "voice": voice, "voice_f": voice_f, "mode": mode, "speed": speed}


def dub_mode(c):
    """Who speaks: 'auto' (male voice for men; the female voice only when the speakers are women), or always 'male' /
    'female'. Older saved settings only had a yes/no 'match' flag."""
    mode = c.get("mode")
    if mode in ("auto", "male", "female"):
        return mode
    return "auto" if c.get("match", True) else "male"


def dub_cfg(c, force=False):
    """{voice, voice_f, mode, speed} when dubbing is switched on in setting c (or forced), else None."""
    if not c or not (c.get("on") or force):
        return None
    return {"voice": c.get("voice") if c.get("voice") in VOICES else "am_michael",
            "voice_f": c.get("voice_f") if c.get("voice_f") in VOICES_F else "af_heart",
            "mode": dub_mode(c), "speed": float(c.get("speed") or 1.0)}


# ---------------------------------------------------------------- downloading

def download_with_mirrors(url, opts, job):
    last = None
    for host in MIRRORS:
        # wipe partial files from the previous attempt
        for f in os.listdir(job["dir"]):
            os.remove(os.path.join(job["dir"], f))
        job.update(stage="Downloading clip" if "download_ranges" in opts else "Starting", percent=0)
        try:
            info = get_info(url)
            for f in info["formats"]:
                f["url"] = with_mirror(f["url"], host)
            with YoutubeDL(opts) as ydl:
                ydl.process_ie_result(info, download=True)
            return
        except DownloadError as e:
            last = e
    raise last


def process_video(url, quality, rng, crop, job, no_music=False, captions=None, dub=None):
    """Download (and optionally trim/crop/translate captions/replace the voice/remove music) into job['dir'].
    captions = {y0, y1, auto} or None; dub = {voice, speed} or None. Returns (path, meta)."""
    if (no_music or dub) and not music_available():
        raise RuntimeError("Music removal is turned on but is not installed yet. Turn it off in Settings, or finish its setup.")
    if (captions or dub) and quality != "audio" and not captions_available():
        raise RuntimeError("Caption replacement is turned on but is not installed yet. Turn it off in Settings.")
    if dub and not dub_available():
        raise RuntimeError("The English voice is turned on but is not installed yet. Turn it off in Settings.")
    info = get_info(url)
    meta = {"bvid": bvid_of(info), "title": info.get("title"), "uploader": info.get("uploader"),
            "uploader_id": str(info.get("uploader_id") or "")}

    def hook(d):
        if job.get("cancel"):
            raise DownloadCancelled()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            kind = "audio" if d.get("info_dict", {}).get("vcodec") == "none" else "video"
            job.update(
                stage=f"Downloading {kind}",
                percent=round(done * 100 / total, 1) if total else 0,
                speed=d.get("speed") or 0,
                eta=d.get("eta"),
                downloaded=done,
                total=total,
            )
        elif d["status"] == "finished":
            job.update(stage="Processing", speed=0, eta=None)

    opts = {
        **base_opts(media=True),
        "outtmpl": os.path.join(job["dir"], "%(title).100s [%(id)s].%(ext)s"),
        "progress_hooks": [hook],
    }
    if quality == "audio":
        opts["format"] = "bestaudio/best"
        if HAS_FFMPEG:
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
    elif HAS_FFMPEG:
        opts["format"] = "bestvideo+bestaudio/best"
        if quality.isdigit():
            # "res" is the short side, so 720 means 720p for landscape and vertical videos alike
            opts["format_sort"] = [f"res:{quality}", "vcodec:h264", "acodec:aac"]
        opts["merge_output_format"] = "mp4"
    else:
        opts["format"] = "best"

    if rng and HAS_FFMPEG:
        # only the chosen section is fetched; re-encode at the cut points so they are frame-accurate
        opts["download_ranges"] = download_range_func(None, [(rng[0], rng[1] if rng[1] is not None else float("inf"))])
        opts["force_keyframes_at_cuts"] = True

    download_with_mirrors(url, opts, job)
    files = [f for f in os.listdir(job["dir"]) if not f.endswith((".part", ".ytdl"))]
    if not files:
        raise RuntimeError("Download produced no file.")
    path = os.path.join(job["dir"], files[0])
    video = quality != "audio" and HAS_FFMPEG
    # the English lines are needed for the on-screen captions and for the English voice
    info = read_captions(job, path, captions or {"y0": DEFAULT_CAPTION_REGION[0], "y1": DEFAULT_CAPTION_REGION[1],
                                                 "auto": True}) if (captions or dub) and video else None
    caps = caption_overlay(job, info) if captions and info else None
    if (crop or caps) and video:
        path = render_video(job, path, crop, caps)
    speak = dub if (dub and info) else None  # nothing to speak when the video has no captions
    if no_music or speak:
        path = rebuild_audio(job, path, audio_only=(quality == "audio"), dub=speak,
                             cues_path=info["cues_path"] if info else None, no_music=no_music)
    return path, meta


def store_file(path, meta, channel_id, folder, quality, crop, no_music=False):
    """Move a finished file into the download folder and record it in the library."""
    if channel_id is None and meta.get("uploader_id"):
        row = q("SELECT id FROM channels WHERE mid=?", (meta["uploader_id"],))
        channel_id = row[0]["id"] if row else None
    root = get_settings()["download_dir"]
    dest_dir = os.path.join(root, safe_name(folder))
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(path))
    if os.path.exists(dest):
        base, ext = os.path.splitext(dest)
        dest = f"{base} ({int(time.time())}){ext}"
    shutil.move(path, dest)
    srt = os.path.join(os.path.dirname(path), "captions.en.srt")
    if os.path.exists(srt):  # the translated captions, next to the video, for review
        shutil.move(srt, os.path.splitext(dest)[0] + ".en.srt")
    row = q("INSERT INTO downloads(bvid,title,path,size,quality,crop,channel_id,created_at,nomusic) "
            "VALUES(?,?,?,?,?,?,?,?,?) RETURNING id",
            (meta["bvid"], meta["title"], dest, os.path.getsize(dest), quality,
             json.dumps(crop) if crop else None, channel_id, time.time(), int(bool(no_music))))
    return dest, row[0]["id"]


# ---------------------------------------------------------------- single (quick) downloads

jobs = {}  # id -> dict(status, percent, speed, eta, stage, file, error, dir)


@app.get("/")
def index():
    return render_template("index.html", has_ffmpeg=HAS_FFMPEG)


@app.post("/api/info")
def info():
    url = (request.json or {}).get("url", "").strip()
    if not valid_url(url):
        return jsonify(error="Please enter a valid bilibili.com or b23.tv link."), 400
    try:
        data = get_info(url)
    except DownloadError as e:
        return jsonify(error=clean(e)), 400

    def short_side(f):
        return min(f["width"], f["height"]) if f.get("width") and f.get("height") else f.get("height")

    video_fmts = [f for f in data.get("formats", []) if f.get("height") and f.get("vcodec") != "none"]
    heights = sorted({short_side(f) for f in video_fmts}, reverse=True)
    top = max(video_fmts, key=short_side, default={})
    return jsonify(
        id=bvid_of(data),
        width=top.get("width"),
        height=top.get("height"),
        title=data.get("title"),
        uploader=data.get("uploader"),
        duration=int(data["duration"]) if data.get("duration") else None,
        thumbnail=data.get("thumbnail"),
        heights=heights,
    )


_frame_cache = {}


@app.get("/api/frame")
def frame():
    """One real video frame (JPEG) for the live crop preview."""
    url = request.args.get("url", "").strip()
    if not valid_url(url) or not HAS_FFMPEG:
        return jsonify(error="Invalid URL."), 400
    try:
        t = max(0.0, float(request.args.get("t", 0)))
    except ValueError:
        return jsonify(error="Invalid time."), 400
    try:
        return Response(grab_frame(url, t), mimetype="image/jpeg")
    except DownloadError as e:
        return jsonify(error=clean(e)), 400
    except RuntimeError as e:
        return jsonify(error=str(e)), 502


def grab_frame(url, t, width=720):
    """JPEG bytes of the video frame at time t, `width` pixels wide. Raises RuntimeError if it cannot be fetched."""
    key = (url, round(t), width)
    if key in _frame_cache:
        return _frame_cache[key]
    data = get_info(url)
    vids = sorted((f for f in data["formats"] if f.get("vcodec") not in (None, "none") and f.get("height")),
                  key=lambda f: min(f.get("width") or f["height"], f["height"]))
    if not vids:
        raise RuntimeError("No video stream.")
    # a stream that is just big enough for the requested width: small = fast seek
    pick = next((f for f in vids if min(f.get("width") or f["height"], f["height"]) >= (360 if width <= 720 else 720)), vids[-1])
    tried = set()
    for host in MIRRORS:
        src = with_mirror(pick["url"], host)
        if src in tried:
            continue
        tried.add(src)
        cmd = ["ffmpeg", "-v", "error", "-headers", "Referer: https://www.bilibili.com/\r\nUser-Agent: Mozilla/5.0\r\n",
               "-ss", str(t), "-i", src, "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "3", "-strict", "unofficial",
               "-f", "mjpeg", "pipe:1"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=45)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0 and r.stdout:
            if len(_frame_cache) > 60:
                _frame_cache.clear()
            _frame_cache[key] = r.stdout
            return r.stdout
    raise RuntimeError("Could not grab a frame.")


# ---- caption preview: one frame, captions read + translated, shown the way the finished video will look
cap_previews = {}  # id -> dict(status, stage, zh, en, error, dir)


def run_caption_preview(pid, url, t, y0, y1, auto=True):
    job = cap_previews[pid]
    try:
        if not captions_available():
            raise RuntimeError("Caption replacement is not installed yet.")
        job.update(stage="Fetching a frame")
        jpg = os.path.join(job["dir"], "frame.jpg")
        with open(jpg, "wb") as f:
            f.write(grab_frame(url, t, width=960))
        run_captions_tool(job, ["--image", jpg, "--y0", str(y0), "--y1", str(y1), "--out",
                                os.path.join(job["dir"], "cues.json")] + (["--auto"] if auto else []), False)
        with open(os.path.join(job["dir"], "cues.json"), encoding="utf-8") as f:
            data = json.load(f)
        y0, y1 = data.get("band") or (y0, y1)  # the position found on this frame
        cue = (data["cues"] or [{}])[0]
        job.update(zh=cue.get("zh", ""), en=cue.get("en", ""))
        if not cue.get("en"):
            job.update(status="done", stage="Done", percent=100, found=False)
            return
        build_ass(os.path.join(job["dir"], "captions.ass"), data["width"], data["height"], data["cues"], y0, y1)
        job.update(stage="Rendering the preview")
        vf = caption_filter(data["height"], y0, y1)
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", "frame.jpg", "-vf", vf, "-frames:v", "1", "-q:v", "3",
                            "-strict", "unofficial", "result.jpg"], cwd=job["dir"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        if r.returncode != 0:
            raise RuntimeError("Could not draw the preview: " + (r.stderr or "").strip()[-200:])
        job.update(status="done", stage="Done", percent=100, found=True)
    except Exception as e:
        job.update(status="error", error=clean(e))


@app.post("/api/captions-preview")
def captions_preview_start():
    body = request.json or {}
    url = (body.get("url") or "").strip()
    if not valid_url(url):
        return jsonify(error="Add a valid Bilibili video link first."), 400
    try:
        t = max(0.0, float(body.get("t") or 0))
        c = clean_captions({"on": True, "region": {"y0": body.get("y0"), "y1": body.get("y1")}})
    except (TypeError, ValueError) as e:
        return jsonify(error=str(e)), 400
    for old in list(cap_previews)[:-3]:
        shutil.rmtree(cap_previews.pop(old)["dir"], ignore_errors=True)
    pid = uuid.uuid4().hex
    cap_previews[pid] = {"status": "running", "stage": "Starting", "percent": 0, "eta": None, "zh": "", "en": "",
                         "error": None, "dir": tempfile.mkdtemp(prefix="cap_", dir=WORK_DIR)}
    threading.Thread(target=run_caption_preview, args=(pid, url, t, c["region"]["y0"], c["region"]["y1"],
                                                       bool(body.get("auto", True))), daemon=True).start()
    return jsonify(id=pid)


@app.get("/api/captions-preview/<pid>")
def captions_preview_progress(pid):
    job = cap_previews.get(pid)
    if not job:
        return jsonify(error="Unknown preview."), 404
    return jsonify({k: v for k, v in job.items() if k != "dir"})


@app.get("/api/captions-preview/<pid>/image")
def captions_preview_image(pid):
    job = cap_previews.get(pid)
    if not job or job["status"] != "done" or not job.get("found"):
        return jsonify(error="Not ready."), 404
    return send_file(os.path.join(job["dir"], "result.jpg"), mimetype="image/jpeg")


def run_job(job_id, url, quality, rng=None, crop=None, no_music=False, captions=None, dub=None):
    job = jobs[job_id]
    try:
        path, meta = process_video(url, quality, rng, crop, job, no_music, captions, dub)
        dest, did = store_file(path, meta, None, meta.get("uploader") or "Quick downloads", quality, crop, no_music)
        job.update(status="done", stage="Done", percent=100, file=dest, name=os.path.basename(dest))
    except DownloadCancelled:
        job.update(status="error", error="Cancelled.")
    except Exception as e:  # report any failure to the UI
        job.update(status="error", error=clean(e))
    finally:
        shutil.rmtree(job["dir"], ignore_errors=True)


@app.post("/api/start")
def start():
    body = request.json or {}
    url = body.get("url", "").strip()
    if not valid_url(url):
        return jsonify(error="Invalid URL."), 400
    try:
        start_s = float(body.get("start") or 0)
        end_s = float(body["end"]) if body.get("end") else None
        crop = normalize_crop(body.get("crop"))
    except (TypeError, ValueError) as e:
        return jsonify(error=str(e) if "crop" in str(e).lower() else "Invalid start/end time."), 400
    if start_s < 0 or (end_s is not None and end_s <= start_s):
        return jsonify(error="End time must be after the start time."), 400
    rng = (start_s, end_s) if (start_s > 0 or end_s is not None) else None
    job_id = uuid.uuid4().hex
    jobs[job_id] = {
        "status": "running", "stage": "Starting", "percent": 0, "speed": 0, "eta": None,
        "downloaded": 0, "total": 0, "dir": tempfile.mkdtemp(prefix="bili_", dir=WORK_DIR),
    }
    captions = None
    if body.get("captions"):  # Quick download uses the caption strip saved in Settings (or the default one)
        saved = get_settings().get("captions") or {}
        captions = captions_cfg({"on": True, "region": saved.get("region") or {}, "auto": saved.get("auto", True)})
    dub = dub_cfg(get_settings().get("dub"), force=True) if body.get("dub") else None  # voice chosen in Settings
    threading.Thread(target=run_job, args=(job_id, url, str(body.get("quality", "best")), rng, crop,
                                           bool(body.get("remove_music")), captions, dub), daemon=True).start()
    return jsonify(id=job_id)


@app.get("/api/progress/<job_id>")
def progress(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify(error="Unknown job."), 404
    return jsonify({k: v for k, v in job.items() if k not in ("dir", "file")})


@app.get("/api/file/<job_id>")
def file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify(error="File not ready."), 404
    resp = send_file(job["file"], as_attachment=True, download_name=job["name"])
    resp.call_on_close(lambda: jobs.pop(job_id, None))
    return resp


# ---------------------------------------------------------------- channels

sync_state = {}  # channel_id -> {phase, done, total, error}


def resolve_channel(text):
    """Return (mid, name) from a space link, a numeric user id, or any video link of that channel."""
    text = (text or "").strip()
    m = re.search(r"(?:space\.bilibili\.com|bilibili\.com/list)/(\d+)", text) or re.fullmatch(r"(\d{3,})", text)
    if m:
        return m.group(1), None
    if valid_url(text):
        data = get_info(text)
        if data.get("uploader_id"):
            return str(data["uploader_id"]), data.get("uploader")
    raise ValueError("Paste a channel link (space.bilibili.com/...), a numeric user ID, or any video link from "
                     "the channel. Bilibili blocks looking up channels by name from outside the website.")


def fetch_detail(row):
    try:
        with YoutubeDL({**base_opts(), "skip_download": True}) as ydl:
            for attempt in range(3):
                try:
                    d = ydl.extract_info(row["url"], download=False, process=False)
                    return row["bvid"], d
                except DownloadError as e:
                    err = e
                    time.sleep(2 * (attempt + 1))
        return row["bvid"], err
    except Exception as e:  # keep the sync going
        return row["bvid"], e


def sync_channel(cid):
    st = sync_state[cid] = {"phase": "Listing videos", "done": 0, "total": 0, "error": None, "running": True}
    try:
        ch = q("SELECT * FROM channels WHERE id=?", (cid,))[0]
        # Bilibili's risk control blocks some listing services while others still answer, so try each in turn.
        # The /list/ page (newest first) is the most reliable one.
        urls = [f"https://www.bilibili.com/list/{ch['mid']}?sort_field=pubtime",
                f"https://space.bilibili.com/{ch['mid']}/upload/video",
                f"https://space.bilibili.com/{ch['mid']}/video"]
        pl, last = None, None
        with YoutubeDL({**base_opts(), "skip_download": True, "extract_flat": "in_playlist"}) as ydl:
            for cycle in range(3):
                for u in urls:
                    st["phase"] = "Listing videos" if cycle == 0 else f"Listing videos (retry {cycle}, Bilibili is slowing us down)"
                    try:
                        pl = ydl.extract_info(u, download=False)
                        break
                    except DownloadError as e:
                        last = e
                if pl is not None:
                    break
                if cycle < 2:
                    time.sleep(20 * (cycle + 1))  # rate limits clear after a short wait
            if pl is None:
                if re.search(r"\((352|412)\)|too frequent|blocked|rejected", str(last)):
                    raise RuntimeError("Bilibili is temporarily rate-limiting requests from your connection "
                                       "(error 352/412). Wait a few minutes and press Sync again. If it keeps "
                                       "happening, add a Bilibili cookies file in Settings.")
                raise last
            entries = [e for e in pl.get("entries") or [] if e and e.get("id")]
        qmany("INSERT INTO videos(bvid,channel_id,url,position) VALUES(?,?,?,?) "
              "ON CONFLICT(bvid) DO UPDATE SET position=excluded.position, channel_id=excluded.channel_id",
              [(e["id"], cid, e.get("url") or f"https://www.bilibili.com/video/{e['id']}", i)
               for i, e in enumerate(entries)])
        todo = q("SELECT bvid, url FROM videos WHERE channel_id=? AND detail=0", (cid,))
        st.update(phase="Reading video details", total=len(todo))
        failures = 0
        with ThreadPoolExecutor(max_workers=2) as pool:
            for bvid, d in pool.map(fetch_detail, todo):
                if isinstance(d, Exception):
                    failures += 1
                else:
                    date = d.get("upload_date") or (time.strftime("%Y%m%d", time.localtime(d["timestamp"])) if d.get("timestamp") else None)
                    q("UPDATE videos SET title=?, thumbnail=?, duration=?, upload_date=?, detail=1 WHERE bvid=?",
                      (d.get("title"), d.get("thumbnail"), d.get("duration"), date, bvid))
                    if not ch.get("name") and d.get("uploader"):
                        ch["name"] = d["uploader"]
                        q("UPDATE channels SET name=? WHERE id=?", (ch["name"], cid))
                st["done"] += 1
        q("UPDATE channels SET synced_at=? WHERE id=?", (time.time(), cid))
        if failures:
            st["error"] = f"{failures} video(s) could not be read (private, removed or temporarily blocked). Sync again later."
    except Exception as e:
        st["error"] = clean(e)
    finally:
        st.update(phase="Done", running=False)


def start_sync(cid):
    if sync_state.get(cid, {}).get("running"):
        return
    sync_state[cid] = {"phase": "Starting", "done": 0, "total": 0, "error": None, "running": True}
    threading.Thread(target=sync_channel, args=(cid,), daemon=True).start()


def channel_rows():
    have = downloaded_bvids()
    out = []
    for c in q("SELECT * FROM channels ORDER BY added_at"):
        c["custom_settings"] = bool(c.pop("settings", None))
        vids = [r["bvid"] for r in q("SELECT bvid FROM videos WHERE channel_id=?", (c["id"],))]
        c["total"] = len(vids)
        c["downloaded"] = sum(1 for b in vids if b in have)
        c["sync"] = sync_state.get(c["id"])
        out.append(c)
    return out


@app.get("/api/channels")
def channels_list():
    return jsonify(channel_rows())


@app.post("/api/channels")
def channels_add():
    try:
        mid, name = resolve_channel((request.json or {}).get("input"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    except DownloadError as e:
        return jsonify(error=clean(e)), 400
    row = q("SELECT id FROM channels WHERE mid=?", (mid,))
    if row:
        cid = row[0]["id"]
    else:
        cid = q("INSERT INTO channels(mid,name,added_at) VALUES(?,?,?) RETURNING id", (mid, name, time.time()))[0]["id"]
    start_sync(cid)
    return jsonify(id=cid)


@app.post("/api/channels/<int:cid>/sync")
def channels_sync(cid):
    if not q("SELECT id FROM channels WHERE id=?", (cid,)):
        return jsonify(error="Unknown channel."), 404
    start_sync(cid)
    return jsonify(ok=True)


@app.delete("/api/channels/<int:cid>")
def channels_delete(cid):
    q("DELETE FROM videos WHERE channel_id=?", (cid,))
    q("UPDATE downloads SET channel_id=NULL WHERE channel_id=?", (cid,))
    q("DELETE FROM channels WHERE id=?", (cid,))
    sync_state.pop(cid, None)
    return jsonify(ok=True)


@app.get("/api/channels/<int:cid>/settings")
def channel_settings_get(cid):
    rows = q("SELECT settings FROM channels WHERE id=?", (cid,))
    if not rows:
        return jsonify(error="Unknown channel."), 404
    eff = effective_settings(cid)
    return jsonify(override=json.loads(rows[0]["settings"]) if rows[0]["settings"] else None,
                   effective={k: eff.get(k) for k in CHANNEL_KEYS}, music_available=music_available(),
                   captions_available=captions_available(), dub_available=dub_available(), voices=VOICES,
                   voices_f=VOICES_F)


@app.put("/api/channels/<int:cid>/settings")
def channel_settings_put(cid):
    """Body {override: {quality, crop, remove_music}} sets this channel's own settings; {override: null} goes back
    to the global ones."""
    if not q("SELECT id FROM channels WHERE id=?", (cid,)):
        return jsonify(error="Unknown channel."), 404
    ov = (request.json or {}).get("override")
    if ov is None:
        q("UPDATE channels SET settings=NULL WHERE id=?", (cid,))
        return jsonify(ok=True)
    quality = str(ov.get("quality", "best"))
    if quality not in ("best", "audio") and not quality.isdigit():
        return jsonify(error="Invalid quality."), 400
    crop = ov.get("crop")
    if crop:
        try:
            normalize_crop(crop.get("spec"))  # validate only; stored as sent so the page can restore it
        except ValueError as e:
            return jsonify(error=str(e)), 400
        crop = {"on": bool(crop.get("on")), "spec": crop.get("spec"), "ui": crop.get("ui")}
    try:
        captions = clean_captions(ov.get("captions"))
        dub = clean_dub(ov.get("dub"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    q("UPDATE channels SET settings=? WHERE id=?",
      (json.dumps({"quality": quality, "crop": crop or None, "remove_music": bool(ov.get("remove_music")),
                   "captions": captions, "dub": dub}), cid))
    return jsonify(ok=True)


@app.get("/api/channels/<int:cid>/videos")
def channel_videos(cid):
    have, on_disk = downloaded_bvids(), bvids_with_file()
    rows = q("SELECT bvid,url,title,thumbnail,duration,upload_date FROM videos WHERE channel_id=? ORDER BY position", (cid,))
    for r in rows:
        r["downloaded"] = r["bvid"] in have
        r["file_exists"] = r["bvid"] in on_disk  # False = downloaded before, file since deleted
    return jsonify(rows)


# ---------------------------------------------------------------- download queue (one video at a time)

queue_items = []  # bvids waiting, in order
qstate = {}       # bvid -> status dict (queued / downloading / error)
q_cond = threading.Condition()
q_version = 0     # bumps whenever a video finishes so the page knows to refresh


def enqueue(bvids):
    n = 0
    with q_cond:
        for b in bvids:
            st = qstate.get(b)
            if st and st["status"] in ("queued", "downloading"):
                continue
            qstate[b] = {"status": "queued", "stage": "Queued", "percent": 0, "speed": 0, "eta": None,
                         "downloaded": 0, "total": 0, "error": None}
            queue_items.append(b)
            n += 1
        q_cond.notify()
    return n


def queue_worker():
    global q_version
    while True:
        with q_cond:
            while not queue_items:
                q_cond.wait()
            bvid = queue_items.pop(0)
            st = qstate.get(bvid)
            if not st or st["status"] != "queued":
                continue
            st["status"] = "downloading"
        row = q("SELECT v.url, v.title, v.channel_id, c.name AS channel FROM videos v "
                "LEFT JOIN channels c ON c.id=v.channel_id WHERE v.bvid=?", (bvid,))
        s = effective_settings(row[0]["channel_id"] if row else None)  # this channel's own settings, if it has any
        crop = None
        try:
            if s.get("crop") and s["crop"].get("on"):
                crop = normalize_crop(s["crop"].get("spec"))
            if not row:
                raise RuntimeError("Video is no longer in the list.")
            row = row[0]
            st["dir"] = tempfile.mkdtemp(prefix="bili_", dir=WORK_DIR)
            no_music = bool(s.get("remove_music"))
            path, meta = process_video(row["url"], s["quality"], None, crop, st, no_music, captions_cfg(s.get("captions")),
                                       dub_cfg(s.get("dub")))
            store_file(path, meta, row["channel_id"], row["channel"] or "Unknown channel", s["quality"], crop, no_music)
            with q_cond:
                qstate.pop(bvid, None)
                q_version += 1
        except DownloadCancelled:
            with q_cond:
                qstate.pop(bvid, None)
        except Exception as e:
            st.update(status="error", stage="Error", error=clean(e))
        finally:
            if st.get("dir"):
                shutil.rmtree(st["dir"], ignore_errors=True)
        time.sleep(1)  # be gentle with Bilibili between videos


@app.get("/api/queue")
def queue_get():
    with q_cond:
        items = {b: {k: v for k, v in st.items() if k not in ("dir", "cancel")} for b, st in qstate.items()}
        return jsonify(items=items, version=q_version, order=list(queue_items))


@app.post("/api/queue")
def queue_add():
    body = request.json or {}
    if body.get("channel_id") is not None:
        have = downloaded_bvids()
        rows = q("SELECT bvid FROM videos WHERE channel_id=? ORDER BY position", (int(body["channel_id"]),))
        bvids = [r["bvid"] for r in rows if r["bvid"] not in have]
    else:
        bvids = [b for b in body.get("bvids", []) if isinstance(b, str)]
    return jsonify(added=enqueue(bvids))


@app.post("/api/queue/cancel")
def queue_cancel():
    bvid = (request.json or {}).get("bvid")
    with q_cond:
        for b in ([bvid] if bvid else list(qstate)):
            st = qstate.get(b)
            if not st:
                continue
            if st["status"] == "downloading":
                st["cancel"] = True
            else:  # queued or failed: just forget it
                qstate.pop(b, None)
                if b in queue_items:
                    queue_items.remove(b)
    return jsonify(ok=True)


# ---------------------------------------------------------------- library

@app.get("/api/library")
def library():
    rows = q("SELECT d.*, c.name AS channel FROM downloads d LEFT JOIN channels c ON c.id=d.channel_id "
             "ORDER BY d.created_at DESC")
    for r in rows:
        r["exists"] = bool(r["path"] and os.path.exists(r["path"]))
        r["folder"] = os.path.basename(os.path.dirname(r["path"] or ""))
        r["crop"] = json.loads(r["crop"]) if r["crop"] else None
    return jsonify(rows)


def library_row(did):
    rows = q("SELECT * FROM downloads WHERE id=?", (did,))
    return rows[0] if rows else None


@app.get("/api/library/<int:did>/file")
def library_file(did):
    r = library_row(did)
    if not r or not os.path.exists(r["path"]):
        return jsonify(error="File not found on disk."), 404
    return send_file(r["path"], as_attachment=True, download_name=os.path.basename(r["path"]))


@app.post("/api/library/<int:did>/reveal")
def library_reveal(did):
    r = library_row(did)
    if not r or not os.path.exists(r["path"]):
        return jsonify(error="File not found on disk."), 404
    if os.name != "nt":
        return jsonify(error="Only supported on Windows."), 501
    subprocess.Popen(["explorer", f"/select,{os.path.normpath(r['path'])}"])
    return jsonify(ok=True)


@app.delete("/api/library/<int:did>")
def library_delete(did):
    """?file=1 deletes the file from disk but keeps the entry, so the video stays marked as downloaded.
    ?forget=1 removes the entry (the video is then no longer marked as downloaded). Neither parameter means forget."""
    r = library_row(did)
    delete_file = request.args.get("file") == "1"
    forget = request.args.get("forget") == "1" or not delete_file
    if r and delete_file and r["path"] and os.path.exists(r["path"]):
        try:
            os.remove(r["path"])
            srt = os.path.splitext(r["path"])[0] + ".en.srt"
            if os.path.exists(srt):
                os.remove(srt)
        except OSError as e:
            return jsonify(error=f"Could not delete the file: {e}"), 400
    if forget:
        q("DELETE FROM downloads WHERE id=?", (did,))
    return jsonify(ok=True)


# ---------------------------------------------------------------- music-removal preview (short before/after clip)

previews = {}  # id -> dict(status, stage, percent, eta, error, dir)


def run_preview(pid, url, t, length=15):
    job = previews[pid]
    try:
        info = get_info(url)
        audio = [f for f in info["formats"] if f.get("vcodec") in (None, "none") and f.get("acodec") not in (None, "none")]
        if not audio:
            raise RuntimeError("No audio stream found for this video.")
        pick = max(audio, key=lambda f: f.get("abr") or 0)
        wav_in = os.path.join(job["dir"], "original.wav")
        job.update(stage="Fetching a short clip")
        last = None
        for host in MIRRORS:
            try:
                run_ffmpeg(["-headers", "Referer: https://www.bilibili.com/\r\nUser-Agent: Mozilla/5.0\r\n",
                            "-ss", str(t), "-t", str(length), "-i", with_mirror(pick["url"], host),
                            "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", wav_in])
                last = None
                break
            except RuntimeError as e:
                last = e
        if last:
            raise last
        job.update(stage="Removing music", percent=0)
        run_separation(job, wav_in, os.path.join(job["dir"], "clean.wav"))
        job.update(status="done", stage="Done", percent=100, eta=None)
    except Exception as e:
        job.update(status="error", error=clean(e))


@app.post("/api/music-preview")
def music_preview_start():
    body = request.json or {}
    url = (body.get("url") or "").strip()
    if not valid_url(url):
        return jsonify(error="Add a valid Bilibili video link first."), 400
    if not music_available():
        return jsonify(error="Music removal is not installed yet."), 400
    try:
        t = max(0.0, float(body.get("t") or 0))
    except ValueError:
        return jsonify(error="Invalid time."), 400
    for old in list(previews)[:-2]:  # keep only the latest few previews on disk
        shutil.rmtree(previews.pop(old)["dir"], ignore_errors=True)
    pid = uuid.uuid4().hex
    previews[pid] = {"status": "running", "stage": "Starting", "percent": 0, "eta": None, "error": None,
                     "dir": tempfile.mkdtemp(prefix="prev_", dir=WORK_DIR)}
    threading.Thread(target=run_preview, args=(pid, url, t), daemon=True).start()
    return jsonify(id=pid)


@app.get("/api/music-preview/<pid>")
def music_preview_progress(pid):
    job = previews.get(pid)
    if not job:
        return jsonify(error="Unknown preview."), 404
    return jsonify({k: v for k, v in job.items() if k != "dir"})


@app.get("/api/music-preview/<pid>/audio/<which>")
def music_preview_audio(pid, which):
    job = previews.get(pid)
    if not job or job["status"] != "done" or which not in ("original", "clean"):
        return jsonify(error="Not ready."), 404
    return send_file(os.path.join(job["dir"], which + ".wav"), mimetype="audio/wav")


# ---------------------------------------------------------------- settings

@app.get("/api/settings")
def settings_get():
    return jsonify({**get_settings(), "music_available": music_available(), "captions_available": captions_available(),
                    "dub_available": dub_available(), "voices": VOICES, "voices_f": VOICES_F,
                    "network": {**net_state(), "direct_lookups": lookup_address()}})


@app.get("/api/voice-sample")
def voice_sample():
    """A spoken sample of one of the English voices, so it can be heard before choosing it."""
    voice = request.args.get("voice", "am_michael")
    if voice not in ALL_VOICES:
        return jsonify(error="Unknown voice."), 400
    if not dub_available():
        return jsonify(error="The English voice is not installed yet."), 400
    path = os.path.join(WORK_DIR, f"voice_{voice}.wav")
    if not os.path.exists(path):
        r = subprocess.run([AUDIO_PY, DUB_PY, "--sample", path, "--voice", voice], capture_output=True, timeout=180,
                           creationflags=NO_WINDOW)
        if r.returncode != 0 or not os.path.exists(path):
            return jsonify(error="Could not make the sample."), 500
    return send_file(path, mimetype="audio/wav")


@app.put("/api/settings")
def settings_put():
    body = request.json or {}
    new = {}
    quality = str(body.get("quality", "best"))
    if quality not in ("best", "audio") and not quality.isdigit():
        return jsonify(error="Invalid quality."), 400
    new["quality"] = quality
    folder = os.path.abspath(os.path.expanduser(str(body.get("download_dir") or DEFAULT_DL)))
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        return jsonify(error=f"Cannot use that download folder: {e}"), 400
    new["download_dir"] = folder
    new["preview_url"] = str(body.get("preview_url") or "")
    new["remove_music"] = bool(body.get("remove_music"))
    new["lookup_mode"] = "default" if body.get("lookup_mode") == "default" else "auto"
    try:
        new["captions"] = clean_captions(body.get("captions"))
        new["dub"] = clean_dub(body.get("dub"))
    except ValueError as e:
        return jsonify(error=str(e)), 400
    cookies = str(body.get("cookies_file") or "").strip().strip('"')
    if cookies and not os.path.isfile(cookies):
        return jsonify(error="Cookies file not found at that path."), 400
    new["cookies_file"] = cookies
    _cookie_cache["t"] = 0
    crop = body.get("crop")
    if crop:
        try:
            normalize_crop(crop.get("spec"))  # validate only; stored as sent so the UI can restore it
        except ValueError as e:
            return jsonify(error=str(e)), 400
        crop = {"on": bool(crop.get("on")), "spec": crop.get("spec"), "ui": crop.get("ui")}
    new["crop"] = crop or None
    for k, v in new.items():
        q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
          (k, json.dumps(v)))
    return jsonify({**get_settings(), "music_available": music_available()})


init_db()
threading.Thread(target=queue_worker, daemon=True).start()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("BILI_PORT", 5000)), debug=False, threaded=True)
