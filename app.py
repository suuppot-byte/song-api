import re
import time
import urllib.parse
from urllib.parse import urlparse, quote
import requests
import yt_dlp
from curl_cffi import requests as cffi_requests
from flask import Flask, request, jsonify, Response
import os
import uuid
from sc_player import sc_player_resolve, sc_player_upload

app = Flask(__name__)

LOADER = "https://loader.to"
LOADER_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Origin": LOADER,
    "Referer": LOADER + "/",
    "X-Requested-With": "XMLHttpRequest",
}
LOADER_MAX_SECONDS = 30  # ⚡ زيادة الوقت

def loader_get(path, params, timeout=15):
    return cffi_requests.get(LOADER + path, params=params, headers=LOADER_HEADERS,
                            impersonate="chrome", timeout=timeout)

def get_youtube_url(q):
    if "youtube.com" in q or "youtu.be" in q:
        m = re.search(r"(?:v=|/v/|embed/|youtu.be/|/shorts/)([a-zA-Z0-9_-]{11})", q)
        if m: return "https://www.youtube.com/watch?v=" + m.group(1)
    try:
        url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q)
        r = cffi_requests.get(url, impersonate="chrome", timeout=8)
        ids = re.findall(r"watch\?v=([a-zA-Z0-9_-]{11})", r.text)
        if ids: return "https://www.youtube.com/watch?v=" + ids[0]
    except Exception: pass
    try:
        opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "socket_timeout": 8}
        with yt_dlp.YoutubeDL(opts) as ydl:
            res = ydl.extract_info("ytsearch1:" + q, download=False)
            entries = (res or {}).get("entries") or []
            if entries and entries[0].get("id"):
                return "https://www.youtube.com/watch?v=" + entries[0]["id"]
    except Exception: pass
    return None

def url_is_valid(url):
    if not url or not url.startswith("http"): return False
    try:
        r = requests.head(url, allow_redirects=True, timeout=8,
                         headers={"User-Agent": "Mozilla/5.0"})
        return r.status_code in (200, 206, 302)
    except Exception:
        return False

# ✅ إرجاع rehost_audio مع زيادة الحد
REHOST_MAX_BYTES = 200 * 1024 * 1024  # ⚡ 200MB بدلاً من 40MB

def is_audio_bytes(b):
    return b[:3] == b"ID3" or (len(b) > 1 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0)

def rehost_audio(link):
    """يحمّل الـ MP3 ثم يرفعه إلى uguu.se"""
    path = f"/tmp/ld_{uuid.uuid4().hex[:8]}.mp3"
    try:
        print(f"[REHOST] 📥 Downloading from loader.to...")
        r = cffi_requests.get(link, impersonate="chrome", timeout=60,
                             headers={"Accept": "audio/*, */*"})
        if r.status_code != 200:
            print(f"[REHOST] ❌ Download failed: {r.status_code}")
            return None
        
        data = r.content
        print(f"[REHOST] 📊 Size: {len(data) / (1024*1024):.1f}MB")
        
        if len(data) > REHOST_MAX_BYTES:
            print(f"[REHOST] ❌ Too large: {len(data)} bytes")
            return None
        
        if not is_audio_bytes(data):
            print(f"[REHOST] ❌ Not audio")
            return None
        
        with open(path, "wb") as f:
            f.write(data)
        
        print(f"[REHOST] 📤 Uploading to uguu.se...")
        return sc_player_upload(path)
    except Exception as e:
        print(f"[REHOST] ❌ {e!r}")
        return None
    finally:
        try: os.remove(path)
        except OSError: pass

def player_loader(q):
    yt_url = get_youtube_url(q)
    if not yt_url: return None, "youtube: no results"
    
    print(f"[LOADER] 🎵 Processing: {q}")
    r = loader_get("/ajax/download.php", {"format": "mp3", "url": yt_url})
    r.raise_for_status()
    data = r.json()
    
    if data.get("success") not in (True, 1):
        return None, "loader: request rejected"
    
    job_id = data.get("id")
    if not job_id: return None, "loader: no job id"
    
    start = time.time()
    while time.time() - start < LOADER_MAX_SECONDS:
        time.sleep(2)
        pr = loader_get("/ajax/progress.php", {"id": job_id}, timeout=10)
        if pr.status_code != 200: continue
        
        p = pr.json()
        if p.get("success") == 1:
            link = p.get("download_url")
            print(f"[LOADER] ✅ Got download URL")
            
            if url_is_valid(link):
                try: duration = int(p.get("duration") or 0)
                except (ValueError, TypeError): duration = 0
                
                clean = rehost_audio(link)
                if not clean:
                    return None, "loader: rehost failed"
                
                return {
                    "title": p.get("title") or q,
                    "url": clean,
                    "original_url": link,
                    "rehosted": True,
                    "duration": duration,
                    "track_url": yt_url,
                    "source": "Loader",
                }, None
    
    return None, "loader: timeout"

def player_soundcloud(q):
    res = sc_player_resolve(q)
    if res.get("error"): return None, res["error"]
    return {
        "title": res["title"],
        "url": res["audio_url"],
        "duration": res["duration"],
        "track_url": res.get("track_url", ""),
        "source": "SoundCloud",
    }, None

def player_tiktok(q):
    url = "https://tik-sand-two.vercel.app/tiktok?q=" + urllib.parse.quote(q)
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    d = r.json()
    link = d.get("music_url")
    if not link: return None, "no music_url in response"
    title = (d.get("title") or "").split("#")[0].strip()
    if not title or len(title) > 100: title = "TikTok Music"
    return {
        "title": title, "url": link, "duration": 0,
        "author": d.get("author", ""), "source": "TikTok",
    }, None

PLAYERS = [("Loader", player_loader), ("SoundCloud", player_soundcloud), ("TikTok", player_tiktok)]

@app.route("/")
def home():
    return "Song API works. Use /api/song?q=name"

@app.route("/api/dl")
def download():
    u = request.args.get("u", "")
    name = (request.args.get("n") or "song")[:80]
    parsed = urlparse(u)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host.endswith("savenow.to"):
        return "bad url", 400
    r = requests.get(u, stream=True, timeout=30, allow_redirects=False,
                    headers={"User-Agent": "Mozilla/5.0", "Accept": "audio/*, */*"})
    if r.status_code in (301, 302, 303, 307, 308):
        loc = r.headers.get("Location", "")
        if not (urlparse(loc).hostname or "").endswith("savenow.to"):
            return "redirected to ad page", 502
        r = requests.get(loc, stream=True, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    ctype = r.headers.get("Content-Type", "")
    if "audio" not in ctype and "octet-stream" not in ctype:
        return "not an audio file", 502
    headers = {
        "Content-Type": "audio/mpeg",
        "Content-Disposition": "attachment; filename=UTF-8''" + quote(name) + ".mp3",
    }
    if r.headers.get("Content-Length"):
        headers["Content-Length"] = r.headers["Content-Length"]
    return Response(r.iter_content(65536), headers=headers)

@app.route("/api/song")
def song():
    q = request.args.get("q", "").strip()
    if not q: return jsonify({"ok": False, "error": "missing q"}), 400
    only = request.args.get("p", "").lower()
    errors = {}
    for name, fn in PLAYERS:
        if only and only != name.lower(): continue
        try:
            res, err = fn(q)
            if res:
                if res.get("source") == "Loader" and not res.get("rehosted"):
                    res["download"] = (
                        request.host_url.rstrip("/") + "/api/dl?" +
                        urllib.parse.urlencode({"u": res["url"], "n": res["title"]}))
                return jsonify({"ok": True, **res})
            errors[name] = err
        except Exception as e:
            errors[name] = repr(e)
    return jsonify({"ok": False, "errors": errors}), 404

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
