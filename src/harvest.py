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
import os, json, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import search, extract, synth, brain

from .search import resolve_out_dir

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


def run(queries, slug, goal="", backends=None, workers=6,
        per_backend=6, do_extract=True, do_synth=True, on_progress=None):
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

    return consolidate(slug, queries, goal, backends, do_synth=do_synth)


def consolidate(slug, queries=None, goal="", backends=None, do_synth=True):
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

    corpus = {
        "slug": slug,
        "goal": goal,
        "backends": backends or search.DEFAULT_BACKENDS,
        "engine": brain.engine_info(),
        "safety": search.status_report(),
        "n_queries": len(records),
        "n_sources": len(sources),
        "n_facts": len(all_facts),
        "queries": queries or [r["query"] for r in records],
        "records": records,
        "sources": sources,
        "facts": all_facts,
    }
    corpus["report"] = synth.synthesize(goal, all_facts) if do_synth else None

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
    L.append(f"_synthesized by {c['engine']} from {c['n_facts']} facts across "
             f"{c['n_queries']} searches · confidence {r.get('confidence', 0):.0%}_\n")
    L.append("## Answer\n")
    L.append(r.get("answer", "") + "\n")
    if r.get("key_findings"):
        L.append("## Key findings\n")
        L.extend(f"- {x}" for x in r["key_findings"])
        L.append("")
    if r.get("open_questions"):
        L.append("## Open questions\n")
        L.extend(f"- {x}" for x in r["open_questions"])
        L.append("")
    L.append("## Top sources\n")
    for s in top_sources(c, 12):
        L.append(f"- [{s.get('title', '(untitled)')}]({s.get('url', '')})"
                 + (f" `{s.get('source')}`" if s.get("source") else ""))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(L))


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
