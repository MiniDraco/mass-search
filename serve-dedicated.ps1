# Mass Search's own Ollama instance, pinned to the idle GPU (GTX 1660 SUPER = index 0),
# on port 11435 so it never competes with other local LLM work (e.g. Cue on the 3060 @ :11434).
# Leave this window open while a campaign runs. Mass Search auto-detects :11435 first.
$env:CUDA_VISIBLE_DEVICES = "0"          # the idle 1660 SUPER
$env:OLLAMA_HOST = "127.0.0.1:11435"
$env:OLLAMA_KEEP_ALIVE = "30m"           # keep the model warm between queries
Write-Host "Starting dedicated Mass Search Ollama on GPU0 (1660 SUPER) @ 127.0.0.1:11435 ..." -ForegroundColor Cyan
ollama serve
