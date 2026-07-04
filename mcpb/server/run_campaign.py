#!/usr/bin/env python3
"""
run_campaign.py - detached background runner for a mass_search campaign.

tool_mass_search spawns this with the job params as argv[1] (JSON). It runs the
full expand -> harvest -> distill -> synthesize pipeline (writing out/<slug>.*)
and drops a <slug>.done marker when finished, which read_campaign watches for.
"""
import sys, os, json

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tools_impl   # noqa: E402


def main():
    params = json.loads(sys.argv[1])
    slug = tools_impl._slug(params["question"])
    base = os.path.join(tools_impl.harvest.OUT, slug)
    try:
        tools_impl.run_campaign(params)
        print("campaign complete:", slug)
    except Exception as e:
        print("campaign FAILED:", type(e).__name__, e)
    finally:
        try:
            open(base + ".done", "w", encoding="utf-8").close()
        except OSError:
            pass


if __name__ == "__main__":
    main()
