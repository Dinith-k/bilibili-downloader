"""Build dist/bilibili-downloader.zip: the app + launcher only (no personal data, no big models).

Run from anywhere:  python tools/build_zip.py
"""
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "dist", "bilibili-downloader.zip")
TOP = "bilibili-downloader"

FILES = [
    "app.py", "bili_patch.py", "requirements.txt", "run.bat", "start.ps1", "README.md", "THIRD_PARTY.md", "LICENSE",
    "templates/index.html", "static/app.js",
    "audio/separate.py", "audio/captions.py", "audio/dub.py",
    "models/config_dnr_bandit.json",
    "tools/sep/LICENSE",
]
DIRS = ["tools/sep/models/bandit"]  # only the model code the music remover imports
TEXT_CRLF = {".bat", ".ps1"}         # Windows scripts want CRLF; the .ps1 also gets a UTF-8 BOM for Windows PowerShell 5

os.makedirs(os.path.dirname(OUT), exist_ok=True)
names = list(FILES)
for d in DIRS:
    for base, dirs, files in os.walk(os.path.join(ROOT, d)):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for f in files:
            if not f.endswith(".pyc"):
                names.append(os.path.relpath(os.path.join(base, f), ROOT).replace("\\", "/"))

with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
    for name in names:
        data = open(os.path.join(ROOT, name), "rb").read()
        ext = os.path.splitext(name)[1].lower()
        if ext in TEXT_CRLF:
            data = data.replace(b"\xef\xbb\xbf", b"").replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
            if ext == ".ps1":
                data = b"\xef\xbb\xbf" + data
        z.writestr(f"{TOP}/{name}", data)

print(f"{len(names)} files -> {OUT} ({os.path.getsize(OUT) / 1024:.0f} KB)")
