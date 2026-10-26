"""Workaround for Bilibili's new video pages.

Bilibili now serves video pages as an empty shell: the video's details used to be in the page as
`window.__INITIAL_STATE__ = {...}` and that assignment is gone, so yt-dlp stops with
"Unable to extract initial state". The same details are still available from Bilibili's own data service
(`wbi/view/detail`), which yt-dlp already calls. When the page has no initial state, this fetches the details
from there and puts them where yt-dlp expects them.

If Bilibili brings the old pages back (or yt-dlp is fixed), the page contains the state again and this does nothing.
"""
import json
import re

try:
    from yt_dlp.extractor import bilibili as _bilibili
    from yt_dlp.utils import traverse_obj
except Exception:  # yt-dlp missing or restructured: leave everything as it is
    _bilibili = None

STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=")
RISK_RE = re.compile(r"window\._riskdata_\s*=")


def install():
    """Patch yt-dlp's Bilibili video extractor. Returns True if the patch is active."""
    if _bilibili is None or getattr(_bilibili.BiliBiliIE, "_bili_patch", False):
        return _bilibili is not None
    original = _bilibili.BiliBiliIE._download_webpage_handle

    def patched(self, url_or_request, video_id, *args, **kwargs):
        webpage, urlh = original(self, url_or_request, video_id, *args, **kwargs)
        try:
            if not isinstance(url_or_request, str) or STATE_RE.search(webpage) or RISK_RE.search(webpage):
                return webpage, urlh  # normal page, or a rate-limit page that yt-dlp reports itself
            match = self._match_valid_url(url_or_request)
            if not match:
                return webpage, urlh
            prefix, vid = match.group("prefix").upper(), match.group("id")
            query = {"platform": "web", ("bvid" if prefix == "BV" else "aid"): (prefix + vid if prefix == "BV" else vid)}
            headers = {**self.geo_verification_headers(), "Referer": "https://www.bilibili.com/",
                       "Origin": "https://www.bilibili.com"}
            detail = self._download_json(
                "https://api.bilibili.com/x/web-interface/wbi/view/detail", video_id,
                note="Page has no video data; asking Bilibili's data service instead",
                errnote="Failed to download video details", fatal=False,
                query=self._sign_wbi(query, video_id), headers=headers)
            data = traverse_obj(detail, ("data", {dict}))
            view = traverse_obj(data, ("View", {dict}))
            if not view or not view.get("bvid"):
                return webpage, urlh
            state = {
                "videoData": view,
                "upData": traverse_obj(data, ("Card", "card", {dict})) or {},
                "tags": traverse_obj(data, ("Tags", {list})) or [],
            }
            return webpage + "<script>window.__INITIAL_STATE__=" + json.dumps(state, ensure_ascii=False) + ";</script>", urlh
        except Exception:
            return webpage, urlh  # never make things worse than the unpatched behaviour

    _bilibili.BiliBiliIE._download_webpage_handle = patched
    _bilibili.BiliBiliIE._bili_patch = True
    return True
