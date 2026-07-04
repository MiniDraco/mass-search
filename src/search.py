"""
search.py - KEYLESS web search across MANY free resolvers, ban-safe by design.

Every backend is a function(query, n) -> [{title,url,snippet,source}]. More
resolvers across more hosts = more parallel throughput without leaning on any
single site. The general-web slice is the ban-prone part (DuckDuckGo,
Marginalia); the structured/vertical APIs (Wikipedia, HN, Crossref, arXiv,
Semantic Scholar, StackExchange, GitHub) *want* traffic and are safe to hammer.

SAFETY (this is the part that keeps our one IP alive -- no do-overs):
  * per-host lock held ACROSS the request  -> never two hits to a host at once
  * per-host min-gap + jitter              -> documented-safe pace per host
  * CIRCUIT BREAKER: a host that returns a block signal (202 / 403 / 429 / 503)
    is DROPPED for the rest of the run instead of hammered into a real ban.
    Ban-prone hosts trip on the first block; friendly hosts get a gap-doubling
    soft backoff and only trip after repeated blocks.
  * per-run request CAP per host           -> a bug can't run away
  * empty/blocked responses are NEVER cached (so a blip can't poison the cache)
  * contact-info User-Agent + polite-pool mailto where the host asks for it

Everything here is FREE. Backends that need a (free) key are OFF unless their
env var is set: MASS_SEARXNG_URL, MASS_MOJEEK_KEY, MASS_OPENALEX_KEY.
"""
import os, re, json, time, html, gzip, hashlib, threading
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET

CONTACT = (os.environ.get("MASS_CONTACT")
           or os.environ.get("RIPOSTE_CONTACT")
           or "anonymous@example.com").strip()
_UA = f"MassSearch/0.2 (+https://localhost; {CONTACT})"

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def resolve_out_dir():
    """Absolute, writable output dir. Guards against an unexpanded ${HOME} or a
    relative MASS_OUT_DIR (some hosts don't substitute config vars) -> fall back
    to ~/mass-search so cache/out creation never lands in a read-only CWD."""
    d = os.environ.get("MASS_OUT_DIR", "").strip()
    if not d:
        return ""                                   # unset -> caller uses its default
    if ("${" in d) or ("%" in d) or (not os.path.isabs(d)):
        return os.path.join(os.path.expanduser("~"), "mass-search")
    return d


_OUTDIR = resolve_out_dir()
_CACHE = (os.environ.get("MASS_CACHE_DIR", "").strip()
          or (os.path.join(_OUTDIR, "cache") if _OUTDIR else os.path.join(_ROOT, "data", "cache")))
_DEFAULT_GAP = float(os.environ.get("MASS_SEARCH_GAP",
                     os.environ.get("RIPOSTE_SEARCH_GAP", "1.5")))
_DEFAULT_CAP = int(os.environ.get("MASS_HOST_CAP", "300"))
_CACHE_TTL = int(os.environ.get("MASS_CACHE_TTL", str(7 * 24 * 3600)))

# ---- per-host safety policy (keyed by netloc) -----------------------------
# gap = min seconds between hits; ban_prone = trip breaker on first block.
_HOST_GAP = {
    "html.duckduckgo.com": 3.0, "lite.duckduckgo.com": 3.0,
    "en.wikipedia.org": 1.0,
    "hn.algolia.com": 0.5,
    "lobste.rs": 2.5,
    "api2.marginalia-search.com": 2.0,
    "api.crossref.org": 1.0,
    "export.arxiv.org": 3.0,
    "api.semanticscholar.org": 3.0,
    "api.stackexchange.com": 2.0,
    "api.github.com": 6.0,
    "api.openalex.org": 1.0,
    "api.mojeek.com": 1.0,
}
_HOST_CAP = {"api.github.com": 30, "api2.marginalia-search.com": 120,
             "api.stackexchange.com": 100, "export.arxiv.org": 120}
_BAN_PRONE = {"html.duckduckgo.com", "lite.duckduckgo.com", "lobste.rs",
              "api2.marginalia-search.com", "export.arxiv.org",
              "api.semanticscholar.org", "api.stackexchange.com", "api.github.com"}

# ---- thread-safe throttle + circuit-breaker state -------------------------
_last = {}                       # host -> last-call epoch
_host_locks = {}                 # host -> lock held across its request
_reqs = {}                       # host -> requests made this run
_blocks = {}                     # host -> block signals seen this run
_tripped = set()                 # hosts disabled for the rest of this run
_meta = threading.Lock()
_state = threading.Lock()
_cache_lock = threading.Lock()


class Blocked(Exception):
    """A host refused / rate-limited us; the resolver should yield no results."""


def _host_lock(host):
    with _meta:
        lk = _host_locks.get(host)
        if lk is None:
            lk = _host_locks[host] = threading.Lock()
        return lk


def _gap_for(host):
    return _HOST_GAP.get(host, _DEFAULT_GAP)


def _throttle(host):
    now = time.time()
    wait = _gap_for(host) - (now - _last.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    time.sleep((int(hashlib.md5(host.encode()).hexdigest(), 16) % 400) / 1000.0)
    _last[host] = time.time()


def _reserve(host):
    """Breaker gate + per-run cap. Raises Blocked if the host is off-limits."""
    with _state:
        if host in _tripped:
            raise Blocked(f"{host} disabled this run")
        cap = _HOST_CAP.get(host, _DEFAULT_CAP)
        if _reqs.get(host, 0) >= cap:
            _tripped.add(host)
            raise Blocked(f"{host} hit per-run cap {cap}")
        _reqs[host] = _reqs.get(host, 0) + 1


def _register_block(host, why, soft=False):
    with _state:
        _blocks[host] = _blocks.get(host, 0) + 1
        n = _blocks[host]
        if soft:
            _HOST_GAP[host] = min(_gap_for(host) * 2, 30.0)   # slow this host down
        trip = ((host in _BAN_PRONE and not soft) or n >= 3)
        if trip:
            _tripped.add(host)
    tail = " -> HOST DISABLED for this run" if host in _tripped else \
           (f" -> backing off to {_gap_for(host):.0f}s" if soft else "")
    print(f"  [!] {host}: {why}{tail}")


def status_report():
    with _state:
        return {"requests": dict(_reqs), "blocks": dict(_blocks),
                "tripped": sorted(_tripped)}


# ---- core fetch (status-aware, gzip-aware, breaker-aware) ------------------
def _raw(method, url, params=None, headers=None, timeout=20):
    host = urllib.parse.urlparse(url).netloc
    _reserve(host)                       # breaker + cap (also counts the request)
    hdrs = {"User-Agent": _UA, "Accept-Language": "en-US,en", "Accept-Encoding": "gzip"}
    if headers:
        hdrs.update(headers)
    with _host_lock(host):
        _throttle(host)
        try:
            if method == "POST":
                data = urllib.parse.urlencode(params).encode()
                hdrs["Content-Type"] = "application/x-www-form-urlencoded"
                req = urllib.request.Request(url, data=data, headers=hdrs)
            else:
                req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                status = getattr(r, "status", None) or r.getcode() or 200
                raw = r.read()
                if "gzip" in (r.headers.get("Content-Encoding") or ""):
                    raw = gzip.decompress(raw)
                text = raw.decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            soft = e.code in (429, 503) and host not in _BAN_PRONE
            _register_block(host, f"HTTP {e.code}", soft=soft)
            raise Blocked(f"HTTP {e.code} on {host}")
        except Exception as e:
            raise Blocked(f"{type(e).__name__} on {host}: {e}")
    if status == 202 or status >= 400:               # DDG signals rate-limit via 202
        _register_block(host, f"status {status}")
        raise Blocked(f"status {status} on {host}")
    return text


def _get(url, headers=None, timeout=20):
    return _raw("GET", url, headers=headers, timeout=timeout)


def _post(url, params, headers=None, timeout=20):
    return _raw("POST", url, params=params, headers=headers, timeout=timeout)


def _strip(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


# ---- cache (never stores empty/blocked results) ---------------------------
def _cache_path(key):
    os.makedirs(_CACHE, exist_ok=True)
    h = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
    return os.path.join(_CACHE, h + ".json")


def _cached(key, ttl=_CACHE_TTL):
    p = _cache_path(key)
    if os.path.exists(p) and (time.time() - os.path.getmtime(p)) < ttl:
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            return None
    return None


def _store(key, value):
    if not value:                          # do NOT cache empties / blocks
        return value
    with _cache_lock:
        json.dump(value, open(_cache_path(key), "w", encoding="utf-8"), ensure_ascii=False)
    return value


def _run(name, key, fn):
    """cache-wrap a resolver body; swallow Blocked/errors into []."""
    hit = _cached(key)
    if hit is not None:
        return hit
    try:
        out = fn()
    except Blocked:
        return []
    except Exception:
        return []
    return _store(key, out)


# =========================================================================
# GENERAL-WEB resolvers (ban-prone -- handled with the biggest gaps + breaker)
# =========================================================================
def ddg(query, n=6):
    def body():
        for base in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
            try:
                page = _post(base, {"q": query, "kl": "us-en"})
            except Blocked:
                continue
            rows = _parse_ddg(page)
            if rows:
                return rows[:n]
        return []
    return _run("ddg", "ddg:" + query, body)[:n]


def _parse_ddg(page):
    out, seen = [], set()
    anchors = re.findall(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
    snippets = [_strip(s) for s in re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)]
    for i, (href, title) in enumerate(anchors):
        url = _ddg_unwrap(href)
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"title": _strip(title), "url": url, "source": "ddg",
                    "snippet": snippets[i] if i < len(snippets) else ""})
    if not out:
        for href, title in re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*class="result-link"[^>]*>(.*?)</a>', page, re.S):
            url = _ddg_unwrap(href)
            if url and url not in seen and "duckduckgo.com" not in url:
                seen.add(url)
                out.append({"title": _strip(title), "url": url, "snippet": "", "source": "ddg"})
    return out


def _ddg_unwrap(href):
    if href.startswith("//"):
        href = "https:" + href
    m = re.search(r"[?&]uddg=([^&]+)", href)
    if m:
        return urllib.parse.unquote(m.group(1))
    return href if href.startswith("http") else ""


def marginalia(query, n=5):
    """Independent 'small web' index. Keyless via the shared 'public' key."""
    def body():
        url = "https://api2.marginalia-search.com/search?query=" + urllib.parse.quote(query)
        data = json.loads(_get(url, headers={"API-Key": "public"}))
        out = []
        for r in (data.get("results") or [])[:n]:
            if r.get("url"):
                out.append({"title": r.get("title") or r["url"], "url": r["url"],
                            "snippet": _strip(r.get("description") or ""), "source": "marginalia"})
        return out
    return _run("marginalia", "marg:" + query, body)[:n]


def mojeek(query, n=5):
    """Independent index. OFF unless MASS_MOJEEK_KEY is set (free key: 2k/mo)."""
    keyv = os.environ.get("MASS_MOJEEK_KEY", "").strip()
    if not keyv:
        return []

    def body():
        url = ("https://api.mojeek.com/search?fmt=json&api_key=%s&q=%s"
               % (urllib.parse.quote(keyv), urllib.parse.quote(query)))
        data = json.loads(_get(url))
        res = (((data.get("response") or {}).get("results")) or data.get("results") or [])
        out = []
        for r in res[:n]:
            u = r.get("url") or r.get("link")
            if u:
                out.append({"title": _strip(r.get("title") or u), "url": u,
                            "snippet": _strip(r.get("desc") or r.get("description") or ""),
                            "source": "mojeek"})
        return out
    return _run("mojeek", "mojeek:" + query, body)[:n]


# =========================================================================
# STRUCTURED / VERTICAL resolvers (safe to run in parallel -- they want it)
# =========================================================================
def wikipedia(query, n=3):
    def body():
        api = ("https://en.wikipedia.org/w/api.php?action=query&list=search&format=json"
               "&srlimit=%d&srsearch=%s" % (n, urllib.parse.quote(query)))
        data = json.loads(_get(api))
        out = []
        for hitr in data.get("query", {}).get("search", []):
            title = hitr["title"]
            out.append({
                "title": "Wikipedia: " + title,
                "url": "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_")),
                "snippet": _wiki_summary(title) or _strip(hitr.get("snippet", "")),
                "source": "wiki",
            })
        return out
    return _run("wiki", "wiki:" + query, body)[:n]


def _wiki_summary(title):
    try:
        url = "https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"))
        return json.loads(_get(url)).get("extract", "")
    except Exception:
        return ""


def crossref(query, n=4):
    """Scholarly works / DOIs. Keyless; mailto -> polite pool."""
    def body():
        url = ("https://api.crossref.org/works?rows=%d&select=title,URL,DOI,container-title,abstract"
               "&mailto=%s&query=%s" % (n, urllib.parse.quote(CONTACT), urllib.parse.quote(query)))
        items = (json.loads(_get(url)).get("message") or {}).get("items") or []
        out = []
        for it in items[:n]:
            title = (it.get("title") or [""])[0]
            u = it.get("URL") or ("https://doi.org/" + it["DOI"] if it.get("DOI") else "")
            snip = _strip(it.get("abstract") or "") or (it.get("container-title") or [""])[0]
            if u:
                out.append({"title": title or u, "url": u, "snippet": snip, "source": "crossref"})
        return out
    return _run("crossref", "cref:" + query, body)[:n]


def arxiv(query, n=4):
    """Preprints. Keyless Atom API; be gentle (>=3s)."""
    def body():
        url = ("https://export.arxiv.org/api/query?sortBy=relevance&max_results=%d&search_query=all:%s"
               % (n, urllib.parse.quote(query)))
        root = ET.fromstring(_get(url))
        ns = {"a": "http://www.w3.org/2005/Atom"}
        out = []
        for e in root.findall("a:entry", ns)[:n]:
            title = (e.findtext("a:title", default="", namespaces=ns) or "").strip()
            u = (e.findtext("a:id", default="", namespaces=ns) or "").strip()
            summ = (e.findtext("a:summary", default="", namespaces=ns) or "").strip()
            if u:
                out.append({"title": title, "url": u,
                            "snippet": re.sub(r"\s+", " ", summ)[:400], "source": "arxiv"})
        return out
    return _run("arxiv", "arxiv:" + query, body)[:n]


def semanticscholar(query, n=4):
    """Papers. Keyless (~100 req / 5 min shared)."""
    def body():
        url = ("https://api.semanticscholar.org/graph/v1/paper/search?limit=%d"
               "&fields=title,url,abstract,year&query=%s" % (n, urllib.parse.quote(query)))
        data = json.loads(_get(url)).get("data") or []
        out = []
        for p in data[:n]:
            u = p.get("url")
            if u:
                yr = f" ({p['year']})" if p.get("year") else ""
                out.append({"title": (p.get("title") or u) + yr, "url": u,
                            "snippet": _strip(p.get("abstract") or "")[:400], "source": "s2"})
        return out
    return _run("s2", "s2:" + query, body)[:n]


def openalex(query, n=4):
    """Scholarly graph. mailto polite pool; MASS_OPENALEX_KEY used if set."""
    keyv = os.environ.get("MASS_OPENALEX_KEY", "").strip()

    def body():
        extra = ("&api_key=" + urllib.parse.quote(keyv)) if keyv else ""
        url = ("https://api.openalex.org/works?per_page=%d&mailto=%s%s&search=%s"
               % (n, urllib.parse.quote(CONTACT), extra, urllib.parse.quote(query)))
        res = json.loads(_get(url)).get("results") or []
        out = []
        for w in res[:n]:
            u = w.get("doi") or w.get("id")
            venue = ((w.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
            if u:
                out.append({"title": w.get("display_name") or u, "url": u,
                            "snippet": venue, "source": "openalex"})
        return out
    return _run("openalex", "oa:" + query, body)[:n]


def hackernews(query, n=5):
    def body():
        api = ("https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage=%d&query=%s"
               % (n, urllib.parse.quote(query)))
        hits = json.loads(_get(api)).get("hits", [])
        out = []
        for h in hits[:n]:
            u = h.get("url") or ("https://news.ycombinator.com/item?id=%s" % h.get("objectID"))
            out.append({"title": h.get("title") or "(HN)", "url": u,
                        "snippet": (h.get("story_text") or "")[:400] or
                        f"HN: {h.get('points', 0)} points, {h.get('num_comments', 0)} comments",
                        "source": "hn"})
        return out
    return _run("hn", "hn:" + query, body)[:n]


def stackexchange(query, n=5):
    """Stack Overflow Q&A. Keyless (light IP quota; gzip forced -> handled)."""
    def body():
        api = ("https://api.stackexchange.com/2.3/search/advanced?order=desc&sort=relevance"
               "&site=stackoverflow&pagesize=%d&q=%s" % (n, urllib.parse.quote(query)))
        data = json.loads(_get(api))
        if data.get("backoff"):
            _register_block("api.stackexchange.com", f"backoff {data['backoff']}s", soft=True)
        out = []
        for it in (data.get("items") or [])[:n]:
            u = it.get("link")
            if u:
                tags = ", ".join(it.get("tags", [])[:5])
                out.append({"title": _strip(it.get("title") or u), "url": u,
                            "snippet": f"score {it.get('score', 0)}, {it.get('answer_count', 0)} answers"
                                       + (f" [{tags}]" if tags else ""), "source": "stack"})
        return out
    return _run("stack", "stack:" + query, body)[:n]


def github(query, n=4):
    """Repos. Unauthenticated search (10/min) -- big gap, small cap."""
    def body():
        api = "https://api.github.com/search/repositories?per_page=%d&q=%s" % (n, urllib.parse.quote(query))
        data = json.loads(_get(api, headers={"Accept": "application/vnd.github+json"}))
        out = []
        for r in (data.get("items") or [])[:n]:
            out.append({"title": r.get("full_name") or "(repo)", "url": r.get("html_url", ""),
                        "snippet": (_strip(r.get("description") or "")
                                    + f"  ★{r.get('stargazers_count', 0)}")[:300], "source": "github"})
        return [r for r in out if r["url"]]
    return _run("github", "gh:" + query, body)[:n]


def searxng(query, n=6):
    """Local self-hosted meta-engine. OFF unless MASS_SEARXNG_URL is set."""
    base = os.environ.get("MASS_SEARXNG_URL", "").strip().rstrip("/")
    if not base:
        return []

    def body():
        url = base + "/search?format=json&q=" + urllib.parse.quote(query)
        data = json.loads(_get(url))
        out = []
        for r in (data.get("results") or [])[:n]:
            u = r.get("url")
            if u:
                out.append({"title": _strip(r.get("title") or u), "url": u,
                            "snippet": _strip(r.get("content") or "")[:400],
                            "source": "searx:" + (r.get("engine") or "")})
        return out
    return _run("searxng", "searx:" + query, body)[:n]


# ---- registry + groups ----------------------------------------------------
BACKENDS = {
    "ddg": ddg, "marginalia": marginalia, "mojeek": mojeek,
    "wiki": wikipedia, "crossref": crossref, "arxiv": arxiv,
    "semanticscholar": semanticscholar, "openalex": openalex,
    "hn": hackernews, "stackexchange": stackexchange,
    "github": github, "searxng": searxng,
}
GROUPS = {
    "web":      ["ddg", "marginalia", "mojeek", "wiki", "searxng"],
    "academic": ["crossref", "openalex", "semanticscholar", "arxiv", "wiki"],
    "tech":     ["hn", "stackexchange", "github"],
    "all":      list(BACKENDS),
}
# key-gated backends are skipped unless their env var is present
_KEY_GATED = {"mojeek": "MASS_MOJEEK_KEY", "searxng": "MASS_SEARXNG_URL"}
DEFAULT_BACKENDS = ["ddg", "wiki", "hn"]


def available(name):
    env = _KEY_GATED.get(name)
    return (name in BACKENDS) and (env is None or os.environ.get(env, "").strip() != "")


def resolve_backends(spec):
    """Expand group names, drop unknowns + unavailable key-gated backends."""
    names = []
    for tok in spec:
        names.extend(GROUPS.get(tok, [tok]))
    seen, out = set(), []
    for name in names:
        if name in seen or name not in BACKENDS:
            continue
        seen.add(name)
        if available(name):
            out.append(name)
    return out or [b for b in DEFAULT_BACKENDS if available(b)]


# ---- query build + combined gather ----------------------------------------
_PRONOUN_START = re.compile(r"^(he|his|him|she|her|they|their|them|it|its|this|"
                            r"that|these|those|we|our|i|you|there)\b", re.I)


def build_query(claim, anchor=None):
    q = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", claim)
    q = re.sub(r"\s+", " ", q).strip(" -—.?!,")
    if anchor and (_PRONOUN_START.match(q) or len(q.split()) < 6):
        if anchor.lower() not in q.lower():
            q = f"{anchor} {q}"
    return " ".join(q.split()[:18])


def gather(query, backends=None, per_backend=6, anchor=None):
    q = build_query(query, anchor)
    backends = backends or DEFAULT_BACKENDS
    seen, out = set(), []
    for name in backends:
        fn = BACKENDS.get(name)
        if not fn or not available(name):
            continue
        try:
            rows = fn(q, per_backend)
        except Exception:
            rows = []
        for r in rows:
            u = r.get("url", "")
            if u and u not in seen:
                seen.add(u)
                out.append(r)
    return out


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "field-aligned quad remeshing"
    names = resolve_backends(["all"])
    print("backends:", ", ".join(names))
    for r in gather(q, backends=names):
        print(f"- [{r.get('source','?'):10}] {r['title'][:60]}\n    {r['url']}")
    print("status:", status_report())
