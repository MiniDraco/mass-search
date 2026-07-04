#!/usr/bin/env python3
"""
worker.py - one-shot Python worker the Node MCP transport spawns per tool call.

Reads {"tool": name, "arguments": {...}} as JSON on stdin, runs the tool, and
prints a single JSON envelope on stdout: {"ok": true, "text": ...} or
{"ok": false, "error": ...}. Engine print()s are redirected to stderr so the
only thing on stdout is that one envelope line.
"""
import sys, os, json, contextlib

# Windows consoles default to cp1252; results contain non-Latin chars. Force UTF-8
# so writing the envelope (or engine chatter on stderr) can never UnicodeError.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tools_impl   # noqa: E402


def main():
    raw = sys.stdin.read()
    try:
        req = json.loads(raw or "{}")
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"bad request json: {e}"}))
        return
    fn = tools_impl.DISPATCH.get(req.get("tool"))
    if not fn:
        print(json.dumps({"ok": False, "error": f"unknown tool: {req.get('tool')}"}))
        return
    try:
        with contextlib.redirect_stdout(sys.stderr):   # keep engine chatter off stdout
            text = fn(req.get("arguments") or {})
        sys.stdout.write(json.dumps({"ok": True, "text": text}, ensure_ascii=True) + "\n")
    except Exception as e:
        sys.stdout.write(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=True) + "\n")


if __name__ == "__main__":
    main()
