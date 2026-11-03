import re
import urllib.parse
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

H = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
    "Referer": "https://jmnails.pl/",
}


def get_video_id(q):
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([0-9A-Za-z_-]{11})", q)
    if m:
        return m.group(1)
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q)
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
    ids = re.findall(r"watch\?v=(\S{11})", r.text)
    return ids[0] if ids else None


def player_vip(q):
    vid = get_video_id(q)
    if not vid:
        return None, "no video id"
    title, duration = "Unknown", 0
    try:
        r = requests.get(f"https://v2.y2jar.cc/i/{vid}", headers=H, timeout=6)
        d = r.json()
        title = d.get("title", title)
        duration = int(d.get("duration") or 0)
    except Exception:
        pass
    r = requests.get(f"https://capi.y2jar.cc/scr/{vid}?s=5", headers=H, timeout=10)
    d = r.json()
    link = d.get("downloadUrl") or d.get("link") or d.get("url")
    if not link:
        return None, "no download link"
    return {"title": title, "url": link, "duration": duration, "source": "VIP"}, None


PLAYERS = [("VIP", player_vip)]


@app.route("/")
def home():
    return "Song API works. Use /api/song?q=name"


@app.route("/api/song")
def song():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"ok": False, "error": "missing q"}), 400
    errors = {}
    for name, fn in PLAYERS:
        try:
            res, err = fn(q)
            if res:
                return jsonify({"ok": True, **res})
            errors[name] = err
        except Exception as e:
            errors[name] = repr(e)
    return jsonify({"ok": False, "errors": errors}), 404
