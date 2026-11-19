"""Is this the shared operation repo, or one channel's own copy of the public engine?

Our operation syncs through git: several machines pull drops and push the ledger, views and
weights. A public copy (marked by the `.public-build` file the export adds) is one channel on one
machine: it never commits or pushes anything, and only ever pulls engine releases through
`clipengine update`, which tests a release before keeping it.
"""
from __future__ import annotations

import os
import subprocess

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKER = os.path.join(REPO, ".public-build")
NO_PUSH = "no-push--this-copy-only-pulls-engine-updates"


def public() -> bool:
    return os.path.exists(MARKER)


def sync() -> bool:
    """True only in the shared operation repo: commit and push drops, ledger, views, weights."""
    return not public() and os.environ.get("CLIPENGINE_NO_SYNC") != "1"


def push_locked() -> bool:
    r = subprocess.run(["git", "-C", REPO, "config", "--get", "remote.origin.pushurl"], capture_output=True, text=True)
    return r.stdout.strip() == NO_PUSH


def lock_push() -> bool:
    """Public copies can pull engine releases but never push to the engine's repository."""
    if not public():
        return False
    if not push_locked():
        subprocess.run(["git", "-C", REPO, "config", "remote.origin.pushurl", NO_PUSH], capture_output=True)
    return push_locked()
