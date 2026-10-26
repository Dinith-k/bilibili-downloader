"""Unpack dist/bilibili-downloader.zip into a throwaway folder and check that the unpacked app can read a video."""
import os
import shutil
import sys
import tempfile
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ZIP = os.path.join(ROOT, "dist", "bilibili-downloader.zip")
url = sys.argv[1] if len(sys.argv) > 1 else "https://www.bilibili.com/video/BV1U8PszCE3x"

tmp = tempfile.mkdtemp(prefix="zt_", dir=os.path.expanduser("~"))  # short path, outside the project
try:
    zipfile.ZipFile(ZIP).extractall(tmp)
    app_dir = os.path.join(tmp, "bilibili-downloader")
    sys.path.insert(0, app_dir)
    os.chdir(app_dir)
    import app  # the unpacked copy; this also installs the Bilibili page workaround

    print("unpacked copy loaded from:", os.path.dirname(app.__file__) == app_dir and "the zip (not the project)" or app.__file__)
    print("workaround installed:", getattr(app.bili_patch, "install")())
    t = time.time()
    info = app.get_info(url)
    print("read the video:", (info.get("title") or "")[:30], "|", info.get("uploader"), "|", len(info.get("formats", [])), "streams |",
          "%.1fs" % (time.time() - t))
finally:
    os.chdir(ROOT)
    shutil.rmtree(tmp, ignore_errors=True)
print("temporary copy removed:", not os.path.exists(tmp))
