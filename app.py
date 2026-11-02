import io
import json
import os
import re
import threading
import time
import zipfile
from collections import OrderedDict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from PIL import Image
from pydantic import BaseModel

MANGA_DIR = Path(os.environ.get("MANGA_DIR", "~/Downloads/Kingdom")).expanduser()
DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
CACHE_VOLUMES = int(os.environ.get("CACHE_VOLUMES", "1"))  # each volume is ~0.5 GB in RAM

FAVORITES_FILE = DATA_DIR / "favorites.json"
THUMB_DIR = DATA_DIR / "thumbs"
THUMB_DIR.mkdir(parents=True, exist_ok=True)

IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def volume_title(filename: str) -> str:
    m = re.search(r"\bv(\d+)", filename)
    return f"Volume {int(m.group(1))}" if m else Path(filename).stem


def page_names(zf: zipfile.ZipFile) -> list[str]:
    names = [n for n in zf.namelist() if Path(n).suffix.lower() in IMAGE_EXTS and not n.startswith("__MACOSX")]
    return sorted(names, key=natural_key)


# --- library -----------------------------------------------------------------

def scan_library() -> dict[str, dict]:
    vols = {}
    for p in sorted(MANGA_DIR.glob("*.cbz"), key=lambda p: natural_key(p.name)):
        with zipfile.ZipFile(p) as zf:
            vols[p.name] = {"id": p.name, "title": volume_title(p.name), "pages": len(page_names(zf))}
    return vols


LIBRARY = scan_library()


def get_volume_path(vid: str) -> Path:
    if vid not in LIBRARY:
        raise HTTPException(404, "unknown volume")
    return MANGA_DIR / vid


# --- in-memory cache of the volume(s) being read ------------------------------

_cache: OrderedDict[str, list[tuple[str, bytes]]] = OrderedDict()
_cache_lock = threading.Lock()


def load_volume(vid: str) -> list[tuple[str, bytes]]:
    with _cache_lock:
        if vid in _cache:
            _cache.move_to_end(vid)
            return _cache[vid]
        # drop the old volume before loading the next so we never hold more than CACHE_VOLUMES
        while len(_cache) >= CACHE_VOLUMES:
            _cache.popitem(last=False)
        with zipfile.ZipFile(get_volume_path(vid)) as zf:
            pages = [(n, zf.read(n)) for n in page_names(zf)]
        _cache[vid] = pages
        return pages


def read_single_page(vid: str, n: int) -> tuple[str, bytes]:
    """Read one page straight from disk, without disturbing the cache (used for thumbnails)."""
    with _cache_lock:
        if vid in _cache:
            return _cache[vid][n]
    with zipfile.ZipFile(get_volume_path(vid)) as zf:
        names = page_names(zf)
        return names[n], zf.read(names[n])


# --- favorites ---------------------------------------------------------------

_fav_lock = threading.Lock()


def load_favorites() -> list[dict]:
    try:
        return json.loads(FAVORITES_FILE.read_text())
    except FileNotFoundError:
        return []


def save_favorites(favs: list[dict]) -> None:
    tmp = FAVORITES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(favs, indent=2))
    os.replace(tmp, FAVORITES_FILE)


class FavoriteIn(BaseModel):
    volume: str
    pages: list[int]  # one page, or both pages of a two-page spread


# --- routes ------------------------------------------------------------------

app = FastAPI()
STATIC = Path(__file__).parent / "static"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/volumes")
def volumes():
    return list(LIBRARY.values())


def check_page(vid: str, n: int):
    get_volume_path(vid)
    if not 0 <= n < LIBRARY[vid]["pages"]:
        raise HTTPException(404, "no such page")


@app.get("/api/page/{vid}/{n}")
def page(vid: str, n: int):
    check_page(vid, n)
    name, data = load_volume(vid)[n]
    return Response(data, media_type=IMAGE_EXTS[Path(name).suffix.lower()],
                    headers={"Cache-Control": "public, max-age=86400"})


_sizes: dict[str, list[tuple[int, int]]] = {}


@app.get("/api/sizes/{vid}")
def sizes(vid: str):
    """Width/height of every page, so the reader can show wide (double-page) images on their own."""
    path = get_volume_path(vid)
    if vid not in _sizes:
        with _cache_lock:
            cached = _cache.get(vid)
        if cached:
            dims = [Image.open(io.BytesIO(data)).size for _, data in cached]
        else:
            # Image.open only reads the header, so this doesn't decompress whole pages
            with zipfile.ZipFile(path) as zf:
                dims = []
                for name in page_names(zf):
                    with zf.open(name) as f:
                        dims.append(Image.open(f).size)
        _sizes[vid] = dims
    return _sizes[vid]


@app.get("/api/thumb/{vid}/{n}")
def thumb(vid: str, n: int):
    check_page(vid, n)
    out = THUMB_DIR / f"{vid}.{n}.jpg"
    if not out.exists():
        _, data = read_single_page(vid, n)
        img = Image.open(io.BytesIO(data)).convert("RGB")
        img.thumbnail((400, 600))
        img.save(out, "JPEG", quality=80)
    return FileResponse(out, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/favorites")
def favorites():
    return load_favorites()


@app.post("/api/favorites/toggle")
def toggle_favorite(f: FavoriteIn):
    if not 1 <= len(f.pages) <= 2:
        raise HTTPException(422, "a favorite is one page or a two-page spread")
    for p in f.pages:
        check_page(f.volume, p)
    with _fav_lock:
        favs = load_favorites()
        kept = [x for x in favs if not (x["volume"] == f.volume and x["pages"] == f.pages)]
        favorited = len(kept) == len(favs)
        if favorited:
            kept.append({"volume": f.volume, "pages": f.pages, "added": int(time.time())})
        save_favorites(kept)
    return {"favorited": favorited}
