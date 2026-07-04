"""
extract.py - local LLM distills a query's raw search hits into structured data.

Given the snippets we harvested for one query, the model pulls out the concrete
facts / findings relevant to the overall goal. This is the "understand what we
grabbed" layer -- runs on your hardware, so you can afford to run it on every
query in the campaign.

No LLM? extract() is skipped; the raw hits are still written to file.
"""
from . import brain

_EXTRACT_PROMPT = """You are a research analyst. Below are web search results for one query in a larger research campaign.

CAMPAIGN GOAL: {goal}
THIS QUERY: {query}

SEARCH RESULTS:
{evidence}

From ONLY what these results actually say, extract what's relevant to the campaign goal.
Return ONLY a JSON object:
{{
  "summary": "<2-4 sentence synthesis of what these results establish>",
  "facts": ["<concrete, specific fact or data point stated in the results>", ...],
  "relevance": <0.0-1.0 how relevant this query's results are to the goal>
}}
If the results are empty or off-topic, return summary "", facts [], relevance 0.0."""


def _fmt(results, cap=10):
    lines = []
    for i, r in enumerate(results[:cap], 1):
        snip = (r.get("snippet") or "").strip().replace("\n", " ")
        lines.append(f"[{i}] {r.get('title','')}\n    {r.get('url','')}\n    {snip[:500]}")
    return "\n".join(lines) if lines else "(no results)"


def extract(goal, query, results):
    """Return {'summary','facts','relevance'} distilled from results, or None."""
    if not brain.has_llm() or not results:
        return None
    try:
        res = brain.ask(_EXTRACT_PROMPT.format(
            goal=goal, query=query, evidence=_fmt(results)), want_json=True)
        data = brain.extract_json(res["text"])
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    facts = data.get("facts") or []
    if not isinstance(facts, list):
        facts = [str(facts)]
    return {
        "summary": str(data.get("summary", "")).strip(),
        "facts": [str(f).strip() for f in facts if str(f).strip()],
        "relevance": float(data.get("relevance", 0.0) or 0.0),
    }
