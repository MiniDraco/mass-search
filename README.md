# Mass Search

A **keyless, local-hardware mass web-search engine** forked from
[Riposte](../riposte)'s fact-check search bot.

Riposte aimed a keyless-search + local-LLM engine at one video's claims, one at
a time. **Mass Search** re-aims that same engine at *any* topic or query list —
fanning one seed into many searches, running them **in parallel** across
keyless backends, distilling each result set with your **local LLM**, and
**streaming everything to a file**.

No API key. No per-call quota. Your hardware is the ceiling, not somebody's
usage cap — that's how you win the search game.

```
topic ──► EXPAND (local LLM: 1 seed -> N diverse queries)
            │
            ▼
      HARVEST (thread pool over many free resolvers — see the roster below)
            │   ← per-host lock + gap + circuit breaker keep every site safe
            ▼
      EXTRACT (local LLM pulls the facts/answer out of each result set)
            │
            ▼
      DEEP-READ (fetch the top ON-TOPIC sources' FULL pages via the same
            │      polite/ban-safe plumbing; list goals parse the DOM structure
            │      directly (<li>/<td> entries), prose goals distill the body)
            │   +  DISCOVER: follow the seeds' best on-topic links to reach pages
            │      NO resolver returned (link-following, same ban-safe fetch path)
            ▼
      SYNTHESIZE (local LLM folds all facts -> THE ANSWER; "list me X" goals get
            │       the verbatim items back, not a summary)
            │
            ▼
      WRITE  → out/<slug>.jsonl      (streamed live, resumable)
               out/<slug>.json       (consolidated, deduped corpus)
               out/<slug>.md          (all results)
               out/<slug>.report.md   (the answer + top sources)
```

## Quick start

```bash
# 1. (optional) start a dedicated local LLM on the idle GPU
./serve-dedicated.ps1            # Ollama @ 127.0.0.1:11435

# 2. spin up a campaign
python mass_search.py "field-aligned quad retopology" --queries 30 --workers 8
```

Runs fine with **no LLM** too (falls back to a facet-template query expansion
and just writes the raw search corpus — you read the receipts).

### Built to be driven by an agent (Claude)

This is a research **harness**: point it at a question, and the final
**synthesized ANSWER** (+ key findings, open questions, top source URLs) is
printed compactly to **stdout** — so an agent running it via the shell gets the
*conclusion* back directly, not a wall of raw results. The full corpus stays on
disk (`out/<slug>.*`) for drill-down. It's all local + keyless, so an agent can
run as many campaigns as it wants without burning a cloud quota.

## Install as a Claude Desktop extension (`.mcpb`)

Mass Search ships as a one-click **MCP Bundle** (`.mcpb` — the format formerly
called `.dxt`). Claude Desktop bundles **Node** (not Python), so the transport is
a tiny dependency-free Node server (`server/index.js`) that spawns the stdlib
Python engine per tool call. That keeps the Install button enabled (a Python-type
bundle greys it out — see [mcpb#84](https://github.com/modelcontextprotocol/mcpb/issues/84)).
You just need **Python 3.9+** on your PATH for the engine to run.

```powershell
python build_mcpb.py        # syncs src/ into the bundle + writes dist/mass-search.mcpb
```

Then in **Claude Desktop → Settings → Extensions**, install
`dist/mass-search.mcpb`. Set your output folder, Ollama URL/model, and
(optionally) a local SearXNG URL + contact email in the extension's settings.

It exposes three tools to Claude:
- **`web_search`** — fast keyless multi-resolver hit list (quick lookups)
- **`mass_search`** — kicks off a full campaign **in the background** (it's minutes of
  local-LLM work, longer than the MCP call timeout) and returns a slug immediately
- **`read_campaign`** — fetch a campaign's synthesized answer by slug once it's done
  (reports live progress if it's still running)

Rebuild anytime with `python build_mcpb.py` (or `npx @anthropic-ai/mcpb pack mcpb
dist/mass-search.mcpb` for the validated CLI path). Bundle source lives in
`mcpb/` (`manifest.json` + `server/main.py`).

## Ways to run it

```bash
# topic -> local LLM expands into N queries -> harvest + distill
python mass_search.py "webgpu llm inference" --queries 30 --workers 8

# preview the queries without searching
python mass_search.py "webgpu llm inference" --dry-run

# your own query list, one per line, run verbatim
python mass_search.py --file queries.txt --slug mytopic

# literal queries on the CLI, no LLM expansion
python mass_search.py --raw "instant meshes flow field" "quad remesh blender"

# pick a resolver GROUP (web | academic | tech | all), skip distill for raw speed
python mass_search.py "rust async runtime" --backends tech --no-extract

# everything, across all domains
python mass_search.py "protein folding diffusion models" --backends all --queries 40
```

Rerun the **same `--slug`** to **resume** — queries already in the `.jsonl` are
skipped, so a big campaign is stop/start-safe.

## Resolver roster (all free)

Pass a **group** or a comma-list to `--backends`. Ban-prone general-web hosts get
the biggest gaps + first-block trip; structured APIs *welcome* traffic.

| group | resolvers | notes |
|---|---|---|
| `web` | `ddg`, `marginalia`, `mojeek`*, `wiki`, `searxng`* | general web; DDG is primary, Marginalia is independent failover |
| `academic` | `crossref`, `openalex`, `semanticscholar`, `arxiv`, `wiki` | keyless scholarly; Crossref + OpenAlex are the reliable workhorses |
| `tech` | `hn`, `stackexchange`, `github` | dev / code / Q&A |
| `all` | every available resolver | |

`*` = **off unless you set a free key/URL** (see env below). Everything else is
100% keyless. Resolvers that get rate-limited mid-run are auto-dropped, not
retried into a ban.

## Flags

| flag | default | meaning |
|------|---------|---------|
| `--queries N` | 24 | how many queries to expand a topic into |
| `--workers N` | 6 | parallel search workers |
| `--backends`  | `web` | group (`web`/`academic`/`tech`/`all`) or comma-list of resolvers |
| `--per-backend N` | 6 | results kept per backend per query |
| `--no-extract` | off | skip the local-LLM per-query distillation step |
| `--no-synth` | off | skip the final synthesis (the printed answer) |
| `--goal "..."` | =seed | steer what the LLM extracts + synthesizes |
| `--file PATH` | — | queries from a file (one per line, verbatim) |
| `--raw` | off | positional args are literal queries (no expansion) |
| `--dry-run` | off | print the expanded queries and exit |

## Engine selection (from Riposte, local-first)

Auto-picks **local > gemini > offline**:

- **local (default, no key):** Ollama / LM Studio / any OpenAI-compatible server.
  Tries `:11435` (dedicated) → `:11434` (shared) → `:1234` (LM Studio).
- **gemini (opt-in cheat):** set `GEMINI_API_KEY` to use the free tier instead.
- **offline (no LLM):** facet-template expansion + raw corpus, no distillation.

Env (both `MASS_` and `RIPOSTE_` prefixes work):

| var | default | meaning |
|-----|---------|---------|
| `MASS_BACKEND` | auto | force `local` / `gemini` / `offline` |
| `MASS_LOCAL_MODEL` | first found | e.g. `hermes3:8b` (query-expansion + snippet distill) |
| `MASS_EXTRACT_MODEL` | =local model | heavier model for deep-read + synthesis (e.g. a 14B–32B on the idle GPU) |
| `MASS_LOCAL_URL` | auto | override the local endpoint |
| `MASS_DEEPREAD_K` | `8` | how many top sources to read full-body per campaign |
| `MASS_DEEPREAD_CHARS` | `12000` | chars of page text fed to the distiller |
| `MASS_DISCOVER_N` | `6` | extra pages to reach by following the seeds' on-topic links |
| `MASS_SEARCH_GAP` | `1.5` | default min seconds between hits to one host (per-host overrides in code) |
| `MASS_HOST_CAP` | `300` | hard cap on requests to any one host per run |
| `MASS_CACHE_TTL` | 604800 | search cache lifetime, seconds (7 days) |
| `MASS_CONTACT` | — | email put in the User-Agent + polite-pool `mailto` (Wikipedia/Crossref/OpenAlex ask for it) |
| `MASS_SEARXNG_URL` | — | your local SearXNG instance → enables the `searxng` resolver |
| `MASS_MOJEEK_KEY` | — | free Mojeek key (2k/mo) → enables the `mojeek` resolver |
| `MASS_OPENALEX_KEY` | — | optional OpenAlex key (works keyless via `mailto` too) |
| `GEMINI_API_KEY` | — | enables the Gemini path |

## Ban-safe by design (no do-overs on one IP)

Bans are permanent, so safety is structural, not hopeful:

- **Per-host lock held across the request** — never two hits to one host at once,
  no matter how many workers. Parallelism overlaps *across* hosts + local-LLM work.
- **Per-host gap + jitter** — each host has its own documented-safe pace (DuckDuckGo
  3s, Wikipedia/Crossref 1s, HN 0.5s, GitHub 6s…), not one global number.
- **Circuit breaker** — a host that returns a block signal (202 / 403 / 429 / 503)
  is **dropped for the rest of the run**. Ban-prone hosts trip on the first block;
  friendly hosts get a gap-doubling soft backoff first. The run reports what tripped.
- **Per-run request cap** per host, so a bug can't run away.
- **Empty/blocked responses are never cached**, so a blip can't poison the 7-day cache.

Want more general-web throughput without more ban risk? Stand up a **local
SearXNG** and point `MASS_SEARXNG_URL` at it — it spreads each query across dozens
of upstream engines that *you* rate-limit and own.

## Local SearXNG (the general-web firehose) — installed

A native-Windows SearXNG (no Docker, no hypervisor) is set up at
`D:\AI\searxng`. One query fans across brave + duckduckgo + startpage + … so the
ban risk is spread across many upstreams instead of hammering DuckDuckGo.

```powershell
# 1. start it (leave the window open)
D:\AI\searxng\start-searxng.ps1          # -> http://127.0.0.1:8888, JSON API on

# 2. point Mass Search at it and use the web/all groups
$env:MASS_SEARXNG_URL = "http://127.0.0.1:8888"
python mass_search.py "your topic" --backends web --queries 30 --workers 8
```

Config lives in `D:\AI\searxng\my-settings.yml` (JSON format enabled, limiter off
for local use). A `pwd.py` shim in that folder covers a Unix-only import so it runs
on Windows. Add more upstream engines by editing `my-settings.yml`.

## Where it's strong / where it's weak

Field-tested over multi-campaign runs (hundreds of requests, zero bans):

- **Strong:** discovery + ban-safety. It finds the authoritative pages with no
  search cap and never gets the IP blocked — the circuit breaker routes around
  every soft block.
- **Deep-read + extractive mode** turn "list me X" goals into the actual verbatim
  list. Three things make the list clean and complete:
  1. **Structure-aware parsing** — list goals read the page *DOM* (`<li>`/`<td>`
     entries) rather than flattening to text, so the full verbatim list comes
     straight from the HTML (no LLM retyping loss — and no per-page LLM call).
  2. **Goal-aware ranking** keeps off-topic pages out of the deep-read entirely.
  3. **Cross-source corroboration** ranks items by how many sources repeat them,
     so recurring real entries surface and one-off page-chrome drops.
  Example: *"list of overused AI words"* → a clean ~60-item list, every entry
  corroborated across 2+ sources (delve, leverage, synergy, "paradigm shift",
  "it is important to note"…), zero blog-title/heading junk — pulled from the
  actual authoritative listicles.
- **Confidence is computed**, not self-graded — from cross-source corroboration
  (list goals) or mean relevance × deep-read coverage × fact volume (prose goals).
- **Reaching beyond the resolvers:** keyless search engines are a shallow well
  (Brave killed its free tier, most others are anti-bot or self-host). So extra
  coverage comes from **link-following** — parsing the top pages and following
  their most on-topic links to reach sources no search backend returned (a
  `instant-meshes-vs-zbrush` comparison, a SIGGRAPH paper, etc.). Yield is high
  for research/reference goals that cross-link, lower for thin listicles that
  don't. For raw general breadth, run a **local SearXNG** and set
  `MASS_SEARXNG_URL` — it fans each query across ~70 upstream engines.
- **Remaining lever:** the default reasoner is an 8B. Point `MASS_EXTRACT_MODEL`
  at a 14B–32B (e.g. on the idle-GPU Ollama) for even cleaner extraction.

**Roadmap:** auto-select the resolver group by topic · promote local SearXNG to
the primary general-web index · parse discovered PDFs (academic sources).

## Output shape

`out/<slug>.json`:
```jsonc
{
  "slug": "...", "goal": "...", "engine": "local (ollama:hermes3:8b ...)",
  "n_queries": 30, "n_sources": 214, "n_facts": 143,
  "records": [ { "query": "...", "n_results": 9,
                 "digest": { "summary": "...", "facts": ["..."], "relevance": 0.8 },
                 "results": [ { "title","url","snippet","source" } ] } ],
  "sources": [ /* every unique {title,url,snippet,source} across the run */ ],
  "facts":   [ { "fact": "...", "query": "..." } ],
  "report":  { "answer": "...", "key_findings": ["..."],
               "open_questions": ["..."], "confidence": 0.8 },
  "safety":  { "requests": {...}, "blocks": {...}, "tripped": [...] }
}
```
