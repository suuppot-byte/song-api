import re
import urllib.parse
import requests
import yt_dlp
from flask import Flask, request, jsonify

app = Flask(__name__)
UA = {"User-Agent": "Mozilla/5.0"}


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


def sc_search(q):
    if q.startswith("http") and "soundcloud.com" in q:
        return q
    r = requests.get("https://m.soundcloud.com/search",
                     params={"q": q}, headers=UA, timeout=10)
    urls = re.findall(r'data-testid="cell-entity-link" href="([^"]+)', r.text)
    return "https://soundcloud.com" + urls[0] if urls else None


def sc_pick_direct(formats):
    pref = {"mp3": 0, "m4a": 1, "aac": 2, "ogg": 3, "opus": 4}
    best = None
    for f in formats:
        u = f.get("url") or ""
        if (not u or ".m3u8" in u or ".mpd" in u
                or f.get("ext") not in pref
                or "preview" in ((f.get("format_id") or "") + u).lower()
                or f.get("protocol") not in (None, "http", "https")):
            continue
        if best is None or pref[f["ext"]] < pref[best["ext"]]:
            best = f
    return best["url"] if best else None


def player_soundcloud(q):
    opts = {"quiet": True, "no_warnings": True,
            "skip_download": True, "socket_timeout": 10}
    infos = []
    try:
        url = sc_search(q)
        if url:
            with yt_dlp.YoutubeDL(opts) as ydl:
                infos.append(ydl.extract_info(url, download=False))
    except Exception:
        pass
    if not infos:
        with yt_dlp.YoutubeDL(opts) as ydl:
            res = ydl.extract_info(f"scsearch3:{q}", download=False)
        infos = [e for e in (res or {}).get("entries") or [] if e]
    for info in infos:
        direct = sc_pick_direct(info.get("formats") or [])
        if direct:
            return {
                "title": info.get("title", "Unknown"),
                "url": direct,
                "duration": int(info.get("duration") or 0),
                "source": "SoundCloud",
            }, None
    return None, "no direct audio found"


PLAYERS = [("TikTok", player_tiktok), ("SoundCloud", player_soundcloud)]


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
