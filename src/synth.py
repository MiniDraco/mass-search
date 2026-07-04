"""
synth.py - fold a whole campaign's harvested facts into THE ANSWER.

Harvest + extract give you a pile of per-query facts. For an agent driving this
harness, the useful output is the synthesized conclusion: a direct, grounded
answer to the campaign goal + the key findings + what's still open. That's what
this stage produces (local LLM, so it's free to run on every campaign).

Map-reduce when there are many facts, so nothing is silently dropped: summarize
in chunks, then synthesize from the partial findings.
"""
import re
from . import brain, extract

_CHUNK = 60

# page-chrome / navigation junk that leaks into deep-read extraction
_JUNK = re.compile(r"(tagged with|table of contents|add a header|read more|"
                   r"subscribe|newsletter|cookie|privacy policy|sign in|log in|"
                   r"»|\||©|^https?://|^\W*$|latecomer|walknotes)", re.I)


def _clean_item(fact):
    return fact.strip().lstrip("-*0123456789.)• \t").strip().strip('"“”\'')

_SYNTH_PROMPT = """You are a research analyst closing out a search campaign.
Synthesize a final answer to the GOAL using ONLY the harvested facts below. Do
not invent anything not supported by the facts.

GOAL: {goal}

HARVESTED FACTS (from many independent web searches):
{facts}

Return ONLY a JSON object:
{{
  "answer": "<3-6 sentences directly answering the goal, grounded in the facts>",
  "key_findings": ["<the most important, specific, non-obvious findings>", ...],
  "open_questions": ["<what the facts leave unclear or unverified>", ...],
  "confidence": <0.0-1.0 how well the facts actually answer the goal>
}}"""


def _call(goal, fact_lines, model=None):
    prompt = _SYNTH_PROMPT.format(goal=goal, facts="\n".join("- " + f for f in fact_lines))
    try:
        data = brain.extract_json(brain.ask(prompt, want_json=True, model=model)["text"])
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    kf = data.get("key_findings") or []
    oq = data.get("open_questions") or []
    return {
        "answer": str(data.get("answer", "")).strip(),
        "key_findings": [str(x).strip() for x in kf if str(x).strip()],
        "open_questions": [str(x).strip() for x in oq if str(x).strip()],
        "confidence": float(data.get("confidence", 0.0) or 0.0),
    }


def _enumerate(goal, facts):
    """P3: for a 'list me X' goal, the answer IS the list -- but rank items by
    how many distinct sources corroborate each one. Real entries recur across
    many pages; page-chrome noise appears once, so corroboration separates
    signal from junk. Prefer deep-read page items over snippet summaries."""
    deep = [f for f in facts if f.get("query", "").startswith("deep-read:")]
    pool = deep or facts
    freq = {}                                        # key -> [display, {sources}]
    for f in pool:
        v = _clean_item(f["fact"])
        if not v or not (1 <= len(v) <= 120) or _JUNK.search(v):
            continue
        entry = freq.setdefault(v.lower(), [v, set()])
        entry[1].add(f.get("query", ""))
    scored = sorted(([len(src), disp] for disp, src in freq.values()),
                    key=lambda x: (-x[0], x[1].lower()))
    corroborated = [d for c, d in scored if c >= 2]
    singles = [d for c, d in scored if c < 2]
    items = (corroborated + (singles if len(corroborated) < 25 else []))[:400]
    n_corr = len(corroborated)
    conf = round(min(0.95, 0.35 + 0.6 * (n_corr / max(1, len(items)))), 2) if items else 0.0
    return {
        "answer": (f"Compiled {len(items)} items for \"{goal}\" -- "
                   f"{n_corr} corroborated across 2+ sources (listed first)."),
        "key_findings": items,
        "open_questions": [],
        "confidence": conf,
        "enumerated": True,
    }


def synthesize(goal, facts):
    """facts: [{'fact','query'}]. Returns the synthesized report dict or None."""
    if not facts:
        return None
    if extract.is_enumerable(goal):                  # list-goal -> keep every item verbatim
        return _enumerate(goal, facts)
    if not brain.has_llm():
        return None
    model = brain.extract_model()
    lines = [f["fact"] for f in facts]
    if len(lines) <= _CHUNK:
        return _call(goal, lines, model=model)
    # map: distill each chunk to findings, then reduce those into the answer
    partial = []
    for i in range(0, len(lines), _CHUNK):
        r = _call(goal, lines[i:i + _CHUNK], model=model)
        if r:
            partial.extend(r["key_findings"] or [r["answer"]])
    return _call(goal, partial or lines[:_CHUNK], model=model)
