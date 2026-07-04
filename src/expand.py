"""
expand.py - turn ONE topic/question into MANY diverse search queries.

With a local LLM: ask it for N distinct angles (history, mechanism, criticism,
data, comparisons, latest developments, edge cases...). This is where "mass"
comes from -- one seed becomes a whole search campaign, all on your hardware.

Without an LLM: a keyless facet-template fallback still fans the seed out.
"""
import re
from . import brain

_EXPAND_PROMPT = """You are a research strategist. Break the SEED below into {n} DISTINCT, high-signal web search queries that together cover it exhaustively.

Cover different angles: definitions/basics, history/origin, how it works, key people/orgs, data/statistics, comparisons/alternatives, criticism/controversy, latest developments, real-world examples, and edge cases -- whichever apply.

Rules:
- Each query is a real search string a person would type (3-9 words), NOT a question sentence.
- No duplicates, no numbering, no quotes.
- Stay tightly on-topic to the seed.

SEED: {seed}

Return ONLY a JSON array of {n} strings."""

# keyless fallback: generic facets appended to the seed
_FACETS = [
    "", "overview", "explained", "history", "how it works", "examples",
    "statistics data", "latest developments", "criticism problems",
    "comparison alternatives", "best practices", "case study",
    "vs", "guide", "research paper", "tutorial",
]


def _clean(q):
    q = re.sub(r'^[\s\d\.\)\-\*"\']+', "", q or "").strip().strip('"\'')
    return re.sub(r"\s+", " ", q)


def expand(seed, n=20):
    """Return up to n distinct search queries derived from seed."""
    seed = seed.strip()
    queries = []
    if brain.has_llm():
        try:
            res = brain.ask(_EXPAND_PROMPT.format(seed=seed, n=n), want_json=True)
            data = brain.extract_json(res["text"])
            if isinstance(data, dict):          # models often wrap: {"queries": [...]}
                for v in data.values():
                    if isinstance(v, list):
                        data = v
                        break
            if isinstance(data, list):
                queries = [_clean(x) for x in data if isinstance(x, str)]
        except Exception:
            queries = []
    if len(queries) < max(3, n // 2):          # LLM missing/short -> facet fallback
        for f in _FACETS:
            queries.append(_clean(f"{seed} {f}") if f else seed)
    # dedupe (case-insensitive), keep order, cap at n
    seen, out = set(), []
    for q in queries:
        k = q.lower()
        if q and k not in seen:
            seen.add(k)
            out.append(q)
        if len(out) >= n:
            break
    return out


if __name__ == "__main__":
    import sys
    seed = " ".join(sys.argv[1:]) or "retopology of AI-generated meshes"
    for i, q in enumerate(expand(seed, 12), 1):
        print(f"{i:2}. {q}")
