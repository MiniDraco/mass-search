"""
census.py - the UBIQUITY engine: capture (nearly) every mention of an entity
type across the web, not the clean subset a knowledge base already catalogued.

Where the normal enumerable path optimizes PRECISION (corroboration filters to
items on 2+ sources), a census optimizes RECALL: it harvests mentions from
ARBITRARY prose (any page, via extract.extract_mentions -- not just listicles),
keeps EVERYTHING (a single mention is still a mention), and LOOPS -- expanding
queries from what it's found and spidering links -- until new finds dry up.

`scope` is the soft control on how wide it casts and how much comes back:
  quick      -> handled by the normal single-pass path (not census)
  broad      -> a couple of rounds, moderate reach
  exhaustive -> many rounds, loop to saturation, widest reach

Every fetch still rides the ban-safe per-host plumbing (lock + gap + breaker +
cap). Ubiquity spreads one light touch across MANY hosts -- the polite pattern.
Output is compatible with read_campaign; items are ranked by how many distinct
sources mention them, with the count kept so you can threshold the returns.
"""
import re
import json
from concurrent.futures import ThreadPoolExecutor

from . import search, expand, extract, deepread, brain, harvest
from .synth import _JUNK        # shared page-chrome/boilerplate filter

# extra boilerplate that DOM/prose harvesting drags in on wiki/list pages
_META = re.compile(
    r"^(references?|notes?|see also|external links?|further reading|bibliography|"
    r"citations?|sources?|contents?|categor(y|ies)|hidden categ|navigation|"
    r"articles? (with|containing|needing|using|lacking)|short description|"
    r"webarchive|cs1|wikidata|isbn|doi|retrieved|archived|edit|view source|talk|"
    r"disambiguation|stub|portal|glossary|index|main page|full list|list of)\b", re.I)


def _keep(item):
    """Filter obvious non-entities (page-chrome, metadata, single chars)."""
    v = item.strip()
    if len(v) < 2 or not re.search(r"[A-Za-z]", v):
        return False
    if _JUNK.search(v) or _META.search(v):
        return False
    return True

SCOPES = {
    "broad":      {"rounds": 2, "q0": 12, "qn": 10, "k": 10, "disc": 8,  "per": 6, "sat": 15},
    "exhaustive": {"rounds": 5, "q0": 18, "qn": 14, "k": 12, "disc": 12, "per": 8, "sat": 8},
}

_MORE_PROMPT = """We are compiling EVERY {entity} mentioned anywhere on the web.
So far we've found examples like:
{sample}

Generate {n} NEW web search queries that would surface {entity}s we have NOT
found yet — cover different regions, cultures, families/subtypes, eras, niches,
and communities. Each query is a short real search string (3-8 words). Stay on
{entity}s only. Do NOT repeat these already-run queries:
{done}

Return ONLY a JSON array of {n} strings."""


def _norm(item):
    """Dedup key: lowercase, drop parenthetical qualifiers + leading article."""
    import re
    v = re.sub(r"\([^)]*\)", "", item).strip().strip('"“”\'').rstrip(",;:.")
    v = re.sub(r"^(the|a|an)\s+", "", v, flags=re.I).strip()
    return v.lower()


def _more_queries(goal, entity, counts, n, done, model):
    if not brain.has_llm():
        return []
    sample = ", ".join(list({v[0] for v in counts.values()})[:40]) or entity
    dones = "; ".join(list(done)[-20:])
    try:
        res = brain.ask(_MORE_PROMPT.format(entity=entity, sample=sample, n=n, done=dones),
                        want_json=True, model=model)
        data = brain.extract_json(res["text"])
    except Exception:
        return []
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    return [str(x).strip() for x in data if isinstance(x, str) and x.strip()][:n] if isinstance(data, list) else []


def run(goal, slug, scope="broad", backends=None, workers=6, on_progress=None):
    cfg = SCOPES.get(scope, SCOPES["broad"])
    entity = extract.target_entity(goal)
    names = backends or search.DEFAULT_BACKENDS
    xmodel = brain.extract_model()

    counts = {}                     # norm -> [display, {source urls}]
    seen_urls = set()
    done_q = set()
    records, all_sources, seen_src = [], [], set()

    queries = expand.expand(goal, cfg["q0"])
    for rnd in range(cfg["rounds"]):
        before = len(counts)
        fresh = [q for q in queries if q not in done_q]
        done_q.update(fresh)

        # 1. harvest this round's queries across the resolvers
        round_hits = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for rows in pool.map(lambda q: search.gather(q, backends=names, per_backend=cfg["per"]), fresh):
                round_hits.extend(rows)
        for r in round_hits:
            u = r.get("url", "")
            if u and u not in seen_src:
                seen_src.add(u)
                all_sources.append(r)

        # 2. pick the most on-topic pages, deep-read + spider to new ones
        pseudo = [{"results": round_hits, "digest": {"relevance": 1.0}}]
        ranked = harvest.rank_sources_for_goal(goal, pseudo, cfg["k"], enumerable=True)
        docs = deepread.read_sources(ranked, k=cfg["k"], want_items=True, want_links=True)
        found = harvest.discover_urls(docs, goal, cfg["disc"], seen_urls, enumerable=True)
        if found:
            docs += deepread.read_sources(found, k=cfg["disc"], want_items=True)

        # 3. harvest mentions from EVERY page. DOM <li>/<td> items = high recall
        #    but structurally noisy (country/section headings on list pages).
        #    Prose mentions come from the LLM, which knows what the entity IS ->
        #    treat those as "vetted" so real entities rank above DOM-only chrome.
        for d in docs:
            url = d.get("url", "")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)

            def _add(it, vetted):
                if not _keep(it):
                    return
                key = _norm(it)
                if key and 2 <= len(key) <= 60:
                    entry = counts.setdefault(key, [it.strip(), set(), False])
                    entry[1].add(url)
                    if vetted:
                        entry[2] = True

            for it in d.get("items", []):
                _add(it, False)                                  # DOM: recall
            for it in extract.extract_mentions(entity, d.get("text", ""), model=xmodel):
                _add(it, True)                                   # prose: LLM-vetted

        added = len(counts) - before
        rec = {"query": f"round {rnd + 1}", "n_results": len(round_hits),
               "results": round_hits[:40], "digest": {"relevance": 1.0},
               "round": rnd + 1, "pages_read": len(docs), "new_items": added, "total_items": len(counts)}
        records.append(rec)
        if on_progress:
            on_progress(rnd + 1, cfg["rounds"], rec)

        # 4. saturation: stop once a round barely adds anything new
        if rnd > 0 and added < cfg["sat"]:
            break
        # 5. widen: generate NEW queries from what we've found (drift-guarded)
        if rnd < cfg["rounds"] - 1:
            queries = _more_queries(goal, entity, counts, cfg["qn"], done_q, xmodel) \
                or expand.expand(goal + f" round {rnd + 2}", cfg["qn"])

    return _finalize(goal, slug, scope, entity, names, counts, records, all_sources)


def _finalize(goal, slug, scope, entity, names, counts, records, sources):
    # vetted (LLM confirmed it's really the entity) first, then by source count
    ranked = sorted(counts.values(), key=lambda v: (v[2], len(v[1])), reverse=True)
    items = [{"item": disp, "sources": len(src), "vetted": bool(vet)} for disp, src, vet in ranked]
    n_vetted = sum(1 for it in items if it["vetted"])
    listed = [f"{it['item']}  ({it['sources']}){'' if it['vetted'] else ' ~'}" for it in items]

    corpus = {
        "slug": slug, "goal": goal, "mode": "census", "scope": scope, "entity": entity,
        "backends": names, "engine": brain.engine_info(), "safety": search.status_report(),
        "n_queries": sum(1 for _ in records), "n_rounds": len(records),
        "n_sources": len(sources), "n_deep_read": sum(r.get("pages_read", 0) for r in records),
        "n_discovered": 0, "n_facts": len(items), "n_items": len(items),
        "queries": [r["query"] for r in records], "records": records, "sources": sources,
        "facts": [{"fact": it["item"], "query": f"census x{it['sources']}"} for it in items],
        "items": items,
        "n_vetted": n_vetted,
        "report": {
            "answer": (f"Census of \"{entity}\": {len(items)} distinct mentions across "
                       f"{len(records)} round(s) / {len(sources)} sources (scope={scope}). "
                       f"{n_vetted} LLM-vetted as real {entity}s (listed first); the rest (marked ~) "
                       f"are DOM-only and may include headings/categories. "
                       f"(N) = how many sources mention each, so you can threshold."),
            "key_findings": listed,
            "open_questions": [],
            "confidence": round(min(0.95, 0.4 + len(items) / 2000.0), 2),
            "enumerated": True,
        },
    }
    jf, jj, md, rp = harvest._paths(slug)
    with open(jj, "w", encoding="utf-8") as f:
        json.dump(corpus, f, ensure_ascii=False, indent=2)
    harvest._write_md(md, corpus)
    harvest._write_report(rp, corpus)
    with open(jf, "w", encoding="utf-8") as f:      # jsonl progress = per-round
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return corpus
