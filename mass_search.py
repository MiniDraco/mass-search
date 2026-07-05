#!/usr/bin/env python3
"""
mass_search.py - spin up a keyless, local-hardware mass search campaign.

Forked from Riposte's fact-check search bot. Instead of checking one video's
claims, it takes a topic (or a query list), fans it into many searches, runs
them in parallel across keyless backends, distills each with your LOCAL LLM,
and writes everything to disk -- no API key, no per-call quota. Your hardware
is the limit, not someone's usage cap.

Usage:
  # topic -> expand into N queries -> harvest (local LLM distills each)
  python mass_search.py "field-aligned quad retopology" --queries 30 --workers 8

  # give it your own query list, one per line, run them verbatim
  python mass_search.py --file queries.txt --slug mytopic

  # pass literal queries on the command line, skip LLM expansion
  python mass_search.py --raw "quad remesh blender" "instant meshes flow field"

  # more reach: add Hacker News + Lobsters backends; skip LLM distill (faster)
  python mass_search.py "webgpu inference" --backends ddg,wiki,hn,lobste --no-extract

Rerun the same --slug to RESUME (already-done queries are skipped).
Outputs land in out/<slug>.jsonl (live), out/<slug>.json, out/<slug>.md.
"""
import os, sys, re, argparse, textwrap

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src import brain, search, expand, harvest


def _wrap(text, indent=0):
    body = textwrap.fill(text or "", width=76, subsequent_indent=" " * indent)
    return body


def _slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower())[:48].strip("-")   # strip after truncate (P5)
    return (s or "search")


def _load_file(path):
    with open(path, encoding="utf-8") as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]


def _progress(done, total, rec):
    d = rec.get("digest") or {}
    rel = f" rel={d['relevance']:.1f}" if d else ""
    facts = f" +{len(d['facts'])} facts" if d else ""
    tag = "!" if rec.get("error") else " "
    print(f"  [{done:>3}/{total}]{tag}{rec['n_results']:>2} hits{facts}{rel}  {rec['query'][:60]}")


def main():
    ap = argparse.ArgumentParser(description="Keyless local-hardware mass search.")
    ap.add_argument("seed", nargs="*", help="topic to expand, or literal queries with --raw")
    ap.add_argument("--file", help="read queries from a file (one per line, verbatim)")
    ap.add_argument("--raw", action="store_true", help="treat positional args as literal queries (no LLM expansion)")
    ap.add_argument("--slug", help="output name (default: derived from the seed)")
    ap.add_argument("--goal", help="the campaign goal used to steer LLM distillation (default: the seed)")
    ap.add_argument("--queries", type=int, default=24, help="how many queries to expand the topic into")
    ap.add_argument("--workers", type=int, default=6, help="parallel search workers")
    ap.add_argument("--per-backend", type=int, default=6, help="results to keep per backend per query")
    ap.add_argument("--backends", default="web",
                    help="groups (web|academic|tech|all) or a comma list of: "
                         + ",".join(search.BACKENDS))
    ap.add_argument("--no-extract", action="store_true", help="skip the local-LLM distillation step")
    ap.add_argument("--no-synth", action="store_true", help="skip the final synthesis (the answer)")
    ap.add_argument("--no-deepread", action="store_true",
                    help="skip reading the top sources' full page bodies (snippets only)")
    ap.add_argument("--no-discover", action="store_true",
                    help="skip following the seeds' on-topic links to new sources")
    ap.add_argument("--dry-run", action="store_true", help="just print the expanded queries and exit")
    args = ap.parse_args()

    backends = search.resolve_backends([b.strip() for b in args.backends.split(",") if b.strip()])

    # ---- build the query list ------------------------------------------------
    if args.file:
        queries = _load_file(args.file)
        goal = args.goal or (queries[0] if queries else "")
        slug = args.slug or _slugify(os.path.splitext(os.path.basename(args.file))[0])
    elif args.raw:
        queries = args.seed
        goal = args.goal or " / ".join(queries[:2])
        slug = args.slug or _slugify(queries[0] if queries else "raw")
    else:
        seed = " ".join(args.seed).strip()
        if not seed:
            ap.error("give a topic, or use --file / --raw")
        goal = args.goal or seed
        slug = args.slug or _slugify(seed)
        print(f"Engine: {brain.engine_info()}")
        print(f"Expanding \"{seed}\" into up to {args.queries} queries ...")
        queries = expand.expand(seed, args.queries)

    if not queries:
        ap.error("no queries to run")

    print(f"\n== Mass Search: {slug} ==")
    print(f"Engine:   {brain.engine_info()}")
    print(f"Backends: {', '.join(backends)}   Workers: {args.workers}")
    print(f"Queries:  {len(queries)}   Distill: {'off' if args.no_extract else 'on'}")
    print("-" * 60)
    for i, q in enumerate(queries, 1):
        print(f"  {i:>2}. {q}")
    if args.dry_run:
        return

    print("-" * 60)
    corpus = harvest.run(
        queries, slug, goal=goal, backends=backends,
        workers=args.workers, per_backend=args.per_backend,
        do_extract=not args.no_extract, do_synth=not args.no_synth,
        do_deepread=not args.no_deepread, do_discover=not args.no_discover,
        on_progress=_progress,
    )
    print("-" * 60)
    rep = search.status_report()
    if rep["tripped"]:
        print(f"SAFETY: auto-disabled this run (hit a block signal): {', '.join(rep['tripped'])}")
    print(f"DONE. {corpus['n_queries']} queries -> "
          f"{corpus['n_sources']} unique sources, {corpus['n_facts']} facts.")

    r = corpus.get("report")
    if r:
        print("\n" + "=" * 60)
        print(f"ANSWER  (confidence {r.get('confidence', 0):.0%})")
        print("=" * 60)
        print(_wrap(r.get("answer", "")))
        if r.get("key_findings"):
            print("\nKey findings:")
            for x in r["key_findings"][:8]:
                print(f"  - {_wrap(x, indent=4)}")
        if r.get("open_questions"):
            print("\nOpen questions:")
            for x in r["open_questions"][:5]:
                print(f"  - {_wrap(x, indent=4)}")
        print("\nTop sources:")
        for s in harvest.top_sources(corpus, 8):
            print(f"  - {s.get('url', '')}")
        print("=" * 60)

    print(f"\n  out/{slug}.jsonl        (live per-query records)")
    print(f"  out/{slug}.json         (consolidated corpus)")
    print(f"  out/{slug}.md           (all results)")
    if r:
        print(f"  out/{slug}.report.md    (the answer + top sources)")


if __name__ == "__main__":
    main()
