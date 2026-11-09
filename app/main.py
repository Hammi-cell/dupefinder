"""DupeFinder – findet doppelte Dateien (SHA-256) und ähnliche Bilder (pHash)."""
import hashlib
import io
import os
import ctypes
import ctypes.util
import json
import shutil
from datetime import datetime, timezone
import sqlite3
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import imagehash
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

DATA_ROOT = Path(os.environ.get("DATA_ROOT", "/data")).resolve()
DB_PATH = os.environ.get("DB_PATH", "/config/dupefinder.db")
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/config/thumbs"))
WORKERS = int(os.environ.get("WORKERS", "4"))
TRASH_NAME = ".dupefinder-trash"
ANON_LOG = os.environ.get("KEEP_DELETE_LOG", "0") != "1"     # Pfade endgültig gelöschter Dateien aus dem Log entfernen (SHA-256 bleibt)
QH = 64 * 1024                                              # Schnell-Hash: Anfang + Ende
EXCLUDE = set(filter(None, os.environ.get(
    "EXCLUDE", f"@eaDir,#recycle,#snapshot,.Trash-1000,{TRASH_NAME}").split(",")))
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".heif"}
WEB_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VID_EXT = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".3gp", ".mts", ".m2ts", ".wmv", ".mpg", ".mpeg", ".flv"}
PLAY_EXT = {".mp4", ".m4v", ".mov", ".webm"}      # i.d.R. direkt im Browser abspielbar


TEXT_EXT = {".txt", ".md", ".log", ".csv", ".tsv", ".json", ".xml", ".yml", ".yaml", ".ini", ".conf", ".cfg",
            ".py", ".js", ".ts", ".sh", ".bat", ".ps1", ".c", ".h", ".cpp", ".java", ".go", ".rs", ".php", ".rb",
            ".sql", ".html", ".htm", ".css", ".svg", ".srt", ".vtt", ".nfo", ".toml", ".env"}


def kind(path) -> str:
    e = Path(path).suffix.lower()
    return "image" if e in IMG_EXT else "video" if e in VID_EXT else "file"

CACHE_DIR.mkdir(parents=True, exist_ok=True)
Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- DB
def db():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


with db() as c:
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS files(
        path TEXT PRIMARY KEY, size INTEGER, mtime REAL,
        sha256 TEXT, phash TEXT, width INTEGER, height INTEGER,
        is_image INTEGER, seen REAL)""")
    cols = [r[1] for r in c.execute("PRAGMA table_info(files)")]
    if "duration" not in cols:
        c.execute("ALTER TABLE files ADD COLUMN duration REAL")
    if "meta" not in cols:
        c.execute("ALTER TABLE files ADD COLUMN meta TEXT")
    if "qhash" not in cols:
        c.execute("ALTER TABLE files ADD COLUMN qhash TEXT")
    c.execute("CREATE TABLE IF NOT EXISTS stats(key TEXT PRIMARY KEY, value INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS scans(root TEXT PRIMARY KEY, ts REAL, files INTEGER, bytes INTEGER)")
    c.execute("INSERT OR IGNORE INTO stats VALUES('first_start', ?)", (int(time.time()),))
    c.execute("""CREATE TABLE IF NOT EXISTS trash_log(
        ts REAL, path TEXT, size INTEGER, sha256 TEXT, trash_path TEXT)""")
    if "status" not in [r[1] for r in c.execute("PRAGMA table_info(trash_log)")]:
        c.execute("ALTER TABLE trash_log ADD COLUMN status TEXT DEFAULT 'trash'")   # trash | restored | deleted
    c.execute("CREATE INDEX IF NOT EXISTS ix_trash ON trash_log(trash_path)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_size ON files(size)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_sha ON files(sha256)")


# ---------------------------------------------------------------- Pfade
def safe(rel: str) -> Path:
    p = (DATA_ROOT / rel.lstrip("/")).resolve()
    if p != DATA_ROOT and DATA_ROOT not in p.parents:
        raise HTTPException(400, "Pfad außerhalb von /data")
    return p


def rel(p: Path) -> str:
    return str(p.relative_to(DATA_ROOT))


def under(path: str, roots: list[str]) -> bool:
    return any(r in ("", ".") or path == r or path.startswith(r.rstrip("/") + "/") for r in roots)


# ---------------------------------------------------------------- Hashing
def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def quick_hash(path: Path):
    """Größe + erste/letzte 64 KB. Kleine Dateien komplett -> dann auch gleich SHA-256."""
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with open(path, "rb") as f:
        if size <= 2 * QH:
            data = f.read()
            h.update(data)
            return h.hexdigest(), hashlib.sha256(data).hexdigest()
        h.update(f.read(QH))
        f.seek(-QH, 2)
        h.update(f.read(QH))
    return h.hexdigest(), None


def thumb_cache_files(relpath: str, mtime: float):
    for size in (320, 1280):
        key = hashlib.md5(f"{relpath}|{mtime}|{size}".encode()).hexdigest()
        yield CACHE_DIR / key[:2] / key[2:4] / f"{key}.jpg"


def drop_thumbs(relpath: str, mtime: float):
    for cp in thumb_cache_files(relpath, mtime):
        try:
            cp.unlink()
        except OSError:
            pass


def verify_row(con, r) -> bool:
    """Index-Eintrag außerhalb des aktuellen Scans prüfen: existiert die Datei noch unverändert?"""
    try:
        st = safe(r["path"]).stat()
    except (OSError, HTTPException):
        con.execute("DELETE FROM files WHERE path=?", (r["path"],))
        return False
    if st.st_size != r["size"] or abs(st.st_mtime - r["mtime"]) > 1:
        con.execute("""UPDATE files SET size=?, mtime=?, sha256=NULL, qhash=NULL, phash=NULL, meta=NULL, seen=?
                       WHERE path=?""", (st.st_size, st.st_mtime, time.time(), r["path"]))
        return False
    return True


def img_hash(path: Path):
    with Image.open(path) as im:
        w, h = im.size
        im.draft("RGB", (512, 512))          # JPEG: verkleinert dekodieren -> schnell
        im = ImageOps.exif_transpose(im)      # EXIF-Drehung berücksichtigen
        return str(imagehash.phash(im.convert("L"))), w, h


# ---------------------------------------------------------------- Metadaten
_libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)


def birthtime(path: Path):
    """Erstellzeit per statx() – nur wenn Kernel/Dateisystem sie liefern (sonst None)."""
    try:
        buf = ctypes.create_string_buffer(256)
        if _libc.statx(-100, str(path).encode(), 0, 0x800, buf) != 0:   # AT_FDCWD, STATX_BTIME
            return None
        mask = int.from_bytes(buf.raw[0:4], "little")
        sec = int.from_bytes(buf.raw[80:88], "little", signed=True)
        return sec if mask & 0x800 and sec > 0 else None
    except Exception:
        return None


def _iso_exif(v):
    try:
        return datetime.strptime(str(v).strip("\x00 ")[:19], "%Y:%m:%d %H:%M:%S").isoformat()
    except Exception:
        return None


def _iso_utc(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
    except Exception:
        return None


def _num(x):
    try:
        v = float(x)
        return v if v == v and v not in (float("inf"), float("-inf")) else None
    except Exception:
        return None


def _gps(g):
    def dms(v, ref):
        d, m, s_ = (_num(x) for x in v)
        val = d + m / 60 + s_ / 3600
        return -val if ref in ("S", "W") else val
    try:
        lat, lon = dms(g[2], g.get(1, "N")), dms(g[4], g.get(3, "E"))
        if lat == 0 and lon == 0:
            return None
        out = {"lat": round(lat, 6), "lon": round(lon, 6)}
        if 6 in g:
            out["alt"] = round(_num(g[6]), 1)
        return out
    except Exception:
        return None


META_VERSION = 2


def _clean(v):
    if isinstance(v, bytes):
        v = v.decode("utf-8", "ignore")
    return str(v).strip("\x00 ").strip() if v is not None else ""


def _first(v):
    return v[0] if isinstance(v, (tuple, list)) and v else v


def image_meta(path: Path) -> dict:
    """EXIF robust auslesen – jedes Feld einzeln, damit ein kaputter Wert nicht alles verhindert."""
    m = {}
    with Image.open(path) as im:
        m["format"] = im.format
        ex = im.getexif()
        if not ex:
            return m
        tags = dict(ex)
        try:
            tags.update(ex.get_ifd(0x8769))           # Exif-IFD (Belichtung, Aufnahmedatum …)
        except Exception:
            pass
        try:
            legacy = im._getexif() if hasattr(im, "_getexif") else None   # JPEG: flach, fängt Sonderfälle
            if legacy:
                for k, v in legacy.items():
                    tags.setdefault(k, v)
        except Exception:
            pass

        def field(name, fn):
            try:
                v = fn()
                if v not in (None, "", []):
                    m[name] = v
            except Exception:
                pass

        field("taken", lambda: _iso_exif(tags.get(36867) or tags.get(36868) or ""))
        if "taken" not in m:
            field("exif_date", lambda: _iso_exif(tags.get(306)))
        make, model = _clean(tags.get(271)), _clean(tags.get(272))
        if make or model:
            m["camera"] = model if make and model.lower().startswith(make.split()[0].lower()) else " ".join(x for x in (make, model) if x)
        field("lens", lambda: _clean(tags.get(42036)) or _clean(tags.get(0xA434)))

        def shutter():
            t = _num(_first(tags.get(33434)))
            if not t and (ssv := _num(_first(tags.get(37377)))) is not None:   # APEX ShutterSpeedValue
                t = 2 ** -ssv
            if not t:
                return None
            return f"1/{round(1 / t)} s" if t < 0.5 else f"{t:.1f} s".replace(".0 s", " s")

        def aperture():
            f = _num(_first(tags.get(33437)))
            if not f and (av := _num(_first(tags.get(37378)))) is not None:    # APEX ApertureValue
                f = 2 ** (av / 2)
            return f"f/{f:.1f}" if f else None

        field("shutter", shutter)
        field("aperture", aperture)
        field("iso", lambda: f"ISO {int(_num(_first(tags.get(34855) or tags.get(34867))))}")
        field("focal", lambda: f"{_num(_first(tags.get(37386))):g} mm")
        field("focal35", lambda: f"{int(_num(tags.get(41989)))} mm KB" if _num(tags.get(41989)) else None)
        field("flash", lambda: ("Blitz" if int(tags.get(37385)) & 1 else None) if tags.get(37385) is not None else None)
        exp = [m.get(k) for k in ("shutter", "aperture", "iso", "focal") if m.get(k)]
        if exp:
            m["exposure"] = " · ".join(exp)
        field("software", lambda: _clean(tags.get(305))[:60])
        field("gps", lambda: _gps(ex.get_ifd(0x8825)) or (_gps(tags[34853]) if isinstance(tags.get(34853), dict) else None))
    return m


def _iso6709(v):
    import re
    mm = re.match(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)?", v or "")
    if not mm:
        return None
    out = {"lat": round(float(mm[1]), 6), "lon": round(float(mm[2]), 6)}
    if mm[3]:
        out["alt"] = round(float(mm[3]), 1)
    return out


def video_meta(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "stream=codec_type,codec_name,width,height,r_frame_rate:stream_side_data=rotation"
                        ":stream_tags=rotate:format=duration,bit_rate,format_long_name:format_tags",
                        "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
    d = json.loads(r.stdout or "{}")
    fmt_, tags = d.get("format", {}), {k.lower(): v for k, v in d.get("format", {}).get("tags", {}).items()}
    vs = next((x for x in d.get("streams", []) if x.get("codec_type") == "video"), {})
    au = next((x for x in d.get("streams", []) if x.get("codec_type") == "audio"), {})
    m = {"width": vs.get("width"), "height": vs.get("height")}
    rot = next((abs(int(x.get("rotation", 0))) for x in vs.get("side_data_list", []) if "rotation" in x), 0) \
        or abs(int(vs.get("tags", {}).get("rotate", 0) or 0))
    if rot in (90, 270):
        m["width"], m["height"] = m["height"], m["width"]
    m["duration"] = _num(fmt_.get("duration"))
    m["taken"] = (_iso_utc(tags.get("com.apple.quicktime.creationdate")) if tags.get("com.apple.quicktime.creationdate")
                  else _iso_utc(tags["creation_time"]) if tags.get("creation_time") else None)
    make = tags.get("com.apple.quicktime.make") or tags.get("com.android.manufacturer") or tags.get("make")
    model = tags.get("com.apple.quicktime.model") or tags.get("com.android.model") or tags.get("model")
    if make or model:
        m["camera"] = " ".join(x for x in (make, model) if x)
    m["gps"] = _iso6709(tags.get("com.apple.quicktime.location.iso6709") or tags.get("location") or tags.get("location-eng"))
    codec = [vs.get("codec_name", "").upper()]
    if (fr := vs.get("r_frame_rate")) and "/" in fr and fr.split("/")[1] != "0":
        codec.append(f"{round(int(fr.split('/')[0]) / int(fr.split('/')[1]), 2):g} fps")
    if au.get("codec_name"):
        codec.append("Audio " + au["codec_name"].upper())
    m["codec"] = " · ".join(x for x in codec if x)
    if (br := _num(fmt_.get("bit_rate"))):
        m["bitrate"] = f"{br / 1e6:.1f} Mbit/s"
    m["format"] = (fmt_.get("format_long_name") or "")[:40]
    if tags.get("com.apple.quicktime.software") or tags.get("encoder"):
        m["software"] = (tags.get("com.apple.quicktime.software") or tags.get("encoder"))[:60]
    return {k: v for k, v in m.items() if v}


def file_meta(path: Path) -> dict:
    st = path.stat()
    m = {"v": META_VERSION, "ctime": st.st_ctime, "btime": birthtime(path)}
    k = kind(path)
    try:
        if k == "image":
            m.update(image_meta(path))
        elif k == "video":
            m.update(video_meta(path))
    except Exception as e:
        m["error"] = str(e)[:100]
    return {k_: v for k_, v in m.items() if v is not None}


# ---------------------------------------------------------------- Job
job = {"state": "idle", "phase": "", "done": 0, "total": 0, "msg": "", "result": None, "started": 0}
lock = threading.Lock()


def walk(roots):
    for r in roots:
        base = safe(r)
        if base.is_file():
            yield base
            continue
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d not in EXCLUDE and not d.startswith(".")]
            for fn in fns:
                if not fn.startswith("."):
                    yield Path(dp) / fn


def scan(roots, need_images, use_index=True):
    """Liefert (scope, extra): Dateien der Auswahl und geprüfte Index-Dateien außerhalb mit gleicher Größe."""
    con = db()
    known = {r["path"]: r for r in con.execute("SELECT path,size,mtime FROM files")}
    now = time.time()

    # Phase 1: Dateisystem einlesen
    job.update(phase="Dateien einlesen", done=0, total=0)
    entries, unchanged = [], []
    for p in walk(roots):
        try:
            st = p.stat()
        except OSError:
            continue
        rp = rel(p)
        k = known.get(rp)
        if not k or k["size"] != st.st_size or abs(k["mtime"] - st.st_mtime) > 1:
            con.execute("""INSERT OR REPLACE INTO files(path,size,mtime,is_image,seen) VALUES(?,?,?,?,?)""",
                        (rp, st.st_size, st.st_mtime, int(p.suffix.lower() in IMG_EXT), now))
        else:
            unchanged.append((now, rp))
        entries.append(rp)
        job["done"] = len(entries)
    con.executemany("UPDATE files SET seen=? WHERE path=?", unchanged)
    scope = set(entries)
    # verwaiste Einträge unterhalb der gescannten Ordner entfernen (Datei existiert nicht mehr)
    stale = [(p,) for p in known if p not in scope and under(p, roots)]
    con.executemany("DELETE FROM files WHERE path=?", stale)
    con.commit()
    job["pruned"] = len(stale)
    for r in roots:                                   # merken: dieser Ordner ist komplett im Index
        lo, hi = _prange(r)
        n, b = con.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM files WHERE path>=? AND path<?", (lo, hi)).fetchone()
        con.execute("INSERT OR REPLACE INTO scans VALUES(?,?,?,?)", (r, now, n, b))
    con.commit()

    allrows = {r["path"]: dict(r) for r in con.execute(
        "SELECT path,size,mtime,sha256,qhash,phash,is_image FROM files")}

    # Phase 2: Kandidaten = gleiche Größe (innerhalb der Auswahl und – optional – im ganzen Index)
    scope_sizes = {allrows[p]["size"] for p in scope if allrows[p]["size"] > 0}
    by_size = defaultdict(list)
    for r in allrows.values():
        if r["size"] in scope_sizes and (use_index or r["path"] in scope):
            by_size[r["size"]].append(r)
    cand = [r for g in by_size.values() if len(g) > 1 for r in g]
    verified = set()

    def check(rows, phase):
        """Index-Treffer außerhalb der Auswahl auf Existenz/Unverändertheit prüfen."""
        if not rows:
            return set()
        job.update(phase=phase, done=0, total=len(rows))
        bad = set()
        for i, r in enumerate(rows, 1):
            if verify_row(con, r):
                verified.add(r["path"])
            else:
                bad.add(r["path"])
            job["done"] = i
        con.commit()
        return bad

    # Index-Treffer ohne Schnell-Hash müssen ohnehin gelesen werden -> jetzt prüfen
    bad = check([r for r in cand if r["path"] not in scope and not r["qhash"]], "Index-Treffer prüfen")
    cand = [r for r in cand if r["path"] not in bad]

    # Phase 3: Schnell-Hash (128 KB je Datei), danach SHA-256 nur bei gleichem Schnell-Hash
    todo = [r["path"] for r in cand if not r["qhash"]]
    run_pool("Schnell-Hash", todo, lambda p: quick_hash(safe(p)),
             "UPDATE files SET qhash=?, sha256=COALESCE(?, sha256) WHERE path=?", con)
    fresh = {r["path"]: dict(r) for r in con.execute("SELECT path,size,mtime,qhash,sha256 FROM files")}
    cand = [fresh[r["path"]] for r in cand if r["path"] in fresh]
    by_q = defaultdict(list)
    for r in cand:
        if r["qhash"]:
            by_q[(r["size"], r["qhash"])].append(r)
    # nur Gruppen mit mindestens einer Datei der Auswahl sind interessant
    groups = [g for g in by_q.values() if len(g) > 1 and any(r["path"] in scope for r in g)]
    # Index-Treffer mit gespeichertem Schnell-Hash erst jetzt prüfen – nur die, die wirklich in einer Gruppe landen
    bad = check([r for g in groups for r in g if r["path"] not in scope and r["path"] not in verified],
                "Index-Treffer prüfen")
    groups = [[r for r in g if r["path"] not in bad] for g in groups]
    groups = [g for g in groups if len(g) > 1]
    todo = [r["path"] for g in groups for r in g if not r["sha256"]]
    run_pool("SHA-256 berechnen", todo, lambda p: (sha256(safe(p)),),
             "UPDATE files SET sha256=? WHERE path=?", con)
    extra = {r["path"] for g in groups for r in g if r["path"] not in scope}

    # Phase 4: pHash für Bilder der Auswahl
    if need_images:
        todo = [p for p in scope if allrows[p]["is_image"] and not allrows[p]["phash"]]
        run_pool("Bild-Hashes berechnen", todo, lambda p: img_hash(safe(p)),
                 "UPDATE files SET phash=?, width=?, height=? WHERE path=?", con)
    con.close()
    return scope, extra


def run_pool(phase, todo, fn, sql, con):
    job.update(phase=phase, done=0, total=len(todo))

    def work(p):
        try:
            return p, fn(p)
        except Exception:
            return p, None

    with ThreadPoolExecutor(WORKERS) as ex:
        for i, (p, res) in enumerate(ex.map(work, todo), 1):
            if res is None and "phash" in sql:
                res = ("", None, None)          # nicht lesbar -> nicht erneut versuchen
            if res is not None:
                con.execute(sql, (*res, p))
            job["done"] = i
            if i % 200 == 0:
                con.commit()
    con.commit()


# ---------------------------------------------------------------- Vergleich
def popcount_dist(a: int, b: int) -> int:
    return (a ^ b).bit_count()


class BKTree:
    def __init__(self):
        self.root = None

    def add(self, h, item):
        if self.root is None:
            self.root = [h, item, {}]
            return
        node = self.root
        while True:
            d = popcount_dist(h, node[0])
            if d in node[2]:
                node = node[2][d]
            else:
                node[2][d] = [h, item, {}]
                return

    def find(self, h, t):
        out, stack = [], [self.root] if self.root else []
        while stack:
            n = stack.pop()
            d = popcount_dist(h, n[0])
            if d <= t:
                out.append((n[1], d))
            for k, c in n[2].items():
                if d - t <= k <= d + t:
                    stack.append(c)
        return out


def norm_roots(paths):
    """Leere/doppelte/verschachtelte Pfade bereinigen, Reihenfolge der Auswahl bleibt ('' = ganz /data)."""
    norm = [str(Path(p.strip("/"))) if p.strip("/") else "" for p in paths] or [""]
    if "" in norm:
        return [""]
    out = []
    for r in norm:
        if r in out or any(under(r, [o]) for o in norm if o != r):
            continue                            # doppelt oder in einem anderen gewählten Ordner enthalten
        out.append(r)
    return out


def root_of(path, roots):
    for i, r in enumerate(roots):
        if under(path, [r]):
            return i + 1
    return 0


def compare(paths, cross, mode, threshold, use_index=True):
    roots = norm_roots(paths)
    cross = cross and len(roots) > 1
    files, extra = scan(roots, need_images=(mode == "similar"), use_index=use_index)
    job.update(phase="Gruppen bilden", done=0, total=0)
    con = db()
    allr = [dict(r) for r in con.execute("SELECT * FROM files")]
    rows = [r for r in allr if r["path"] in files]
    if mode == "exact":
        rows += [r for r in allr if r["path"] in extra]
    elif use_index:                                   # bekannte Bilder außerhalb (pHash schon vorhanden)
        rows += [r for r in allr if r["path"] not in files and r["phash"]]
    con.close()

    groups = []
    if mode == "exact":
        g = defaultdict(list)
        for r in rows:
            if r["sha256"]:
                g[r["sha256"]].append(r)
        groups = [v for v in g.values() if len(v) > 1]
    else:
        imgs = [r for r in rows if r["phash"]]
        tree, hashes = BKTree(), [int(r["phash"], 16) for r in imgs]
        for i, h in enumerate(hashes):
            tree.add(h, i)
        parent = list(range(len(imgs)))

        def f(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        job["total"] = len(imgs)
        for i, h in enumerate(hashes):
            for j, _ in tree.find(h, threshold):
                if j != i:
                    parent[f(i)] = f(j)
            job["done"] = i + 1
        g = defaultdict(list)
        for i, r in enumerate(imgs):
            g[f(i)].append(r)
        con = db()
        for v in g.values():
            if not any(r["path"] in files for r in v):
                continue
            v[:] = [r for r in v if r["path"] in files or verify_row(con, r)]   # Index-Treffer prüfen
            if len(v) > 1:
                ref = int(v[0]["phash"], 16)
                for r in v:
                    r["dist"] = popcount_dist(ref, int(r["phash"], 16))
                groups.append(v)
        con.commit()
        con.close()
    groups = [v for v in groups if any(r["path"] in files for r in v)]

    todo = [r for v in groups for r in v if not r.get("meta") or f'"v": {META_VERSION}' not in r["meta"]]
    if todo:
        job.update(phase="Metadaten lesen", done=0, total=len(todo))
        con = db()

        def work(r):
            try:
                return r, file_meta(safe(r["path"]))
            except Exception as e:
                return r, {"error": str(e)[:100]}

        with ThreadPoolExecutor(WORKERS) as ex:
            for i, (r, m) in enumerate(ex.map(work, todo), 1):
                if kind(r["path"]) == "video":
                    r["width"], r["height"] = m.pop("width", r["width"]), m.pop("height", r["height"])
                    r["duration"] = m.pop("duration", 0)
                r["meta"] = json.dumps(m)
                con.execute("UPDATE files SET width=?, height=?, duration=?, meta=? WHERE path=?",
                            (r["width"], r["height"], r["duration"], r["meta"], r["path"]))
                job["done"] = i
                if i % 200 == 0:
                    con.commit()
        con.commit()
        con.close()
    for v in groups:
        for r in v:
            r["meta"] = json.loads(r["meta"]) if isinstance(r.get("meta"), str) else (r.get("meta") or {})

    out = []
    for v in groups:
        for r in v:
            r["outside"] = r["path"] not in files
            r["root"] = 0 if r["outside"] else root_of(r["path"], roots)
            r["kind"] = kind(r["path"])
        if cross and len({r["root"] for r in v if not r["outside"]}) < 2:
            continue                            # nur ordnerübergreifende Treffer
        v.sort(key=lambda r: (r["outside"], r["root"], -(r["width"] or 0) * (r["height"] or 0), r["path"]))
        out.append({
            "id": len(out),
            "files": [{k: r.get(k) for k in ("path", "size", "mtime", "width", "height", "duration", "is_image", "kind",
                                             "root", "dist", "sha256", "phash", "meta", "outside", "seen")} for r in v],
            "wasted": sum(r["size"] for r in v[1:]),
            "outside": sum(1 for r in v if r["outside"]),
        })
    out.sort(key=lambda g: -g["wasted"])
    return {"mode": mode, "roots": roots, "cross": cross, "groups": out, "scanned": len(files),
            "wasted": sum(g["wasted"] for g in out), "index": use_index,
            "outside_files": sum(g["outside"] for g in out), "pruned": job.get("pruned", 0)}


def run_job(paths, cross, mode, threshold, use_index=True):
    try:
        job.update(state="running", started=time.time(), result=None, msg="", kind="search")
        job["result"] = compare(paths, cross, mode, threshold, use_index)
        job.update(state="done", phase="fertig")
    except Exception as e:
        job.update(state="error", msg=str(e))
    finally:
        lock.release()


# ---------------------------------------------------------------- API
app = FastAPI(title="DupeFinder")


@app.middleware("http")
async def no_cache(request, call_next):
    resp = await call_next(request)
    path = request.url.path
    if path.startswith("/api/") and not path.startswith(("/api/thumb", "/api/view", "/api/download")):
        resp.headers["Cache-Control"] = "no-store"
    elif path in ("/", "/index.html"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


class CompareReq(BaseModel):
    paths: list[str] = []        # leer = ganz /data
    cross: bool = False          # nur Treffer zwischen verschiedenen Ordnern
    use_index: bool = True       # bekannte Dateien außerhalb der Auswahl einbeziehen
    mode: str = "exact"          # exact | similar
    threshold: int = 6           # Hamming-Distanz für similar (0..64)


class PathsReq(BaseModel):
    paths: list[str]


def _prange(r):
    """Pfadbereich für Index-Abfragen per Primärschlüssel: alles unterhalb von r."""
    return ("", "\uffff") if r == "" else (r + "/", r + "0")       # '0' folgt direkt auf '/'


def index_info(con, scans, path, dir_mtime):
    cover = [(r, ts) for r, ts in scans if under(path, [r])]
    if cover:
        ts = max(t for _, t in cover)
        lo, hi = _prange(path)
        n = con.execute("SELECT COUNT(*) FROM files WHERE path>=? AND path<?", (lo, hi)).fetchone()[0]
        return {"state": "changed" if dir_mtime and dir_mtime > ts + 2 else "ok", "ts": ts, "count": n}
    sub = [ts for r, ts in scans if r != path and under(r, [path] if path else [""])]
    if sub:
        return {"state": "partial", "ts": max(sub)}
    return None


def has_subdirs(path):
    try:
        with os.scandir(path) as it:
            return any(e.is_dir() and not e.name.startswith(".") and e.name not in EXCLUDE for e in it)
    except OSError:
        return False


@app.get("/api/ls")
def ls(path: str = ""):
    p = safe(path)
    if not p.is_dir():
        raise HTTPException(404)
    dirs, files = [], []
    for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
        if e.name.startswith(".") or e.name in EXCLUDE:
            continue
        try:
            if e.is_dir():
                d = {"name": e.name, "path": rel(Path(e.path)), "has_dirs": has_subdirs(e.path)}
                if p == DATA_ROOT:
                    try:
                        du = shutil.disk_usage(e.path)
                        d.update(total=du.total, used=du.used)
                    except OSError:
                        pass
                dirs.append(d)
            else:
                st = e.stat()
                files.append({"name": e.name, "path": rel(Path(e.path)), "size": st.st_size,
                              "mtime": st.st_mtime, "is_image": kind(e.name) == "image", "kind": kind(e.name)})
        except OSError:
            pass
    here = rel(p) if p != DATA_ROOT else ""
    with db() as c:
        scans = [(r["root"], r["ts"]) for r in c.execute("SELECT root, ts FROM scans")]
        if scans:
            for d in dirs:
                try:
                    mt = os.stat(safe(d["path"])).st_mtime
                except OSError:
                    mt = None
                d["idx"] = index_info(c, scans, d["path"], mt)
    titems = _trash_items()
    cnt = defaultdict(int)
    for i in titems:
        cnt[i["path"].split("/")[0]] += 1
    th = sum(1 for i in titems if under(i["path"], [here]))
    if p == DATA_ROOT:
        for d in dirs:
            d["trash_count"] = cnt.get(d["name"], 0)
    return {"path": here, "dirs": dirs, "files": files, "trash_here": th}


@app.post("/api/compare")
def start(req: CompareReq):
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "Es läuft bereits ein Vergleich")
    try:
        for p in req.paths:
            if not safe(p).is_dir():
                raise HTTPException(400, f"kein Verzeichnis: {p}")
    except HTTPException:
        lock.release()
        raise
    threading.Thread(target=run_job, args=(req.paths, req.cross, req.mode,
                                           max(0, min(req.threshold, 32)), req.use_index), daemon=True).start()
    return {"ok": True}


def run_maintenance():
    try:
        job.update(state="running", started=time.time(), msg="", kind="maint")
        con = db()
        rows = [dict(r) for r in con.execute("SELECT path,size,mtime FROM files")]
        job.update(phase="Index prüfen", done=0, total=len(rows))
        removed = changed = 0
        keep_thumbs = set()
        for i, r in enumerate(rows, 1):
            ok = verify_row(con, r)
            if ok:
                keep_thumbs.update(str(x) for x in thumb_cache_files(r["path"], r["mtime"]))
            else:
                try:
                    safe(r["path"]).stat()
                    changed += 1
                except Exception:
                    removed += 1
            job["done"] = i
            if i % 1000 == 0:
                con.commit()
        con.commit()
        for (r,) in con.execute("SELECT root FROM scans").fetchall():
            if r and not safe(r).is_dir():
                con.execute("DELETE FROM scans WHERE root=?", (r,))
        con.commit()
        anon = 0
        if ANON_LOG:
            anon = con.execute("""UPDATE trash_log SET path=NULL, trash_path=NULL
                                  WHERE status IN ('deleted','gone') AND (path IS NOT NULL OR trash_path IS NOT NULL)""").rowcount
            con.commit()
        con.execute("VACUUM")
        con.close()
        for it in _trash_items():
            keep_thumbs.update(str(x) for x in thumb_cache_files(it["trash_path"], it["mtime"]))
        job.update(phase="Vorschaubilder aufräumen", done=0, total=0)
        tdel = tbytes = 0
        for dp, _, fns in os.walk(CACHE_DIR):
            for fn in fns:
                fp = os.path.join(dp, fn)
                if fp not in keep_thumbs:
                    try:
                        tbytes += os.path.getsize(fp)
                        os.remove(fp)
                        tdel += 1
                    except OSError:
                        pass
        for dp, dns, fns in os.walk(CACHE_DIR, topdown=False):
            if dp != str(CACHE_DIR) and not os.listdir(dp):
                os.rmdir(dp)
        job.update(state="done", phase="fertig",
                   msg=f"Index: {removed} verwaiste Einträge entfernt, {changed} geänderte zurückgesetzt · "
                       f"Vorschaubilder: {tdel} gelöscht ({tbytes / 1e6:.1f} MB)"
                       + (f" · Protokoll: {anon} Einträge anonymisiert" if anon else ""))
    except Exception as e:
        job.update(state="error", msg=str(e))
    finally:
        lock.release()


@app.post("/api/maintenance")
def maintenance():
    if not lock.acquire(blocking=False):
        raise HTTPException(409, "Es läuft bereits ein Vorgang")
    threading.Thread(target=run_maintenance, daemon=True).start()
    return {"ok": True}


@app.get("/api/status")
def status():
    s = {k: v for k, v in job.items() if k != "result"}
    s["has_result"] = job["result"] is not None
    return s


@app.get("/api/result")
def result():
    r = job["result"] or {"groups": []}
    return {k: v for k, v in r.items() if not k.startswith("_")}


@app.get("/api/stats")
def stats():
    with db() as c:
        r = dict(c.execute("SELECT COUNT(*) n, SUM(sha256 IS NOT NULL) s, SUM(phash<>'') p FROM files").fetchone())
        r.update(c.execute("""SELECT COUNT(*) saved_files, COALESCE(SUM(size),0) saved_bytes FROM trash_log
                              WHERE COALESCE(status,'trash')<>'restored'""").fetchone())
    items = _trash_items()
    r.update(trash_files=len(items), trash_bytes=sum(i["size"] for i in items))
    sizes = []
    for suf in ("", "-wal"):
        try:
            sizes.append(os.path.getsize(DB_PATH + suf))
        except OSError:
            sizes.append(0)
    r["db_main"], r["db_wal"] = sizes
    r["db_bytes"] = sum(sizes)
    with db() as c:
        r["first_start"] = c.execute("SELECT value FROM stats WHERE key='first_start'").fetchone()[0]
    return r


@app.get("/api/meta")
def meta(path: str):
    """Metadaten einer einzelnen Datei (z.B. im Papierkorb, die nicht im Index steht)."""
    p = safe(path)
    if not p.is_file():
        raise HTTPException(404)
    m = file_meta(p)
    out = {"width": m.pop("width", None), "height": m.pop("height", None), "duration": m.pop("duration", None)}
    if kind(p) == "image" and not out["width"]:
        try:
            with Image.open(p) as im:
                w, h = im.size
                o = (im.getexif() or {}).get(274, 1)
                out["width"], out["height"] = (h, w) if o in (5, 6, 7, 8) else (w, h)
        except Exception:
            pass
    out["meta"] = m
    return out


def file_info(rp: str) -> dict:
    p = safe(rp)
    if not p.is_file():
        raise HTTPException(404)
    st = p.stat()
    out = meta(rp)
    out.update(path=rp, size=st.st_size, mtime=st.st_mtime, kind=kind(p), is_image=kind(p) == "image")
    con = db()
    row = con.execute("SELECT * FROM files WHERE path=?", (rp,)).fetchone()
    idx = None
    if row:
        idx = {"seen": row["seen"], "qhash": row["qhash"], "sha256": row["sha256"], "phash": row["phash"],
               "current": row["size"] == st.st_size and abs(row["mtime"] - st.st_mtime) <= 1}
        if not idx["current"]:
            idx["sha256"] = idx["qhash"] = None        # Hash gehört zu einem älteren Stand der Datei
    out["index"] = idx
    sha = idx and idx["sha256"]
    out["sha256"] = sha
    copies, deleted = [], []
    if sha:
        for r in con.execute("SELECT path,size,mtime,seen FROM files WHERE sha256=? AND path<>?", (sha, rp)):
            try:
                cst = safe(r["path"]).stat()
                state = "ok" if cst.st_size == r["size"] and abs(cst.st_mtime - r["mtime"]) <= 1 else "changed"
            except Exception:
                state = "missing"
            copies.append({"path": r["path"], "seen": r["seen"], "state": state, "kind": kind(r["path"])})
        for r in con.execute("SELECT ts,path,trash_path,status,size FROM trash_log WHERE sha256=? ORDER BY ts DESC", (sha,)):
            deleted.append(dict(r))
    con.close()
    out["copies"], out["deleted"] = copies, deleted
    return out


@app.get("/api/fileinfo")
def fileinfo(path: str):
    return file_info(path.strip("/"))


@app.post("/api/hash")
def hash_now(req: PathsReq):
    """SHA-256 einer Datei sofort berechnen und in den Index übernehmen."""
    rp = req.paths[0].strip("/")
    p = safe(rp)
    if not p.is_file():
        raise HTTPException(404)
    st = p.stat()
    q, full = quick_hash(p)
    full = full or sha256(p)
    with db() as c:
        c.execute("""INSERT INTO files(path,size,mtime,is_image,seen,qhash,sha256) VALUES(?,?,?,?,?,?,?)
                     ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,
                     qhash=excluded.qhash, sha256=excluded.sha256, seen=excluded.seen""",
                  (rp, st.st_size, st.st_mtime, int(kind(p) == "image"), time.time(), q, full))
    return file_info(rp)


@app.post("/api/exists")
def exists(req: PathsReq):
    out = {}
    for rp in req.paths:
        try:
            out[rp] = safe(rp).is_dir() if rp else True
        except Exception:
            out[rp] = False
    return out


@app.get("/api/thumb")
def thumb(path: str, size: int = 320):
    p = safe(path)
    if not p.is_file():
        raise HTTPException(404)
    st = p.stat()
    key = hashlib.md5(f"{path}|{st.st_mtime}|{size}".encode()).hexdigest()
    cp = CACHE_DIR / key[:2] / key[2:4] / f"{key}.jpg"
    if not cp.exists():
        cp.parent.mkdir(parents=True, exist_ok=True)
        if p.suffix.lower() == ".pdf":
            base = cp.with_suffix("")
            subprocess.run(["pdftoppm", "-jpeg", "-jpegopt", "quality=80", "-f", "1", "-l", "1", "-singlefile",
                            "-scale-to", str(size), str(p), str(base)], capture_output=True, timeout=60)
            if not cp.exists():
                raise HTTPException(415, "keine PDF-Vorschau")
            return FileResponse(cp, headers={"Cache-Control": "max-age=86400"})
        if kind(p) == "video":
            for ss in ("1", "0"):                # Frame bei 1 s, bei sehr kurzen Clips den ersten
                subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", ss, "-i", str(p), "-frames:v", "1",
                                "-vf", f"scale={size}:{size}:force_original_aspect_ratio=decrease",
                                "-q:v", "4", str(cp)], capture_output=True, timeout=60)
                if cp.exists() and cp.stat().st_size:
                    break
            if not cp.exists():
                raise HTTPException(415, "kein Vorschaubild")
            return FileResponse(cp, headers={"Cache-Control": "max-age=86400"})
        try:
            with Image.open(p) as im:
                im.draft("RGB", (size * 2, size * 2))
                im = ImageOps.exif_transpose(im).convert("RGB")
                im.thumbnail((size, size))
                im.save(cp, "JPEG", quality=80)
        except Exception:
            raise HTTPException(415, "kein Bild")
    return FileResponse(cp, headers={"Cache-Control": "max-age=86400"})


@app.get("/api/view")
def view(path: str):
    """Original, falls browsertauglich – sonst (HEIC/TIFF) als JPEG."""
    p = safe(path)
    ext = p.suffix.lower()
    if ext in WEB_EXT or ext in PLAY_EXT:
        return FileResponse(p)                   # Range-Requests -> Spulen im Video geht
    if ext in {".mp3", ".wav", ".flac", ".aac", ".ogg", ".oga", ".opus", ".m4a"}:
        return FileResponse(p)
    if ext == ".pdf":
        return FileResponse(p, media_type="application/pdf", headers={"Content-Disposition": "inline"})
    if ext in TEXT_EXT:
        with open(p, "rb") as fh:
            data = fh.read(512 * 1024)           # max. 512 KB anzeigen
        txt = data.decode("utf-8", "replace")
        if p.stat().st_size > len(data):
            txt += "\n\n… (gekürzt, vollständige Datei per Download)"
        return Response(txt, media_type="text/plain; charset=utf-8",
                        headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})
    if kind(p) != "image":
        raise HTTPException(415, "keine Vorschau für diesen Dateityp")
    with Image.open(p) as im:
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((2560, 2560))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
    return Response(buf.getvalue(), media_type="image/jpeg")


@app.get("/api/download")
def download(path: str):
    p = safe(path)
    return FileResponse(p, filename=p.name)


@app.post("/api/trash")
def trash(req: PathsReq):
    """Verschiebt Dateien nach <Mount>/.dupefinder-trash/<Pfad> (gleiches Dateisystem)."""
    moved, errors, trashed = [], [], []
    con = db()
    for rp in req.paths:
        try:
            src = safe(rp)
            parts = Path(rel(src)).parts
            dst = DATA_ROOT / parts[0] / TRASH_NAME / Path(*parts[1:])
            if not src.is_file():
                raise FileNotFoundError("nicht gefunden")
            sst = src.stat()
            size = sst.st_size
            row = con.execute("SELECT sha256 FROM files WHERE path=?", (rp,)).fetchone()
            if dst.exists():                     # gleicher Name liegt schon im Papierkorb
                dst = dst.with_name(f"{dst.stem}.{int(time.time())}{dst.suffix}")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved.append(rp)
            trashed.append(rel(dst))
            drop_thumbs(rp, sst.st_mtime)
            con.execute("DELETE FROM files WHERE path=?", (rp,))
            con.execute("INSERT INTO trash_log(ts,path,size,sha256,trash_path,status) VALUES(?,?,?,?,?,'trash')",
                        (time.time(), rp, size, row["sha256"] if row else None, rel(dst)))
        except Exception as e:
            errors.append({"path": rp, "error": str(e)})
    con.commit()
    con.close()
    if job["result"]:
        res, ms = job["result"], dict(zip(moved, trashed))
        stash, dropped = res.setdefault("_stash", {}), res.setdefault("_dropped", {})
        for g in res["groups"]:
            for f in g["files"]:
                if f["path"] in ms:
                    stash[ms[f["path"]]] = (g["id"], f)       # für Wiederherstellen/Rückgängig
            g["files"] = [f for f in g["files"] if f["path"] not in ms]
        for g in res["groups"]:
            if len(g["files"]) < 2:
                dropped[g["id"]] = g
        res["groups"] = [g for g in res["groups"] if len(g["files"]) > 1]
        _recalc(res)
    return {"moved": moved, "trashed": trashed, "errors": errors, **stats()}


def _recalc(res):
    for g in res["groups"]:                      # Reihenfolge bewusst stabil lassen (kein Umsortieren)
        g["wasted"] = sum(f["size"] for f in g["files"][1:])
    res["wasted"] = sum(g["wasted"] for g in res["groups"])


def _unstash(restored):
    """Wiederhergestellte Dateien wieder in ihre Ergebnisgruppe einsortieren."""
    res = job["result"]
    if not res or "_stash" not in res:
        return
    byid = {g["id"]: g for g in res["groups"]}
    for tp, newpath in restored:
        if tp not in res["_stash"]:
            continue
        gid, f = res["_stash"].pop(tp)
        f = dict(f, path=newpath)
        g = byid.get(gid) or res["_dropped"].get(gid)
        if not g:
            continue
        g["files"].append(f)
        g["files"].sort(key=lambda r: (bool(r.get("outside")), r["root"], -(r["width"] or 0) * (r["height"] or 0), r["path"]))
        if gid not in byid and len(g["files"]) > 1:
            res["_dropped"].pop(gid, None)
            res["groups"].append(g)
            byid[gid] = g
    _recalc(res)


def _trash_items(root=""):
    """Quelle der Wahrheit ist der Inhalt der .dupefinder-trash-Ordner; Log liefert Zeit/Hash."""
    with db() as c:
        log = {r["trash_path"]: dict(r) for r in c.execute(
            "SELECT * FROM trash_log WHERE COALESCE(status,'trash')='trash' ORDER BY ts")}
    items = []
    mounts = [root.split("/")[0]] if root else [d.name for d in DATA_ROOT.iterdir() if d.is_dir()]
    for m in mounts:
        base = DATA_ROOT / m / TRASH_NAME
        if not base.is_dir():
            continue
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d not in EXCLUDE and not d.startswith((".", "@"))]
            for fn in fns:
                if fn.startswith(".") or fn in ("Thumbs.db", "desktop.ini"):
                    continue
                tp = Path(dp) / fn
                try:
                    st = tp.stat()
                except OSError:
                    continue
                trp = rel(tp)
                lg = log.get(trp, {})
                orig = lg.get("path") or str(Path(m) / tp.relative_to(base))
                items.append({"trash_path": trp, "path": orig, "size": st.st_size, "mtime": st.st_mtime,
                              "ts": lg.get("ts") or st.st_ctime, "sha256": lg.get("sha256"), "kind": kind(fn),
                              "is_image": kind(fn) == "image", "logged": bool(lg)})
    if not root:                       # Log-Einträge ohne Datei (z.B. außerhalb gelöscht) abgleichen
        present = {i["trash_path"] for i in items}
        gone = [tp for tp in log if tp not in present]
        if gone:
            with db() as c:
                c.executemany("UPDATE trash_log SET status='gone' WHERE trash_path=? AND COALESCE(status,'trash')='trash'",
                              [(g,) for g in gone])
                if ANON_LOG:
                    c.execute("UPDATE trash_log SET path=NULL, trash_path=NULL WHERE status='gone'")
    if root and "/" in root:
        items = [i for i in items if under(i["path"], [root])]
    items.sort(key=lambda i: (str(Path(i["path"]).parent).lower(), i["path"].lower()))
    return items


def _cleanup_empty(path: Path):
    """Leere Ordner im Papierkorb nach oben hin aufräumen."""
    while TRASH_NAME in path.parts:
        try:
            if path.is_dir():
                rest = [e for e in os.listdir(path) if e not in ("@eaDir", "Thumbs.db", "desktop.ini", ".DS_Store")]
                if rest:
                    return
                for e in os.listdir(path):
                    q = path / e
                    shutil.rmtree(q, ignore_errors=True) if q.is_dir() else q.unlink(missing_ok=True)
            path.rmdir()
        except OSError:
            return
        if path.name == TRASH_NAME:
            return
        path = path.parent


@app.get("/api/trash")
def trash_list(root: str = ""):
    items = _trash_items(root.strip("/"))
    return {"items": items, "total": sum(i["size"] for i in items), "count": len(items)}


class TrashPathsReq(BaseModel):
    trash_paths: list[str]


@app.post("/api/trash/restore")
def trash_restore(req: TrashPathsReq):
    done, errors = [], []
    items = {i["trash_path"]: i for i in _trash_items()}
    con = db()
    for tp in req.trash_paths:
        try:
            it = items.get(tp)
            if not it:
                raise FileNotFoundError("nicht im Papierkorb")
            src, dst = safe(tp), safe(it["path"])
            if dst.exists():                         # Original-Name wieder belegt -> Suffix
                n = 1
                while (alt := dst.with_name(f"{dst.stem} (wiederhergestellt{'' if n == 1 else ' ' + str(n)}){dst.suffix}")).exists():
                    n += 1
                dst = alt
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            _cleanup_empty(src.parent)
            con.execute("UPDATE trash_log SET status='restored' WHERE trash_path=? AND COALESCE(status,'trash')='trash'", (tp,))
            done.append({"trash_path": tp, "path": rel(dst)})
        except Exception as e:
            errors.append({"path": tp, "error": str(e)})
    con.commit()
    con.close()
    _unstash([(d["trash_path"], d["path"]) for d in done])
    return {"restored": done, "errors": errors, **stats()}


@app.post("/api/trash/purge")
def trash_purge(req: TrashPathsReq):
    done, errors = [], []
    con = db()
    for tp in req.trash_paths:
        try:
            src = safe(tp)
            if TRASH_NAME not in src.parts or not src.is_file():
                raise ValueError("keine Datei im Papierkorb")
            drop_thumbs(tp, src.stat().st_mtime)
            src.unlink()
            if src.exists():
                raise OSError("Datei existiert nach dem Löschen noch")
            side = src.parent / "@eaDir" / src.name          # Synology-Vorschaubilder der Datei
            if side.is_dir():
                shutil.rmtree(side, ignore_errors=True)
            _cleanup_empty(src.parent / "@eaDir")
            _cleanup_empty(src.parent)
            if ANON_LOG:     # nur Größe/Zeit für die Ersparnis-Summe behalten
                con.execute("""UPDATE trash_log SET status='deleted', path=NULL, trash_path=NULL
                               WHERE trash_path=? AND COALESCE(status,'trash')='trash'""", (tp,))
            else:
                con.execute("UPDATE trash_log SET status='deleted' WHERE trash_path=? AND COALESCE(status,'trash')='trash'", (tp,))
            done.append(tp)
        except Exception as e:
            errors.append({"path": tp, "error": str(e)})
    con.commit()
    con.close()
    return {"deleted": done, "errors": errors, **stats()}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
