#!/usr/bin/env python3
"""
build_mcpb.py - assemble the .mcpb bundle for Claude Desktop.

Source of truth stays in src/. This copies the current engine into
mcpb/server/masssearch/, then zips mcpb/ into dist/mass-search.mcpb
(a .mcpb IS just a zip with manifest.json at the root).

Run:  python build_mcpb.py
Then in Claude Desktop: Settings -> Extensions -> install dist/mass-search.mcpb
"""
import os, shutil, zipfile, json

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
BUNDLE = os.path.join(ROOT, "mcpb")
PKG = os.path.join(BUNDLE, "server", "masssearch")
DIST = os.path.join(ROOT, "dist")

ENGINE_FILES = ["__init__.py", "brain.py", "search.py", "expand.py",
                "extract.py", "synth.py", "harvest.py"]


def sync_engine():
    """Copy src/*.py into the bundle as the `masssearch` package."""
    if os.path.isdir(PKG):
        shutil.rmtree(PKG)
    os.makedirs(PKG)
    for name in ENGINE_FILES:
        shutil.copy2(os.path.join(SRC, name), os.path.join(PKG, name))
    print(f"synced {len(ENGINE_FILES)} engine files -> {os.path.relpath(PKG, ROOT)}")


def pack():
    with open(os.path.join(BUNDLE, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, "mass-search.mcpb")
    if os.path.exists(out):
        os.remove(out)
    n = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for base, _dirs, files in os.walk(BUNDLE):
            for fn in files:
                if fn.endswith((".pyc",)) or "__pycache__" in base:
                    continue
                full = os.path.join(base, fn)
                arc = os.path.relpath(full, BUNDLE)   # manifest.json at zip root
                z.write(full, arc)
                n += 1
    size = os.path.getsize(out)
    print(f"packed {n} files -> {os.path.relpath(out, ROOT)}  ({size/1024:.0f} KB)")
    print(f"bundle: {manifest['name']} v{manifest['version']}")
    return out


if __name__ == "__main__":
    sync_engine()
    pack()
