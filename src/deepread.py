"""
deepread.py - P1: read the actual page bodies, not just snippets.

The snippet distiller finds the right pages but never reads them, so verbatim
lists and details never reach the model. This stage fetches the top-ranked
source URLs through the SAME polite/ban-safe plumbing as search (`_get` ->
per-host lock + gap + circuit breaker + per-run cap), strips them to text, and
hands the full body to the extractor. Gated to the top-K sources per campaign.
"""
import os, re, html
from concurrent.futures import ThreadPoolExecutor

from . import search

MAX_CHARS = int(os.environ.get("MASS_DEEPREAD_CHARS", "12000"))
DEFAULT_K = int(os.environ.get("MASS_DEEPREAD_K", "8"))

_BLOCKS = re.compile(r"<(script|style|noscript|svg|head|nav|footer|header|aside|form|button)[^>]*>.*?</\1>",
                     re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n\s*\n+")


def fetch_text(url):
    """Fetch one URL via the polite _get path; return stripped page text ('' on any block/error)."""
    if not url or not url.startswith("http"):
        return ""
    try:
        raw = search._get(url, timeout=20)          # reuses UA + throttle + host-lock + breaker
    except Exception:
        return ""                                    # Blocked / network / non-2xx -> skip politely
    body = _BLOCKS.sub(" ", raw)
    body = _TAGS.sub(" ", body)
    body = html.unescape(body)
    body = _WS.sub(" ", body)
    body = _NL.sub("\n", body).strip()
    return body[:MAX_CHARS]


def read_sources(sources, k=DEFAULT_K, workers=6):
    """Fetch text for the top-k sources concurrently (polite per-host).
    Returns [{url, title, text}] for those that yielded usable body text."""
    picked = [s for s in sources if s.get("url", "").startswith("http")][:k]
    if not picked:
        return []

    def work(s):
        t = fetch_text(s.get("url", ""))
        return {"url": s.get("url", ""), "title": s.get("title", ""), "text": t} if len(t) > 200 else None

    out = []
    with ThreadPoolExecutor(max_workers=min(workers, len(picked))) as pool:
        for r in pool.map(work, picked):
            if r:
                out.append(r)
    return out
