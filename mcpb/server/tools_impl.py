#!/usr/bin/env python3
"""
tools_impl.py - the actual Mass Search tool implementations.

Called by worker.py (which the Node MCP transport spawns per tool call). Kept
separate from any transport so it's trivially testable. All engine print()s are
redirected to stderr by the caller, so nothing here needs to worry about stdout.
"""
import sys, os, re, json, contextlib, subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from masssearch import brain, search, expand, harvest   # noqa: E402


def _slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:48]
    return s or "search"


def _resolve(backends):
    spec = [b.strip() for b in (backends or "web").split(",") if b.strip()]
    return search.resolve_backends(spec)


def _quiet(fn, *a, **k):
    with contextlib.redirect_stdout(sys.stderr):
        return fn(*a, **k)


def tool_web_search(args):
    query = (args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    names = _resolve(args.get("backends"))
    per = int(args.get("per_backend", 6))
    rows = _quiet(search.gather, query, backends=names, per_backend=per)
    trip = search.status_report()["tripped"]
    lines = [f"{len(rows)} results for \"{query}\"  (resolvers: {', '.join(names)})"]
    if trip:
        lines.append(f"(auto-disabled this call after a block signal: {', '.join(trip)})")
    for r in rows:
        lines.append(f"\n- {r.get('title','(untitled)')}  [{r.get('source','?')}]\n  {r.get('url','')}")
        if r.get("snippet"):
            lines.append(f"  {r['snippet'][:240]}")
    return "\n".join(lines)


def run_campaign(params):
    """The actual campaign work (called by run_campaign.py in the background)."""
    question = params["question"]
    names = _resolve(params.get("backends"))
    _quiet(_do_campaign, question, int(params.get("queries", 12)), names,
           int(params.get("workers", 6)), bool(params.get("synth", True)),
           bool(params.get("extract", True)))


def _do_campaign(question, n, names, workers, do_synth, do_extract):
    slug = _slug(question)
    queries = expand.expand(question, n)
    return harvest.run(queries, slug, goal=question, backends=names, workers=workers,
                       do_extract=do_extract, do_synth=do_synth, on_progress=None)


def tool_mass_search(args):
    question = (args.get("question") or "").strip()
    if not question:
        raise ValueError("question is required")
    names = _resolve(args.get("backends"))
    n = int(args.get("queries", 12))
    slug = _slug(question)
    os.makedirs(harvest.OUT, exist_ok=True)

    # A campaign is minutes of local-LLM work -> longer than the MCP call timeout.
    # Launch it DETACHED (survives this worker exiting) and return the slug; the
    # answer is fetched with read_campaign once it lands on disk.
    base = os.path.join(harvest.OUT, slug)
    for ext in (".report.md", ".done", ".log"):     # clear stale markers for a fresh run
        try:
            os.remove(base + ext)
        except OSError:
            pass
    params = {"question": question, "queries": n, "backends": args.get("backends", "web"),
              "workers": int(args.get("workers", 6)), "synth": bool(args.get("synth", True)),
              "extract": bool(args.get("extract", True))}
    runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_campaign.py")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    logf = open(base + ".log", "w", encoding="utf-8")
    subprocess.Popen([sys.executable, runner, json.dumps(params)],
                     stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
                     cwd=harvest.OUT, close_fds=True, **kw)
    est = max(1, round(n * 8 / 60))
    return (f"Campaign '{slug}' started in the background.\n"
            f"  question : {question}\n"
            f"  plan     : expand into {n} queries, harvest across [{', '.join(names)}], "
            f"distill + synthesize\n"
            f"  runs on local hardware, ~{est} min. Nothing is blocked.\n\n"
            f"Call  read_campaign(slug=\"{slug}\")  in a bit to get the synthesized answer "
            f"(it'll report progress if still running).")


def tool_read_campaign(args):
    slug = _slug(args.get("slug") or "")
    section = (args.get("section") or "report").lower()
    base = os.path.join(harvest.OUT, slug)
    report, results, jsonl = base + ".report.md", base + ".md", base + ".jsonl"

    want = {"report": report, "answer": report, "md": results,
            "results": results}.get(section, base + ".json")

    if os.path.exists(want) and os.path.exists(base + ".done"):
        with open(want, encoding="utf-8") as f:
            return f.read()[:20000]

    # not finished (or no marker yet): report progress instead of a stale/missing file
    if os.path.exists(jsonl) or os.path.exists(base + ".log"):
        try:
            done_n = sum(1 for _ in open(jsonl, encoding="utf-8")) if os.path.exists(jsonl) else 0
        except OSError:
            done_n = 0
        finished = os.path.exists(base + ".done")
        if finished and os.path.exists(want):
            with open(want, encoding="utf-8") as f:
                return f.read()[:20000]
        tail = ""
        if os.path.exists(base + ".log"):
            try:
                with open(base + ".log", encoding="utf-8") as f:
                    tail = f.read()[-500:]
            except OSError:
                pass
        state = "finished" if finished else "still running"
        return (f"Campaign '{slug}' {state}: {done_n} queries harvested so far.\n"
                f"Call read_campaign(slug=\"{slug}\") again shortly for the answer."
                + (f"\n\n[log tail]\n{tail}" if tail else ""))

    avail = []
    if os.path.isdir(harvest.OUT):
        avail = sorted({f.split(".")[0] for f in os.listdir(harvest.OUT)
                        if f.endswith((".json", ".md", ".jsonl"))})
    return f"No campaign '{slug}'. Available slugs: {', '.join(avail) or '(none)'}"


DISPATCH = {"web_search": tool_web_search, "mass_search": tool_mass_search,
            "read_campaign": tool_read_campaign}
