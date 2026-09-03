"""Log review-sweep progress periodically so an overnight run can be reviewed later.

    python3 scrapers/watch_sweep.py [interval_seconds] [collector_pid]

Writes logs/sweep.log. Flags a stall when no new reviews land between samples,
which is what a crashed tab or a logged-out session looks like from outside.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "out"
LOG = ROOT / "logs" / "sweep.log"
STATE = OUT / "review_sweep_state.json"
REVIEWS = OUT / "extension_reviews.jsonl"
LIST_STATE = OUT / "listing_state.json"
PRODUCTS = OUT / "extension_products.jsonl"


def snapshot() -> dict:
    done = reviews_state = 0
    inflight = 0
    if STATE.exists():
        try:
            s = json.loads(STATE.read_text())
            done = len(s.get("done", {}))
            inflight = len(s.get("inflight", {}))
            reviews_state = sum(s.get("done", {}).values()) + sum(s.get("partial", {}).values())
        except (json.JSONDecodeError, OSError):
            pass
    lines = 0
    if REVIEWS.exists():
        with REVIEWS.open("rb") as f:
            lines = sum(1 for _ in f)
    prows = 0
    if PRODUCTS.exists():
        with PRODUCTS.open("rb") as f:
            prows = sum(1 for _ in f)
    ldone = 0
    if LIST_STATE.exists():
        try:
            ldone = len(json.loads(LIST_STATE.read_text()).get("done", {}))
        except (json.JSONDecodeError, OSError):
            pass
    return {"done": done, "inflight": inflight, "counted": reviews_state,
            "rows": lines, "prows": prows, "ldone": ldone}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return pid > 0 and isinstance(pid, int) and False


def main() -> None:
    interval = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    pid = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    LOG.parent.mkdir(parents=True, exist_ok=True)

    prev = snapshot()
    start = time.time()
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"\n=== sweep watch started {datetime.now():%Y-%m-%d %H:%M:%S} "
                f"(every {interval}s) ===\n")
        f.flush()
        stalls = 0
        while True:
            time.sleep(interval)
            if pid and not alive(pid):
                f.write(f"{datetime.now():%H:%M:%S}  collector (pid {pid}) exited — stopping watch\n")
                f.flush()
                return
            cur = snapshot()
            gained = (cur["rows"] - prev["rows"]) + (cur["prows"] - prev["prows"])
            mins = (time.time() - start) / 60
            rate = cur["rows"] / mins if mins > 0 else 0
            if gained == 0:
                stalls += 1
                note = f"  STALLED x{stalls}"
            else:
                stalls = 0
                note = ""
            f.write(f"{datetime.now():%H:%M:%S}  listing {cur['ldone']:3}  "
                    f"prod-rows {cur['prows']:6}  sweep {cur['done']:3}  "
                    f"reviews {cur['rows']:6}  (+{gained}){note}\n")
            f.flush()
            prev = cur


if __name__ == "__main__":
    main()
