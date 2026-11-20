#!/usr/bin/env python3
"""Render a fixed 3-second synthetic clip and compare its frames with the golden PNGs."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LAYOUTS = ("stack", "facecam", "single")
OUTPUT = ROOT / "tests" / "golden"


def render_frame(folder: Path, layout: str, house: bool = False):
    from PIL import Image
    from clipengine import brand
    from clipengine import render

    folder.mkdir(parents=True, exist_ok=True)
    source = folder / "sample.mp4"
    if not source.exists():
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=1920x1080:rate=30:duration=3", "-f", "lavfi", "-i",
                        "sine=frequency=440:sample_rate=48000:duration=3", "-shortest", "-c:v", "libx264",
                        "-preset", "ultrafast", "-c:a", "aac", str(source)], check=True)
    filename = folder / f"{layout}.mp4"
    box = [0.04, 0.06, 0.28, 0.29] if layout == "facecam" else None
    words = [{"w": " look", "start": 0.8, "end": 1.4}, {"w": " now", "start": 1.4, "end": 2.1}]
    with patch.object(brand, "load", return_value=brand.load(fresh=True) if house else brand.DEFAULTS), \
         patch.object(render, "render_preset", return_value="ultrafast"):
        render.render(str(source), 0, 0, 3, words, str(filename), layout=layout, facecam=box,
                      title="They landed JUST IN TIME", quote="look now", watermark=None)
    still = folder / f"{layout}.png"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1.5", "-i", str(filename), "-frames:v", "1",
                    "-vf", "scale=270:480:flags=area", "-threads", "1", str(still)], check=True)
    with Image.open(still) as image:
        return image.convert("RGB")


def compare_frame(got, reference: Path) -> None:
    import numpy as np
    from PIL import Image

    with Image.open(reference) as image:
        expected = np.asarray(image.convert("RGB"), dtype=np.int16)
    pixels = np.asarray(got, dtype=np.int16)
    assert pixels.shape == expected.shape, f"{reference.name}: dimensions changed"
    difference = np.abs(pixels - expected)
    share = float((difference.max(axis=-1) > 24).mean())
    assert difference.mean() <= 3 and share <= 0.035, (
        f"{reference.name}: mean delta {difference.mean():.2f}; {100 * share:.1f}% visibly different")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--update", action="store_true", required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="clip-golden-") as temp:
        folder = Path(temp)
        cases = [(layout, False, OUTPUT / f"{layout}.png") for layout in LAYOUTS]
        if (ROOT / "tests" / "test_private.py").exists():
            cases.append(("facecam", True, ROOT / "tests" / "golden-private" / "facecam.png"))
        for layout, house, target in cases:
            frame = render_frame(folder / ("private" if house else "neutral"), layout, house)
            target.parent.mkdir(parents=True, exist_ok=True)
            frame.save(target, optimize=True)
            print(os.path.relpath(target, ROOT))
    if args.update:
        print("A human must look at every updated reference before approving it.")


if __name__ == "__main__":
    main()
