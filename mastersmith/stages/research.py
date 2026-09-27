"""Stage 0b: find a photograph of a NAMED real thing on the web, so the seed comes from the thing
itself rather than from a text-to-image guess. DuckDuckGo image search (no key), our own fetch (the
vendors never fetch a URL for us), size and relevance filters, then a vision ranking of the few that
survive. Returns the best local picture or None; the caller then cleans it up like a customer photo."""
import io
import os
import re

import requests
from PIL import Image

from ..llm import extract_json
from ..netsafe import UnsafeURL, get_public

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MasterSmith/0.1; +https://github.com/kevinpbuckley/Master-Smith)"}
MIN_EDGE = 320
MAX_EDGE = 1280
STOP = {"the", "and", "for", "with", "from", "into", "onto", "over", "under", "view", "side", "front", "top", "back",
        "photo", "picture", "image", "reference", "real", "game", "model", "3d", "render", "concept", "art"}
PRIVATE = re.compile(r"^(localhost|127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|0\.|\[::1\])")

RANK_PROMPT = """You are choosing a photograph to build a 3D model from. The subject: {subject}
For each numbered picture answer with JSON only:
{{"scores": [{{"i": 1, "is_subject": true/false, "single_object": true/false, "whole_visible": true/false,
             "clean": true/false, "score": 1-10}}, ...]}}
score 10 = a clear, whole, unobstructed photo of exactly that subject from a useful angle (side or three-quarter);
1 = wrong subject, a crowd, a crop, a diagram, text, or a toy/render of something else."""


def query_words(text):
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(w) >= 3 and w not in STOP}


def relevant_to(query, row):
    """A row captioned or addressed with a word of the query (an engine misreading a proper noun
    returns wallpapers of a letter; those carry none of the words)."""
    words = query_words(query)
    if not words:
        return True
    hay = " ".join(str((row or {}).get(k) or "") for k in ("title", "url", "image", "source"))
    return bool(words & query_words(hay))


def public_url(url):
    """A cheap first filter on search rows, by name only; fetch_image checks the address the host resolves to."""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.hostname) and not PRIVATE.match(p.hostname)
    except ValueError:
        return False


def _ddg(query, count, log):
    """DuckDuckGo image rows; one retry, because three workers asking at once get an empty answer now and then."""
    import time
    try:
        from ddgs import DDGS
    except ImportError:
        return []
    for attempt in range(2):
        try:
            rows = DDGS().images(query, max_results=max(1, min(int(count), 30))) or []
        except Exception as e:  # noqa: BLE001
            log("  research: duckduckgo failed: %s" % str(e)[:120])
            rows = []
        if rows:
            return rows
        time.sleep(2 + 3 * attempt)
    return []


def _commons(query, count, log):
    """Wikimedia Commons file search: no key, generous limits, and the best free photographs of real
    machines. Returns rows shaped like the DuckDuckGo ones."""
    try:
        r = requests.get("https://commons.wikimedia.org/w/api.php", headers=HEADERS, timeout=20, params={
            "action": "query", "format": "json", "generator": "search", "gsrsearch": query, "gsrnamespace": "6",
            "gsrlimit": str(max(1, min(int(count), 30))), "prop": "imageinfo", "iiprop": "url|size|mime",
            "iiurlwidth": "1600"})
        pages = (r.json().get("query") or {}).get("pages") or {}
    except Exception as e:  # noqa: BLE001
        log("  research: wikimedia failed: %s" % str(e)[:120])
        return []
    rows = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        if info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        rows.append({"title": page.get("title", ""), "image": info.get("thumburl") or info.get("url"),
                     "url": info.get("descriptionurl", ""), "width": info.get("thumbwidth") or info.get("width"),
                     "height": info.get("thumbheight") or info.get("height"), "source": "commons"})
    return rows


def image_search(query, count=12, log=print):
    query = " ".join(str(query or "").split())
    if not query or os.environ.get("MASTERSMITH_IMAGE_SEARCH", "").lower() == "off":
        return []
    rows = _ddg(query, count, log)
    provider = "duckduckgo"
    if len(rows) < 4:
        more = _commons(query, count, log)
        if more:
            rows = rows + more
            provider = "duckduckgo+commons" if rows is not more else "commons"
    rows = [r for r in rows if min(int(r.get("width") or 0), int(r.get("height") or 0)) >= MIN_EDGE and relevant_to(query, r)]
    urls = []
    for r in rows:
        u = r.get("image") or ""
        if u.startswith("http") and u not in urls and public_url(u):
            urls.append(u)
    log("  research: %r -> %d candidate picture(s) via %s" % (query[:60], len(urls), provider))
    return urls


def fetch_image(url, log=print, max_bytes=12 * 1024 * 1024):
    """(PIL image, bytes) fetched by us, or (None, None). Sniffs the bytes, never trusts the extension."""
    try:
        with get_public(url, headers=HEADERS, timeout=20) as r:
            if r.status_code != 200:
                return None, None
            data = r.raw.read(max_bytes + 1, decode_content=True)
        if len(data) > max_bytes or len(data) < 12:
            return None, None
    except (requests.RequestException, UnsafeURL):
        return None, None
    if not (data[:8] == b"\x89PNG\r\n\x1a\n" or data[:2] == b"\xff\xd8" or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")):
        return None, None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:  # noqa: BLE001
        return None, None
    if min(im.size) < MIN_EDGE:
        return None, None
    return im.convert("RGB"), data


def find_reference_photo(job, spec, want=6):
    """Search, fetch, rank; returns the path of the best photograph of the subject, or None."""
    subject = spec.search_query or spec.name
    queries = [subject, "%s side view photo" % subject]
    seen, paths = set(), []
    for q in queries:
        for url in image_search(q, count=12, log=job.log):
            if url in seen or len(paths) >= want:
                continue
            seen.add(url)
            im, _data = fetch_image(url, log=job.log)
            if im is None:
                continue
            if max(im.size) > MAX_EDGE:
                s = MAX_EDGE / float(max(im.size))
                im = im.resize((int(im.size[0] * s), int(im.size[1] * s)), Image.LANCZOS)
            path = os.path.join(job.dir, "research_%d.jpg" % len(paths))
            im.save(path, quality=88)
            paths.append(path)
        if len(paths) >= want:
            break
    if not paths:
        job.log("  research: no usable pictures found")
        return None
    text = job.llm.vision(RANK_PROMPT.format(subject=subject), paths, max_tokens=900)
    j = extract_json(text) or {}
    best, best_score = None, 0
    for row in j.get("scores") or []:
        try:
            i, sc = int(row.get("i", 0)) - 1, int(row.get("score", 0) or 0)
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(paths) and row.get("is_subject") and row.get("single_object") and sc > best_score:
            best, best_score = paths[i], sc
    if best is None or best_score < 6:
        job.log("  research: %d picture(s) fetched, none good enough (best %d/10)" % (len(paths), best_score))
        return None
    job.log("  research: picked %s (%d/10) of %d" % (os.path.basename(best), best_score, len(paths)))
    return best
