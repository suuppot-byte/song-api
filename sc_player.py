import os
import re
import time
import uuid
import glob as _glob
import struct
import requests
import yt_dlp
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin as _urljoin

SC_MAX_BYTES = 60 * 1024 * 1024
SC_UA = {"User-Agent": "Mozilla/5.0"}


def _mp4_boxes(data, start=0, end=None):
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, typ = struct.unpack(">I4s", data[pos:pos + 8])
        hdr = 8
        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            break
        yield typ.decode("latin1"), pos, size, hdr
        pos += size


def _mp4_box(typ, payload):
    return struct.pack(">I4s", len(payload) + 8, typ.encode("latin1")) + payload


def _sc_read_init(init):
    moov = None
    trex = {"dur": 0, "size": 0}
    for t, pos, size, hdr in _mp4_boxes(init):
        if t == "moov":
            moov = init[pos + hdr:pos + size]
    if moov:
        for t, pos, size, hdr in _mp4_boxes(moov):
            if t == "mvex":
                mv = moov[pos + hdr:pos + size]
                for t2, p2, s2, h2 in _mp4_boxes(mv):
                    if t2 == "trex":
                        b = mv[p2 + h2:p2 + s2]
                        trex = {"dur": struct.unpack(">I", b[12:16])[0],
                                "size": struct.unpack(">I", b[16:20])[0]}
    return moov, trex


def _sc_read_fragment(seg, trex):
    runs = []
    for t, pos, size, hdr in _mp4_boxes(seg):
        if t != "moof":
            continue
        moof_start = pos
        moof = seg[pos + hdr:pos + size]
        for t2, p2, s2, h2 in _mp4_boxes(moof):
            if t2 != "traf":
                continue
            traf = moof[p2 + h2:p2 + s2]
            def_dur, def_size = trex["dur"], trex["size"]
            next_off = None
            for t3, p3, s3, h3 in _mp4_boxes(traf):
                b = traf[p3 + h3:p3 + s3]
                if t3 == "tfhd":
                    flags = int.from_bytes(b[1:4], "big")
                    o = 8
                    if flags & 0x1:
                        o += 8
                    if flags & 0x2:
                        o += 4
                    if flags & 0x8:
                        def_dur = struct.unpack(">I", b[o:o + 4])[0]
                        o += 4
                    if flags & 0x10:
                        def_size = struct.unpack(">I", b[o:o + 4])[0]
                        o += 4
                elif t3 == "trun":
                    flags = int.from_bytes(b[1:4], "big")
                    count = struct.unpack(">I", b[4:8])[0]
                    o = 8
                    if flags & 0x1:
                        cur = moof_start + struct.unpack(">i", b[o:o + 4])[0]
                        o += 4
                    else:
                        cur = next_off if next_off is not None else moof_start + size + 8
                    if flags & 0x4:
                        o += 4
                    sizes, durs = [], []
                    for _ in range(count):
                        d, sz = def_dur, def_size
                        if flags & 0x100:
                            d = struct.unpack(">I", b[o:o + 4])[0]
                            o += 4
                        if flags & 0x200:
                            sz = struct.unpack(">I", b[o:o + 4])[0]
                            o += 4
                        if flags & 0x400:
                            o += 4
                        if flags & 0x800:
                            o += 4
                        sizes.append(sz)
                        durs.append(d)
                    total = sum(sizes)
                    runs.append((count, sizes, durs, seg[cur:cur + total]))
                    next_off = cur + total
    return runs
_SC_CONTAINERS = ("moov", "trak", "mdia", "minf", "stbl")
_SC_STBL_DROP = ("stts", "stsc", "stsz", "stz2", "stco", "co64", "ctts", "stss")


def _sc_mdhd_timescale(moov):
    for t, pos, size, hdr in _mp4_boxes(moov):
        if t != "trak":
            continue
        trak = moov[pos + hdr:pos + size]
        for t2, p2, s2, h2 in _mp4_boxes(trak):
            if t2 != "mdia":
                continue
            mdia = trak[p2 + h2:p2 + s2]
            for t3, p3, s3, h3 in _mp4_boxes(mdia):
                if t3 == "mdhd":
                    b = mdia[p3 + h3:p3 + s3]
                    return struct.unpack(">I", b[12:16] if b[0] == 0 else b[20:24])[0]
    return 44100


def _sc_patch_duration(t, b, ctx):
    b = bytearray(b)
    v = b[0]
    if t == "mvhd":
        ts_off, dur_off = (12, 16) if v == 0 else (20, 24)
        ctx["mv_ts"] = struct.unpack(">I", b[ts_off:ts_off + 4])[0]
        dur = round(ctx["total"] * ctx["mv_ts"] / ctx["md_ts"])
    elif t == "tkhd":
        dur_off = 20 if v == 0 else 28
        dur = round(ctx["total"] * ctx.get("mv_ts", ctx["md_ts"]) / ctx["md_ts"])
    else:
        dur_off = 16 if v == 0 else 24
        dur = ctx["total"]
    if v == 0:
        b[dur_off:dur_off + 4] = struct.pack(">I", dur)
    else:
        b[dur_off:dur_off + 8] = struct.pack(">Q", dur)
    return bytes(b)


def _sc_rewrite(typ, body, ctx):
    out = b""
    for t, pos, size, hdr in _mp4_boxes(body):
        b = body[pos + hdr:pos + size]
        if t in ("mvex", "edts", "udta"):
            continue
        if typ == "stbl" and t in _SC_STBL_DROP:
            continue
        if t in _SC_CONTAINERS:
            out += _mp4_box(t, _sc_rewrite(t, b, ctx))
        elif t in ("mvhd", "tkhd", "mdhd"):
            out += _mp4_box(t, _sc_patch_duration(t, b, ctx))
        else:
            out += _mp4_box(t, b)
    if typ == "stbl":
        out += ctx["tables"]()
    return out


def sc_fmp4_to_m4a(init, segments, path):
    moov, trex = _sc_read_init(init)
    if not moov:
        raise ValueError("no moov")
    chunks = []
    sizes, durs = [], []
    for seg in segments:
        for count, sz, du, data in _sc_read_fragment(seg, trex):
            chunks.append((count, data))
            sizes += sz
            durs += du
    if not sizes:
        raise ValueError("no samples")

    md_ts = _sc_mdhd_timescale(moov)
    ftyp = _mp4_box("ftyp", b"M4A " + struct.pack(">I", 0) + b"M4A mp42isom")
    mdat_payload = b"".join(d for _, d in chunks)

    stts = []
    for d in durs:
        if stts and stts[-1][1] == d:
            stts[-1][0] += 1
        else:
            stts.append([1, d])
    stts_box = _mp4_box("stts", struct.pack(">II", 0, len(stts)) +
                        b"".join(struct.pack(">II", c, d) for c, d in stts))
    ents = []
    for i, (cnt, _) in enumerate(chunks, 1):
        if not ents or ents[-1][1] != cnt:
            ents.append((i, cnt))
    stsc_box = _mp4_box("stsc", struct.pack(">II", 0, len(ents)) +
                        b"".join(struct.pack(">III", fc, cnt, 1) for fc, cnt in ents))
    stsz_box = _mp4_box("stsz", struct.pack(">III", 0, 0, len(sizes)) +
                        struct.pack(">%dI" % len(sizes), *sizes))

    def build(mdat_off):
        offs, run = [], mdat_off
        for _, d in chunks:
            offs.append(run)
            run += len(d)
        stco_box = _mp4_box("stco", struct.pack(">II", 0, len(offs)) +
                            struct.pack(">%dI" % len(offs), *offs))
        ctx = {"total": sum(durs), "md_ts": md_ts,
               "tables": lambda: stts_box + stsc_box + stsz_box + stco_box}
        return _mp4_box("moov", _sc_rewrite("moov", moov, ctx))

    moov_len = len(build(0))
    new_moov = build(len(ftyp) + moov_len + 8)
    with open(path, "wb") as f:
        f.write(ftyp)
        f.write(new_moov)
        f.write(struct.pack(">I4s", len(mdat_payload) + 8, b"mdat"))
        f.write(mdat_payload)
    return int(sum(durs) / md_ts)


def sc_ts_to_aac(segments):
    out = bytearray()
    audio_pid = None
    for seg in segments:
        for i in range(0, len(seg) - 187, 188):
            pkt = seg[i:i + 188]
            if pkt[0] != 0x47:
                continue
            pusi = pkt[1] & 0x40
            pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
            afc = (pkt[3] >> 4) & 3
            off = 4
            if afc in (2, 3):
                off += 1 + pkt[4]
            if afc in (0, 2) or off >= 188:
                continue
            payload = pkt[off:]
            if pusi and payload[:3] == b"\x00\x00\x01":
                sid = payload[3]
                if audio_pid is None and 0xC0 <= sid <= 0xDF:
                    audio_pid = pid
                if pid != audio_pid:
                    continue
                out += payload[9 + payload[8]:]
            elif pid == audio_pid and audio_pid is not None:
                out += payload
    return bytes(out)
def _sc_fetch(url, tries=3):
    for i in range(tries):
        try:
            r = requests.get(url, headers=SC_UA, timeout=30)
            r.raise_for_status()
            return r.content
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1)


def sc_player_hls_audio(formats, duration, base):
    def is_preview(f):
        return "preview" in ((f.get("format_id") or "") + f["url"]).lower()

    def rank(f):
        fid = (f.get("format_id") or "").lower()
        if "opus" in fid or f.get("ext") in ("opus", "ogg"):
            return 2
        return 0 if ("mp3" in fid or f.get("ext") == "mp3") else 1

    cands = [f for f in formats if f.get("url") and ".m3u8" in f["url"]
             and f.get("vcodec") in (None, "none")]
    cands.sort(key=lambda f: (is_preview(f), rank(f)))

    for fmt in cands:
        if rank(fmt) == 2:
            continue
        try:
            pl = _sc_fetch(fmt["url"]).decode("utf-8", "ignore")
            lines = [l.strip() for l in pl.splitlines() if l.strip()]
            if any(l.startswith("#EXT-X-KEY") and "METHOD=NONE" not in l for l in lines):
                continue
            if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
                continue

            total = 0.0
            seg_urls, init_url = [], None
            for l in lines:
                if l.startswith("#EXTINF:"):
                    try:
                        total += float(l[8:].split(",")[0])
                    except ValueError:
                        pass
                elif l.startswith("#EXT-X-MAP"):
                    m = re.search(r'URI="([^"]+)"', l)
                    if m:
                        init_url = _urljoin(fmt["url"], m.group(1))
                elif not l.startswith("#"):
                    seg_urls.append(_urljoin(fmt["url"], l))
            if not seg_urls or (duration > 0 and total < duration * 0.8):
                continue
            # تقدير الحجم قبل التحميل (≈160kbps) — لا نضيع الوقت على ملف طويل
            if total * 20000 > SC_MAX_BYTES:
                print(f"[SC_PLAYER] too long for HLS ({int(total)}s)")
                continue

            with ThreadPoolExecutor(max_workers=8) as pool:
                init = pool.submit(_sc_fetch, init_url) if init_url else None
                segs = list(pool.map(_sc_fetch, seg_urls))
                init_data = init.result() if init else None
            if sum(len(s) for s in segs) > SC_MAX_BYTES:
                continue

            first = segs[0]
            if init_data is not None:
                path = f"{base}.m4a"
                sc_fmp4_to_m4a(init_data, segs, path)
                return path, "m4a"
            if first[:1] == b"\x47":
                data = sc_ts_to_aac(segs)
                if data[:1] == b"\xff" and (data[1] & 0xF0) == 0xF0:
                    path = f"{base}.aac"
                    with open(path, "wb") as f:
                        f.write(data)
                    return path, "aac"
                continue
            data = b"".join(segs)
            if data[:3] == b"ID3" or (data[:1] == b"\xff" and (data[1] & 0xE0) == 0xE0):
                ext = "aac" if data[:2] in (b"\xff\xf1", b"\xff\xf9") else "mp3"
                path = f"{base}.{ext}"
                with open(path, "wb") as f:
                    f.write(data)
                return path, ext
        except Exception as e:
            print(f"[SC_PLAYER] ⚠️ {fmt.get('format_id')}: {e!r}")
    return None


def sc_player_search(query):
    if not query:
        return None, "No search query provided."
    try:
        data = requests.get(
            "https://m.soundcloud.com/search",
            params={"q": query},
            headers=SC_UA,
            timeout=15,
        )
        urls = re.findall(r'data-testid="cell-entity-link" href="([^"]+)', data.text)
    except Exception as e:
        return None, f"Search failed: {e}"
    if not urls:
        return None, "No results found."
    return f"https://soundcloud.com{urls[0]}", None


def sc_player_infos(query):
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 15}
    seen = set()
    url, _err = sc_player_search(query)
    if url:
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if info and info.get("formats"):
                seen.add(info.get("webpage_url") or url)
                info.setdefault("webpage_url", url)
                yield info
        except Exception as e:
            print(f"[SC_PLAYER] ⚠️ extract: {e!r}")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            res = ydl.extract_info(f"scsearch3:{query}", download=False)
        for e in (res or {}).get("entries") or []:
            if e and e.get("formats") and e.get("webpage_url") not in seen:
                seen.add(e.get("webpage_url"))
                yield e
    except Exception as e:
        print(f"[SC_PLAYER] ⚠️ scsearch: {e!r}")


def sc_player_pick_direct(formats):
    pref = {"mp3": 0, "m4a": 1, "aac": 2, "ogg": 3, "opus": 4, "wav": 5, "flac": 6}
    best = None
    for f in formats:
        u = f.get("url") or ""
        if (not u or ".m3u8" in u or ".mpd" in u or f.get("vcodec") not in (None, "none")
                or f.get("ext") not in pref or "preview" in ((f.get("format_id") or "") + u).lower()
                or f.get("protocol") not in (None, "http", "https")):
            continue
        if best is None or pref[f["ext"]] < pref[best["ext"]]:
            best = f
    return best


def sc_player_rehost_direct(fmt, base, deadline=None):
    """رابط ساوند كلاود المباشر مؤقت: ننزّله ونرفعه إلى uguu"""
    path = f"{base}.{fmt.get('ext') or 'mp3'}"
    try:
        size = 0
        with requests.get(fmt["url"], stream=True, headers=SC_UA, timeout=30) as r:
            r.raise_for_status()
            with open(path, "wb") as f:
                for chunk in r.iter_content(65536):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > SC_MAX_BYTES or (deadline and time.time() > deadline):
                        return None
                    f.write(chunk)
        if size < 20000:
            return None
        left = int(deadline - time.time()) if deadline else 120
        return sc_player_upload(path, tries=1 if size > 40 * 1024 * 1024 else 2,
                                timeout=max(20, left))
    except Exception as e:
        print(f"[SC_PLAYER] ⚠️ rehost: {e!r}")
        return None


def sc_player_upload(path, tries=2, timeout=120):
    for _ in range(tries):
        try:
            with open(path, "rb") as f:
                r = requests.post("https://uguu.se/upload.php",
                                  files={"files[]": f}, headers=SC_UA, timeout=timeout)
            if r.status_code == 200:
                data = r.json()
                if data.get("success"):
                    return data["files"][0]["url"]
        except Exception as e:
            print(f"[SC_PLAYER] ⚠️ upload: {e!r}")
        time.sleep(1)
    return None


def sc_player_resolve(query, deadline=None):
    base = f"/tmp/temp_sc_{uuid.uuid4().hex[:8]}"
    try:
        for info in sc_player_infos(query):
            if deadline and time.time() > deadline:
                break
            title = info.get("title", "Unknown")
            track_url = info.get("webpage_url")
            try:
                duration = int(info.get("duration") or 0)
            except (ValueError, TypeError):
                duration = 0
            formats = [f for f in info.get("formats", []) if f.get("url")]

            direct = sc_player_pick_direct(formats)
            if direct:
                hosted = sc_player_rehost_direct(direct, base, deadline)
                return {"title": title, "track_url": track_url,
                        "audio_url": hosted or direct["url"],
                        "rehosted": bool(hosted),
                        "duration": duration, "source": "SoundCloud"}

            got = sc_player_hls_audio(formats, duration, base)
            if got:
                path, ext = got
                url = sc_player_upload(path)
                if url:
                    return {"title": title, "track_url": track_url, "audio_url": url,
                            "rehosted": True,
                            "duration": duration, "source": "SoundCloud"}
        return {"error": "No results found."}
    except Exception as e:
        print(f"[SC_PLAYER] ❌ {e!r}")
        return {"error": str(e)}
    finally:
        for f in _glob.glob(base + "*"):
            try:
                os.remove(f)
            except OSError:
                pass
