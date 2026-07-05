"""
deepread.py - P1: read the actual page bodies, not just snippets.

The snippet distiller finds the right pages but never reads them, so verbatim
lists and details never reach the model. This stage fetches the top-ranked
source URLs through the SAME polite/ban-safe plumbing as search (`_get` ->
per-host lock + gap + circuit breaker + per-run cap), strips them to text, and
hands the full body to the extractor. Gated to the top-K sources per campaign.
"""
import os, re, html
from html.parser import HTMLParser
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor

from . import search

MAX_CHARS = int(os.environ.get("MASS_DEEPREAD_CHARS", "12000"))
DEFAULT_K = int(os.environ.get("MASS_DEEPREAD_K", "8"))

_BLOCKS = re.compile(r"<(script|style|noscript|svg|head|nav|footer|header|aside|form|button)[^>]*>.*?</\1>",
                     re.S | re.I)
# lighter strip for LINK extraction: keep nav/footer/"related" areas, because
# that's where cross-links to related pages live (discovery wants those).
_SCRIPTS = re.compile(r"<(script|style|noscript|svg|head)[^>]*>.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n\s*\n+")


def _to_text(raw):
    body = _BLOCKS.sub(" ", raw)
    body = _TAGS.sub(" ", body)
    body = html.unescape(body)
    body = _WS.sub(" ", body)
    body = _NL.sub("\n", body).strip()
    return body[:MAX_CHARS]


# ---- structured extraction: read the DOM, not the flattened soup -----------
_ITEM_TAGS = {"li", "td", "th", "dt", "h2", "h3", "h4", "code"}


class _ItemParser(HTMLParser):
    """Pull the text of list/table/heading elements -- where verbatim list
    entries actually live in the page's structure."""
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.buf = []
        self.items = []

    def handle_starttag(self, tag, attrs):
        if tag in _ITEM_TAGS:
            self.depth += 1
            self.buf = []

    def handle_endtag(self, tag):
        if tag in _ITEM_TAGS and self.depth > 0:
            self.depth -= 1
            txt = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            if 1 <= len(txt) <= 120:
                self.items.append(txt)
            self.buf = []

    def handle_data(self, data):
        if self.depth > 0:
            self.buf.append(data)


def extract_items(raw, cap=400):
    """Deduped list/table/heading items straight from the page structure."""
    try:
        p = _ItemParser()
        p.feed(_BLOCKS.sub(" ", raw))            # drop nav/footer/script first
    except Exception:
        return []
    return list(dict.fromkeys(p.items))[:cap]


class _LinkParser(HTMLParser):
    """Outbound links + their anchor text, from the content area."""
    def __init__(self, base):
        super().__init__()
        self.base, self.cur, self.buf, self.links = base, None, [], []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.cur = dict(attrs).get("href") or ""
            self.buf = []

    def handle_endtag(self, tag):
        if tag == "a" and self.cur is not None:
            try:
                full = urljoin(self.base, self.cur)
            except Exception:
                full = ""
            anchor = re.sub(r"\s+", " ", "".join(self.buf)).strip()
            if full.startswith("http"):
                self.links.append((full.split("#")[0], anchor[:120]))
            self.cur, self.buf = None, []

    def handle_data(self, data):
        if self.cur is not None:
            self.buf.append(data)


def extract_links(raw, base_url, cap=300):
    """Deduped (url, anchor) outbound links from the page's CONTENT area
    (nav/footer/header stripped first, so these are real body links)."""
    try:
        p = _LinkParser(base_url)
        p.feed(_SCRIPTS.sub(" ", raw))               # keep nav/footer links for discovery
    except Exception:
        return []
    seen, out = set(), []
    for u, a in p.links:
        if u in seen:
            continue
        seen.add(u)
        out.append((u, a))
        if len(out) >= cap:
            break
    return out


def fetch_text(url):
    """Back-compat: stripped page text ('' on any block/error)."""
    doc = _fetch(url, want_items=False)
    return doc["text"] if doc else ""


def _fetch(url, want_items=False, want_links=False):
    if not url or not url.startswith("http"):
        return None
    try:
        raw = search._get(url, timeout=20)          # reuses UA + throttle + host-lock + breaker
    except Exception:
        return None                                  # Blocked / network / non-2xx -> skip politely
    doc = {"text": _to_text(raw)}
    if want_items:
        doc["items"] = extract_items(raw)
    if want_links:
        doc["links"] = extract_links(raw, url)
    return doc


def read_sources(sources, k=DEFAULT_K, workers=6, want_items=False, want_links=False):
    """Fetch the top-k sources concurrently (polite per-host). Returns
    [{url, title, text, items?, links?}]. want_items = parse DOM list/table
    entries; want_links = harvest content-area outbound links for discovery."""
    picked = [s for s in sources if s.get("url", "").startswith("http")][:k]
    if not picked:
        return []

    def work(s):
        doc = _fetch(s.get("url", ""), want_items=want_items, want_links=want_links)
        if not doc:
            return None
        if len(doc["text"]) <= 200 and not doc.get("items"):
            return None
        doc["url"] = s.get("url", "")
        doc["title"] = s.get("title", "")
        return doc

    out = []
    with ThreadPoolExecutor(max_workers=min(workers, len(picked))) as pool:
        for r in pool.map(work, picked):
            if r:
                out.append(r)
    return out
