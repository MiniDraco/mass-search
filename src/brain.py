"""
brain.py - pluggable AI engine for Mass Search (forked from Riposte).

Local-first: if a local LLM server (Ollama / OpenAI-compatible) is actually
running we use it -- unlimited, on your own hardware, no cloud quota. Gemini is
the opt-in "cheat" only if you set a key; offline is a no-LLM fallback.

Every backend exposes:
    ask(prompt, want_json=False, grounded=False) -> {"text": str, "sources": [...]}

Env vars accept a MASS_ prefix, falling back to the RIPOSTE_ names so a shared
box "just works" either way. Stdlib only -- dependency-light on purpose.

Thread-safe: ask() may be called from many worker threads at once. The local
LLM handles concurrent HTTP fine (Ollama queues internally); Gemini calls are
serialized behind a lock + min-gap so we never trip its rate limit.
"""
import os, re, json, time, threading, urllib.request, urllib.error


def _env(name, default=""):
    """Read MASS_<name>, then RIPOSTE_<name>, then default."""
    return (os.environ.get("MASS_" + name)
            or os.environ.get("RIPOSTE_" + name)
            or default).strip()


# ---- config ---------------------------------------------------------------
GEMINI_KEY   = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = _env("GEMINI_MODEL", "gemini-2.5-flash")
_GEMINI_URL  = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"

LOCAL_URL    = _env("LOCAL_URL")
LOCAL_MODEL  = _env("LOCAL_MODEL")


def _detect_local():
    """Return (base_url, model, flavor) if a local LLM server answers, else None."""
    candidates = []
    if LOCAL_URL:
        candidates.append(LOCAL_URL.rstrip("/"))
    # 11435 = dedicated Ollama on the idle GPU (see serve-dedicated.ps1);
    # 11434 = the shared Ollama; 1234 = LM Studio.
    candidates += ["http://localhost:11435", "http://localhost:11434", "http://localhost:1234"]
    for base in candidates:
        try:  # Ollama native
            with urllib.request.urlopen(base + "/api/tags", timeout=2) as r:
                tags = json.loads(r.read().decode())
                models = [m["name"] for m in tags.get("models", [])]
                if models:
                    return (base, LOCAL_MODEL or models[0], "ollama")
        except Exception:
            pass
        try:  # OpenAI-compatible (LM Studio, llama-server, vLLM)
            with urllib.request.urlopen(base + "/v1/models", timeout=2) as r:
                data = json.loads(r.read().decode())
                ids = [m["id"] for m in data.get("data", [])]
                if ids:
                    return (base, LOCAL_MODEL or ids[0], "openai")
        except Exception:
            pass
    return None


_forced = _env("BACKEND")
_LOCAL = None
if _forced == "local" or (not _forced):
    _LOCAL = _detect_local()

if _forced in ("gemini", "local", "offline"):
    BACKEND = _forced
    if _forced == "local" and not _LOCAL:
        _LOCAL = ("http://localhost:11434", LOCAL_MODEL or "llama3.2", "ollama")
elif _LOCAL:
    BACKEND = "local"            # local-first: a running local model wins
elif GEMINI_KEY:
    BACKEND = "gemini"           # the cloud "cheat", only if a key is present
else:
    BACKEND = "offline"          # no LLM: keep raw evidence, you read it


# Gemini gets polite serialized pacing; local runs free (it's your hardware).
_MIN_GAP = float(_env("MIN_GAP", "1.1"))
_last_call = [0.0]
_gem_lock = threading.Lock()


def backend_name():
    return BACKEND


def has_llm():
    return BACKEND in ("gemini", "local")


# ---- JSON helpers ---------------------------------------------------------
def extract_json(text):
    """Pull the first JSON object/array out of a model reply (handles ```json fences)."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    body = m.group(1) if m else text
    for candidate in (body, text):
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except Exception:
            pass
        for opener, closer in (("[", "]"), ("{", "}")):
            i, j = candidate.find(opener), candidate.rfind(closer)
            if 0 <= i < j:
                try:
                    return json.loads(candidate[i:j + 1])
                except Exception:
                    continue
    return None


# ---- Gemini ---------------------------------------------------------------
def _gemini(prompt, grounded, want_json, retries=3):
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
    }
    if grounded:
        body["tools"] = [{"google_search": {}}]
    elif want_json:
        body["generationConfig"]["responseMimeType"] = "application/json"

    url = _GEMINI_URL.format(model=GEMINI_MODEL, key=GEMINI_KEY)
    data = json.dumps(body).encode("utf-8")

    for attempt in range(retries):
        with _gem_lock:                       # serialize + pace Gemini calls
            wait = _MIN_GAP - (time.time() - _last_call[0])
            if wait > 0:
                time.sleep(wait)
            _last_call[0] = time.time()
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "ignore")[:300]
            if e.code in (429, 500, 503) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise RuntimeError(f"Gemini HTTP {e.code}: {detail}")
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            raise RuntimeError(f"Gemini network error: {e}")

    cand = (payload.get("candidates") or [{}])[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()

    sources, seen = [], set()
    meta = cand.get("groundingMetadata") or {}
    for chunk in meta.get("groundingChunks") or []:
        web = chunk.get("web") or {}
        if web.get("uri") and web["uri"] not in seen:
            seen.add(web["uri"])
            sources.append({"title": web.get("title") or web["uri"], "url": web["uri"]})
    return {"text": text, "sources": sources}


# ---- offline (no LLM) -----------------------------------------------------
def _offline(prompt, grounded, want_json):
    if want_json:
        return {"text": "[]", "sources": []}
    return {"text": "", "sources": []}


# ---- local LLM (Ollama / OpenAI-compatible) -------------------------------
def _local(prompt, want_json):
    base, model, flavor = _LOCAL
    if flavor == "ollama":
        url = base + "/api/chat"
        body = {"model": model, "stream": False,
                "messages": [{"role": "user", "content": prompt}],
                "options": {"temperature": 0.2}}
        if want_json:
            body["format"] = "json"
    else:  # openai-compatible
        url = base + "/v1/chat/completions"
        body = {"model": model, "temperature": 0.2,
                "messages": [{"role": "user", "content": prompt}]}
        if want_json:
            body["response_format"] = {"type": "json_object"}
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as e:
        raise RuntimeError(f"Local LLM error ({url}): {e}")
    if flavor == "ollama":
        text = (payload.get("message") or {}).get("content", "")
    else:
        text = (payload["choices"][0]["message"]["content"]
                if payload.get("choices") else "")
    return {"text": text.strip(), "sources": []}


def ask(prompt, want_json=False, grounded=False):
    if BACKEND == "gemini":
        return _gemini(prompt, grounded, want_json)
    if BACKEND == "local":
        return _local(prompt, want_json)
    return _offline(prompt, grounded, want_json)


def engine_info():
    if BACKEND == "gemini":
        return f"gemini ({GEMINI_MODEL})"
    if BACKEND == "local":
        return f"local ({_LOCAL[2]}:{_LOCAL[1]} @ {_LOCAL[0]})"
    return "offline / no-LLM (raw evidence only)"


if __name__ == "__main__":
    print("backend:", engine_info())
    if has_llm():
        print(ask("In one sentence, what is the capital of France?")["text"][:200])
