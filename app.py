import re
import time
import urllib.parse
from urllib.parse import urlparse, quote

import requests
import yt_dlp
from curl_cffi import requests as cffi_requests
from flask import Flask, request, jsonify, Response, g
import os
import uuid
from sc_player import sc_player_resolve

app = Flask(__name__)

LOADER = "https://loader.to"
LOADER_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Origin": LOADER,
    "Referer": LOADER + "/",
    "X-Requested-With": "XMLHttpRequest",
}
# الميزانية الكلية للطلب (ثواني) — لازم تكون أقل من maxDuration في vercel.json
TOTAL_BUDGET = int(os.environ.get("TOTAL_BUDGET", "85"))
RESERVE = 12  # وقت احتياطي لإعادة الرفع والرد


def time_left():
    return getattr(g, "deadline", time.time() + TOTAL_BUDGET) - time.time()


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



# ---------- فحص الرابط: هل هو ملف صوت فعلاً؟ ----------
def link_ok(url, impersonate=False):
    """يجلب أول جزء من الملف ويتأكد أنه صوت وليس صفحة (إعلانات/خطأ)"""
    if not url or not url.startswith("http"):
        return False
    try:
        if impersonate:
            r = cffi_requests.get(url, impersonate="chrome", stream=True, timeout=15,
                                  headers={"Accept": "audio/*,*/*"})
        else:
            r = requests.get(url, stream=True, timeout=15,
                             headers={"User-Agent": "Mozilla/5.0", "Accept": "audio/*,*/*"})
        if r.status_code not in (200, 206):
            return False
        ctype = (r.headers.get("Content-Type") or "").lower()
        if "text/html" in ctype or "json" in ctype:
            return False
        chunk = next(r.iter_content(2048), b"")
        r.close()
        return len(chunk) > 0 and chunk.lstrip()[:1] != b"<"
    except Exception as e:
        print("[LINK_CHECK]", repr(e))
        return False


# ---------- المشغل 1: loader.to ----------
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

    while time_left() > RESERVE:
        time.sleep(2)
        try:
            pr = loader_get("/ajax/progress.php", {"id": job_id}, timeout=10)
            if pr.status_code != 200:
                continue
            p = pr.json()
        except Exception:
            continue
        if p.get("success") == 1:
            link = p.get("download_url")
            if not url_is_valid(link):
                continue
            try:
                duration = int(p.get("duration") or 0)
            except (ValueError, TypeError):
                duration = 0
            # فحص: هل الرابط الأصلي ملف صوت فعلاً (بنفس طريقة /api/dl)
            if not link_ok(link, impersonate=True):
                return None, "loader: link check failed"
            title = p.get("title") or q
            proxy = (request.host_url.rstrip("/") + "/api/dl?" +
                     urllib.parse.urlencode({"u": link, "n": title}))
            res = {
                "title": title,
                "url": proxy,
                "original_url": link,
                "rehosted": False,
                "duration": duration,
                "track_url": yt_url,
                "source": "Loader",
            }
            return res, None
    return None, "loader: timeout"


# ---------- المشغل 2: ساوند كلاود ----------
def player_soundcloud(q):
    res = sc_player_resolve(q, deadline=time.time() + max(5, time_left() - 5))
    if res.get("error"):
        return None, res["error"]
    if not link_ok(res["audio_url"]):
        return None, "soundcloud: link check failed"
    return {
        "title": res["title"],
        "url": res["audio_url"],
        "rehosted": res.get("rehosted", False),
        "duration": res["duration"],
        "track_url": res.get("track_url", ""),
        "source": "SoundCloud",
    }, None


# ---------- المشغل 3: تيك توك ----------
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


# الترتيب: Loader ثم SoundCloud ثم TikTok
PLAYERS = [
    ("Loader", player_loader),
    ("SoundCloud", player_soundcloud),
    ("TikTok", player_tiktok),
]


@app.route("/")
def home():
    return "Song API works. Use /api/song?q=name"


# ---------- رابط وسيط يجبر المتصفح على التحميل ----------
@app.route("/api/dl")
def download():
    u = request.args.get("u", "")
    name = (request.args.get("n") or "song")[:80]
    parsed = urlparse(u)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host.endswith("savenow.to"):
        return "bad url", 400
    r = cffi_requests.get(u, stream=True, impersonate="chrome", timeout=60,
                          headers={"Accept": "audio/*,*/*"})
    ctype = r.headers.get("Content-Type", "")
    if "audio" not in ctype and "octet-stream" not in ctype:
        return "not an audio file", 502
    headers = {
        "Content-Type": "audio/mpeg",
        "Content-Disposition":
            "attachment; filename*=UTF-8''" + quote(name) + ".mp3",
    }
    if r.headers.get("Content-Length"):
        headers["Content-Length"] = r.headers["Content-Length"]
    return Response(r.iter_content(65536), headers=headers)


@app.route("/api/song")
def song():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"ok": False, "error": "missing q"}), 400
    only = request.args.get("p", "").lower()
    g.deadline = time.time() + TOTAL_BUDGET
    errors = {}
    for name, fn in PLAYERS:
        if only and only != name.lower():
            continue
        if time_left() < 5:
            errors[name] = "skipped: out of time"
            continue
        try:
            res, err = fn(q)
            if res:
                return jsonify({"ok": True, **res})
            errors[name] = err
        except Exception as e:
            errors[name] = repr(e)
    return jsonify({"ok": False, "errors": errors}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
