"""
harvest.py - the mass-search engine core.

Runs a whole list of queries through the keyless search backends in a thread
pool, optionally distills each with the local LLM, and STREAMS every record to
disk the moment it's ready. The per-host locks in search.py keep us polite no
matter how many workers we run: workers overlap across hosts + local-LLM work,
never bursting a single site.

Outputs (in out/):
  <slug>.jsonl  - one JSON record per query, appended live (resume reads this)
  <slug>.json   - consolidated corpus: config + all records + deduped sources
  <slug>.md     - human-readable report

Resumable: rerun the same slug and already-done queries are skipped.
"""
import os, re, json, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import search, extract, synth, deepread, brain

from .search import resolve_out_dir

# stopwords stripped from a goal when scoring source topical relevance
_STOP = set("the a an of for to in on at by and or with is are was were be been "
            "how what which who whom whose why when where best most top vs list "
            "lists give me find about into over your you i we they it that this "
            "com www http https org net html".split())


def _goal_tokens(goal):
    return {t for t in re.findall(r"[a-z0-9]{3,}", (goal or "").lower()) if t not in _STOP}


_LIST_TITLE = re.compile(r"\b\d+\+?\b.{0,30}\b(words|phrases|terms|list|examples|tips|ways)\b", re.I)
_LIST_HINT = re.compile(r"\b(list|words|phrases|terms|avoid|overused|common|examples|banned|glossary)\b", re.I)


# ---- cross-process campaign lock ------------------------------------------
# The circuit-breaker + per-host throttle state is per-PROCESS. If two campaigns
# ran as separate detached processes at once, each would think it alone owns a
# host -> real hit-rate doubles and the ban protection is void. So only ONE
# campaign runs at a time, machine-wide, enforced by this lock. Stale locks
# (crash / kill) expire after LOCK_TTL so a dead campaign never wedges the tool.
LOCK_TTL = int(os.environ.get("MASS_LOCK_TTL", str(45 * 60)))


def _lock_path():
    os.makedirs(OUT, exist_ok=True)
    return os.path.join(OUT, ".campaign.lock")


def _pid_alive(pid):
    """True if process `pid` is still running (cross-platform, stdlib-only)."""
    if not pid:
        return False
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
            if not h:
                return False
            alive = ctypes.windll.kernel32.WaitForSingleObject(h, 0) == 0x102     # WAIT_TIMEOUT
            ctypes.windll.kernel32.CloseHandle(h)
            return alive
        except Exception:
            return True                              # can't tell -> assume alive (safe)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def campaign_lock():
    """Return the active campaign lock {slug,pid,started} or None. Clears the lock
    if it's stale (past TTL) OR its owning process is no longer alive (crash)."""
    p = _lock_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = None
    stale = (not d) or (time.time() - d.get("started", 0)) > LOCK_TTL or not _pid_alive(d.get("pid"))
    if stale:
        try:
            os.remove(p)
        except OSError:
            pass
        return None
    return d


def acquire_campaign_lock(slug):
    """Atomically claim the single-campaign slot. False if one is already running."""
    if campaign_lock() is not None:                   # also clears a stale lock
        return False
    try:
        fd = os.open(_lock_path(), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"slug": slug, "pid": os.getpid(), "started": time.time()}, f)
    return True


def release_campaign_lock():
    try:
        os.remove(_lock_path())
    except OSError:
        pass


def rank_sources_for_goal(goal, records, k, enumerable=False):
    """Pick the K most ON-TOPIC sources to deep-read (goal-keyword overlap + the
    relevance of the query that found them + a list-page bonus). This keeps
    off-topic pages that merely rode in on a high-relevance query OUT of the
    expensive full-body read -- the main fix for deep-read noise."""
    toks = _goal_tokens(goal)
    best = {}
    for rec in records:
        rel = (rec.get("digest") or {}).get("relevance", 0.0) or 0.0
        for r in rec.get("results", []):
            u = r.get("url", "")
            if not u:
                continue
            title = (r.get("title") or "")
            hay = (title + " " + (r.get("snippet") or "") + " " + u).lower()
            overlap = sum(1 for t in toks if t in hay)
            score = overlap * 2.0 + rel
            if enumerable and title:
                if _LIST_TITLE.search(title):
                    score += 4.0
                elif _LIST_HINT.search(title):
                    score += 1.5
            if u not in best or score > best[u][0]:
                best[u] = (score, overlap, r)
    ranked = sorted(best.values(), key=lambda x: x[0], reverse=True)
    on_topic = [r for sc, ov, r in ranked if ov > 0]      # require real keyword overlap
    picked = (on_topic or [r for sc, ov, r in ranked])[:k]
    return picked


DISCOVER_N = int(os.environ.get("MASS_DISCOVER_N", "6"))

# social share / intent URLs carry the target's keywords in a ?url= param and
# would falsely match; media/binary files aren't parseable as HTML text.
_SHARE = re.compile(r"(twitter\.com|//x\.com|facebook\.com|linkedin\.com/(sharing|share)|"
                    r"/sharer|/sharearticle|/intent/|pinterest\.|reddit\.com/submit|"
                    r"t\.me/share|wa\.me|api\.whatsapp|mailto:|tel:|/rss|/feed/?$)", re.I)
_BADEXT = re.compile(r"\.(mp4|mov|avi|webm|mp3|wav|zip|gz|tar|rar|7z|pdf|docx?|xlsx?|pptx?|"
                     r"jpe?g|png|gif|webp|svg|ico|css|js|json|xml|woff2?|ttf|eot)(\?|#|$)", re.I)


def _followable(url):
    return bool(url) and not (_SHARE.search(url) or _BADEXT.search(url))


def discover_urls(seed_docs, goal, extra, seen, enumerable=False):
    """Find on-topic pages the resolvers never returned, by following the most
    relevant CONTENT-AREA links out of the seed pages (anchor/URL goal-overlap +
    a list-page bonus). This reaches sources outside every search backend, on the
    same ban-safe fetch path. Requires real topical signal so we don't wander."""
    toks = _goal_tokens(goal)
    if not toks:
        return []
    best = {}
    for d in seed_docs:
        for url, anchor in d.get("links", []):
            if url in seen or not _followable(url):
                continue
            hay = (anchor + " " + url).lower()
            ov = sum(1 for t in toks if t in hay)
            if ov == 0:                                   # must be on-topic to follow
                continue
            score = ov * 2.0
            if enumerable and anchor:
                if _LIST_TITLE.search(anchor):
                    score += 3.0
                elif _LIST_HINT.search(anchor):
                    score += 1.0
            if url not in best or score > best[url][0]:
                best[url] = (score, anchor)
    ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)
    return [{"url": u, "title": a} for u, (s, a) in ranked[:extra]]

OUT = (resolve_out_dir()
       or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "out"))


def _paths(slug):
    os.makedirs(OUT, exist_ok=True)
    return (os.path.join(OUT, slug + ".jsonl"),
            os.path.join(OUT, slug + ".json"),
            os.path.join(OUT, slug + ".md"),
            os.path.join(OUT, slug + ".report.md"))


def _done_queries(jsonl_path):
    """Queries already recorded in a prior run (for resume)."""
    done = set()
    if os.path.exists(jsonl_path):
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    done.add(json.loads(line)["query"])
                except Exception:
                    pass
    return done


def _load_records(jsonl_path):
    recs = []
    if os.path.exists(jsonl_path):
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        pass
    return recs


def run(queries, slug, goal="", backends=None, workers=6, per_backend=6,
        do_extract=True, do_synth=True, do_deepread=True, do_discover=True, on_progress=None):
    """
    Harvest `queries` into out/<slug>.*  Returns the consolidated corpus dict.
    on_progress(done, total, record) is called after each query completes.
    """
    jsonl_path, json_path, md_path, report_path = _paths(slug)
    backends = backends or search.DEFAULT_BACKENDS

    done = _done_queries(jsonl_path)
    todo = [q for q in queries if q not in done]
    total = len(queries)

    write_lock = threading.Lock()
    counter = {"n": len(done)}
    jf = open(jsonl_path, "a", encoding="utf-8")

    def work(q):
        results = search.gather(q, backends=backends, per_backend=per_backend)
        digest = extract.extract(goal, q, results) if do_extract else None
        return {
            "query": q,
            "n_results": len(results),
            "digest": digest,
            "results": results,
        }

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(work, q): q for q in todo}
            for fut in as_completed(futs):
                q = futs[fut]
                try:
                    rec = fut.result()
                except Exception as e:
                    rec = {"query": q, "n_results": 0, "digest": None,
                           "results": [], "error": str(e)}
                with write_lock:
                    jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    jf.flush()
                    counter["n"] += 1
                    if on_progress:
                        on_progress(counter["n"], total, rec)
    finally:
        jf.close()

    return consolidate(slug, queries, goal, backends, do_synth=do_synth,
                       do_deepread=do_deepread, do_discover=do_discover)


def consolidate(slug, queries=None, goal="", backends=None, do_synth=True,
                do_deepread=True, do_discover=True):
    """Fold the jsonl into <slug>.json + <slug>.md (+ synthesized report)."""
    jsonl_path, json_path, md_path, report_path = _paths(slug)
    records = _load_records(jsonl_path)

    # dedupe the whole source corpus across every query
    seen, sources, all_facts = set(), [], []
    for rec in records:
        d = rec.get("digest") or {}
        for f in d.get("facts", []):
            all_facts.append({"fact": f, "query": rec["query"]})
        for r in rec.get("results", []):
            u = r.get("url", "")
            if u and u not in seen:
                seen.add(u)
                sources.append(r)

    # P1: deep-read the top ON-TOPIC sources' FULL pages -- this is where verbatim
    # lists/details come from. List goals parse the DOM structure directly (every
    # <li>/<td> entry, no LLM retyping loss); prose goals distill the body via LLM.
    deep_docs = disc_docs = 0
    enum = extract.is_enumerable(goal)
    if do_deepread and sources and (enum or brain.has_llm()):
        ranked = rank_sources_for_goal(goal, records, deepread.DEFAULT_K, enumerable=enum)
        docs = deepread.read_sources(ranked, k=deepread.DEFAULT_K,
                                     want_items=enum, want_links=do_discover)
        # DISCOVERY: follow the seeds' best on-topic links to reach pages no
        # resolver returned, then read those too (same ban-safe fetch path).
        if do_discover and docs:
            seen_urls = {d["url"] for d in docs}
            found = discover_urls(docs, goal, DISCOVER_N, seen_urls, enumerable=enum)
            if found:
                more = deepread.read_sources(found, k=DISCOVER_N, want_items=enum)
                disc_docs = len(more)
                docs += more
        deep_docs = len(docs)
        xmodel = brain.extract_model()
        for d in docs:
            tag = "deep-read: " + d["url"]
            if enum:
                for it in d.get("items", []):        # verbatim DOM items -> corroboration filters noise
                    all_facts.append({"fact": it, "query": tag})
            else:
                for f in extract.extract_deep(goal, d["url"], d["text"], enumerable=False, model=xmodel):
                    all_facts.append({"fact": f, "query": tag})

    corpus = {
        "slug": slug,
        "goal": goal,
        "backends": backends or search.DEFAULT_BACKENDS,
        "engine": brain.engine_info(),
        "safety": search.status_report(),
        "n_queries": len(records),
        "n_sources": len(sources),
        "n_deep_read": deep_docs,
        "n_discovered": disc_docs,
        "n_facts": len(all_facts),
        "queries": queries or [r["query"] for r in records],
        "records": records,
        "sources": sources,
        "facts": all_facts,
    }
    corpus["report"] = synth.synthesize(goal, all_facts) if do_synth else None

    # P7: replace the model's self-graded confidence with a grounded score
    # (mean query relevance x deep-read coverage x fact volume). Enumerable mode
    # computes its own corroboration-based confidence, so leave that one alone.
    rep = corpus["report"]
    if rep and not rep.get("enumerated"):
        rels = [(r.get("digest") or {}).get("relevance", 0.0) or 0.0 for r in records if r.get("digest")]
        mrel = sum(rels) / len(rels) if rels else 0.0
        cov = min(1.0, deep_docs / float(deepread.DEFAULT_K))
        vol = min(1.0, len(all_facts) / 40.0)
        rep["confidence"] = round(min(0.9, 0.20 + 0.45 * mrel + 0.20 * cov + 0.15 * vol), 2)

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)
    _write_md(md_path, corpus)
    if corpus["report"]:
        _write_report(report_path, corpus)
    return corpus


def top_sources(corpus, n=10):
    """Best sources for the goal: prefer ones the LLM rated relevant, then dedupe."""
    ranked, seen = [], set()
    for rec in corpus.get("records", []):
        rel = (rec.get("digest") or {}).get("relevance", 0.0)
        for r in rec.get("results", []):
            u = r.get("url", "")
            if u and u not in seen:
                seen.add(u)
                ranked.append((rel, r))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in ranked[:n]]


def _write_report(path, c):
    r = c["report"]
    L = [f"# Answer: {c['slug']}\n"]
    if c["goal"]:
        L.append(f"**Goal:** {c['goal']}\n")
    dr = f" · {c.get('n_deep_read', 0)} pages deep-read" if c.get("n_deep_read") else ""
    dc = f" ({c['n_discovered']} via link-following)" if c.get("n_discovered") else ""
    L.append(f"_synthesized by {c['engine']} from {c['n_facts']} facts across "
             f"{c['n_queries']} searches{dr}{dc} · confidence {r.get('confidence', 0):.0%}_\n")
    L.append("## Answer\n")
    L.append(r.get("answer", "") + "\n")
    if r.get("key_findings"):
        L.append(f"## Compiled list ({len(r['key_findings'])} items)\n"
                 if r.get("enumerated") else "## Key findings\n")
        L.extend(f"- {x}" for x in r["key_findings"])
        L.append("")
    if r.get("open_questions"):
        L.append("## Open questions\n")
        L.extend(f"- {x}" for x in r["open_questions"])
        L.append("")
    L.append("## Top sources\n")
    enum = bool((c.get("report") or {}).get("enumerated"))
    srcs = (rank_sources_for_goal(c.get("goal", ""), c.get("records", []), 12, enumerable=enum)
            if c.get("records") else None) or top_sources(c, 12)
    for s in srcs:
        L.append(f"- [{s.get('title', '(untitled)')}]({s.get('url', '')})"
                 + (f" `{s.get('source')}`" if s.get("source") else ""))
    L.append("\n---")
    L.append(_safety_line(c))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


def _safety_line(c):
    """Human-readable ban-safety footer — the whole selling point, surfaced (P4)."""
    s = c.get("safety") or {}
    reqs = s.get("requests") or {}
    tripped = s.get("tripped") or []
    total, hosts = sum(reqs.values()), len(reqs)
    drop = (f" · {len(tripped)} auto-dropped on block signals ({', '.join(tripped)})"
            if tripped else " · 0 hosts dropped")
    return f"_ban-safety: {total} requests across {hosts} hosts{drop} · no bans_"


def _write_md(path, c):
    L = []
    L.append(f"# Mass Search: {c['slug']}\n")
    if c["goal"]:
        L.append(f"**Goal:** {c['goal']}\n")
    L.append(f"**Engine:** {c['engine']}  |  **Backends:** {', '.join(c['backends'])}")
    L.append(f"**{c['n_queries']} queries · {c['n_sources']} unique sources · "
             f"{c['n_facts']} extracted facts**\n")

    if c["facts"]:
        L.append("## Key facts harvested\n")
        for item in c["facts"]:
            L.append(f"- {item['fact']}  \n  _(from: {item['query']})_")
        L.append("")

    L.append("## Per-query results\n")
    for rec in c["records"]:
        L.append(f"### {rec['query']}  ({rec.get('n_results', 0)} hits)")
        d = rec.get("digest")
        if d and d.get("summary"):
            L.append(f"> {d['summary']}")
        for r in rec.get("results", [])[:8]:
            src = r.get("source", "")
            L.append(f"- [{r.get('title','(untitled)')}]({r.get('url','')})"
                     + (f" `{src}`" if src else ""))
        if rec.get("error"):
            L.append(f"- _error: {rec['error']}_")
        L.append("")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
