"""
synth.py - fold a whole campaign's harvested facts into THE ANSWER.

Harvest + extract give you a pile of per-query facts. For an agent driving this
harness, the useful output is the synthesized conclusion: a direct, grounded
answer to the campaign goal + the key findings + what's still open. That's what
this stage produces (local LLM, so it's free to run on every campaign).

Map-reduce when there are many facts, so nothing is silently dropped: summarize
in chunks, then synthesize from the partial findings.
"""
from . import brain

_CHUNK = 60

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


def _call(goal, fact_lines):
    prompt = _SYNTH_PROMPT.format(goal=goal, facts="\n".join("- " + f for f in fact_lines))
    try:
        data = brain.extract_json(brain.ask(prompt, want_json=True)["text"])
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


def synthesize(goal, facts):
    """facts: [{'fact','query'}]. Returns the synthesized report dict or None."""
    if not brain.has_llm() or not facts:
        return None
    lines = [f["fact"] for f in facts]
    if len(lines) <= _CHUNK:
        return _call(goal, lines)
    # map: distill each chunk to findings, then reduce those into the answer
    partial = []
    for i in range(0, len(lines), _CHUNK):
        r = _call(goal, lines[i:i + _CHUNK])
        if r:
            partial.extend(r["key_findings"] or [r["answer"]])
    return _call(goal, partial or lines[:_CHUNK])
