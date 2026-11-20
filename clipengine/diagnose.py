"""diagnose — match recent logs against docs/known-problems.json and print only what matched + the fix.

Token-light by design: never prints raw logs, one line per matched problem (most frequent first),
and a single line when nothing matched.
"""
from __future__ import annotations

import glob
import json
import os
import re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CATALOG = os.path.join(REPO, "docs", "known-problems.json")


def recent_log_text(max_files: int = 12, tail_bytes: int = 60000) -> str:
    files = sorted(glob.glob(os.path.join(REPO, "logs", "*.log")) + glob.glob(os.path.join(REPO, "runs", "*.log")),
                   key=os.path.getmtime)[-max_files:]
    out = []
    for f in files:
        try:
            with open(f, errors="replace") as fh:
                fh.seek(max(0, os.path.getsize(f) - tail_bytes))
                out.append(fh.read())
        except OSError:
            pass
    return "\n".join(out)


def match(text: str, catalog: dict | None = None) -> list[dict]:
    catalog = catalog or json.load(open(CATALOG))
    hits = []
    for p in catalog["problems"]:
        n = len(re.findall(p["match"], text, flags=re.I))
        if n:
            hits.append({**p, "count": n})
    return sorted(hits, key=lambda h: -h["count"])


def diagnose(text: str | None = None) -> list[dict]:
    if text is None:
        from . import supervise as SV
        for line in SV.status_lines():
            if line.startswith(("leader: NONE", "OPEN incident", "last 7 days")) or "LATE" in line:
                print(line)
    hits = match(recent_log_text() if text is None else text)
    if not hits:
        print("diagnose: no known problem in recent logs (run `clipengine doctor` to check the machine)")
    for h in hits[:5]:
        print(f"✗ {h['id']} (seen {h['count']}×): {h['problem']}\n  fix: {h['fix']}")
    return hits
