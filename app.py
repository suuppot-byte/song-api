import urllib.parse
import requests
from flask import Flask, request, jsonify
from sc_player import sc_player_resolve

app = Flask(__name__)


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


PLAYERS = [("SoundCloud", player_soundcloud), ("TikTok", player_tiktok)]


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
