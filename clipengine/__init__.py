"""clipengine — find, cut, check, score and post streamer moments."""

# One engine version: doctor, report, the cycle's update line and the public export (Klondike) all show
# this number, so internal and public copies of the same engine code report the same version.
__version__ = "0.6.0"


def version_on_disk(root: str | None = None) -> str:
    """The version of the engine code on disk now (after a pull, the running process still has the old one)."""
    import os
    import re
    root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', open(os.path.join(root, "clipengine", "__init__.py")).read(), re.M)
        return m.group(1) if m else "?"
    except OSError:
        return "?"
