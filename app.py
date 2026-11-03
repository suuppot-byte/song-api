import re
import time
import urllib.parse
import requests
import yt_dlp
from flask import Flask, request, jsonify
from sc_player import sc_player_resolve

app = Flask(__name__)

LOADER = "https://loader.to"
LOADER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Origin": LOADER,
    "Referer": LOADER + "/",
}
LOADER_MAX_SECONDS = 25


# ---------- يوتيوب: تحويل النص إلى رابط ----------
def get_youtube_url(q):
    if "youtube.com" in q or "youtu.be" in q:
        m = re.search(
            r"(?:v=|/v/|embed/|youtu\.be/|/shorts/)([a-zA-Z0-9_-]{11})", q)
        if m:
            return "https://www.youtube.com/watch?v=" + m.group(1)
    try:
        url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q)
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        ids = re.findall(r"watch\?v=(\S{11})", r.text)
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


# ---------- المشغل 1: loader.to ----------
def player_loader(q):
    yt_url = get_youtube_url(q)
    if not yt_url:
        return None, "youtube: no results"

    r = requests.get(LOADER + "/ajax/download.php",
                     params={"format": "mp3", "url": yt_url},
                     headers=LOADER_HEADERS, timeout=12)
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
        pr = requests.get(LOADER + "/ajax/progress.php",
                          params={"id": job_id},
                          headers=LOADER_HEADERS, timeout=10)
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
            return None, "loader: broken download link"
        if p.get("text") == "Error":
            return None, "loader: server error"
    return None, "loader: timeout"


# ---------- المشغل 2: SoundCloud ----------
def player_soundcloud(q):
    res = sc_player_resolve(q)
    if res.get("error"):
        return None, res["error"]
    return {
        "title": res["title"],
        "url": res["audio_url"],
        "duration": res["duration"],
        "track_url": res.get("track_url", ""),
        "source": "SoundCloud",
    }, None


# ---------- المشغل 3: TikTok ----------
def player_tiktok(q):
    url = "https://tik-sand-two.vercel.app/tiktok?q=" + urllib.parse.quote(q)
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    d = r.json()
    link = d.get("music_url")
    if not link:
        return None, "no music_url in response"
    title = (d.get("title") or "").split("#")[0].strip()
    if not title or len(title) > 100:
        title = "TikTok Music"
    return {
        "title": title,
        "url": link,
        "duration": 0,
        "author": d.get("author", ""),
        "source": "TikTok",
    }, None


PLAYERS = [
    ("Loader", player_loader),
    ("SoundCloud", player_soundcloud),
    ("TikTok", player_tiktok),
]


@app.route("/")
def home():
    return "Song API works. Use /api/song?q=name"


@app.route("/api/song")
def song():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"ok": False, "error": "missing q"}), 400
    only = request.args.get("p", "").lower()
    errors = {}
    for name, fn in PLAYERS:
        if only and only != name.lower():
            continue
        try:
            res, err = fn(q)
            if res:
                return jsonify({"ok": True, **res})
            errors[name] = err
        except Exception as e:
            errors[name] = repr(e)
    return jsonify({"ok": False, "errors": errors}), 404
