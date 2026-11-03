import re
import time
import urllib.parse
import requests
import yt_dlp
from curl_cffi import requests as cffi_requests
from flask import Flask, request, jsonify

app = Flask(__name__)

LOADER = "https://loader.to"
LOADER_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Origin": LOADER,
    "Referer": LOADER + "/",
    "X-Requested-With": "XMLHttpRequest",
}
LOADER_MAX_SECONDS = 25


def loader_get(path, params, timeout=12):
    return cffi_requests.get(
        LOADER + path,
        params=params,
        headers=LOADER_HEADERS,
        impersonate="chrome",
        timeout=timeout,
    )


# ---------- يوتيوب: تحويل النص إلى رابط ----------
def get_youtube_url(q):
    if "youtube.com" in q or "youtu.be" in q:
        m = re.search(
            r"(?:v=|/v/|embed/|youtu\.be/|/shorts/)([a-zA-Z0-9_-]{11})", q)
        if m:
            return "https://www.youtube.com/watch?v=" + m.group(1)
    try:
        url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q)
        r = cffi_requests.get(url, impersonate="chrome", timeout=8)
        ids = re.findall(r"watch\?v=([a-zA-Z0-9_-]{11})", r.text)
        if ids:
            return "https://www.youtube.com/watch?v=" + ids[0]
    except Exception:
        pass
    try:
        opts = {"quiet": True, "no_warnings": True,
                "extract_flat": True, "socket_timeout": 8}
        with yt_dlp.YoutubeDL(opts) as ydl:
            res = ydl.extract_info("ytsearch1:" + q, download=False)
        entries = (res or {}).get("entries") or []
        if entries and entries[0].get("id"):
            return "https://www.youtube.com/watch?v=" + entries[0]["id"]
    except Exception:
        pass
    return None


def url_is_valid(url):
    if not url or not url.startswith("http"):
        return False
    try:
        r = requests.head(url, allow_redirects=True, timeout=8,
                          headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code in (200, 206, 302)
    except Exception:
        return False


# ---------- المشغل: loader.to ----------
def player_loader(q):
    yt_url = get_youtube_url(q)
    if not yt_url:
        return None, "youtube: no results"

    r = loader_get("/ajax/download.php", {"format": "mp3", "url": yt_url})
    r.raise_for_status()
    data = r.json()
    if data.get("success") not in (True, 1):
        return None, "loader: request rejected"
    job_id = data.get("id")
    if not job_id:
        return None, "loader: no job id"

    start = time.time()
    while time.time() - start < LOADER_MAX_SECONDS:
        time.sleep(2)
        pr = loader_get("/ajax/progress.php", {"id": job_id}, timeout=10)
        if pr.status_code != 200:
            continue
        p = pr.json()
        if p.get("success") == 1:
            link = p.get("download_url")
            if url_is_valid(link):
                try:
                    duration = int(p.get("duration") or 0)
                except (ValueError, TypeError):
                    duration = 0
                return {
                    "title": p.get("title") or q,
                    "url": link,
                    "duration": duration,
                    "track_url": yt_url,
                    "source": "Loader",
                }, None
    return None, "loader: timeout"


# ---------- المسارات ----------
@app.route("/")
def home():
    return "OK"


@app.route("/api/song")
@app.route("/play")
def song():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "missing q"}), 400
    try:
        result, err = player_loader(q)
    except Exception as e:
        return jsonify({"ok": False, "error": repr(e)}), 502
    if not result:
        return jsonify({"ok": False, "error": err}), 404
    return jsonify({"ok": True, **result})
