"""Reset stalled sweep targets so they are tried again.

Clears attempt counts and stale in-flight marks for anything not finished. Use
after a run where targets were abandoned (e.g. handed out while the browser was
still settling). Completed products keep their results and are not re-swept.

    python3 scrapers/requeue.py

The collector reads this file at startup, so restart it afterwards.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "out"
STATE = OUT / "review_sweep_state.json"
TARGETS = OUT / "review_targets.json"


def main() -> None:
    if not STATE.exists():
        print("no sweep state — nothing to requeue")
        return
    state = json.loads(STATE.read_text())
    done = state.get("done", {})
    targets = json.loads(TARGETS.read_text()) if TARGETS.exists() else []

    stalled = [t for t in targets
               if t["url"] not in done
               and (t["url"] in state.get("inflight", {}) or state.get("attempts", {}).get(t["url"]))]

    state["attempts"] = {u: n for u, n in state.get("attempts", {}).items() if u in done}
    state["inflight"] = {}
    state["partial"] = {}
    STATE.write_text(json.dumps(state))

    print(f"requeued {len(stalled)} stalled targets "
          f"({sum(t['expected_reviews'] for t in stalled)} reviews advertised)")
    for t in stalled[:6]:
        print(f"  {t['expected_reviews']:6}  {t['name'][:46]}")
    print(f"kept {len(done)} completed products")


if __name__ == "__main__":
    main()
