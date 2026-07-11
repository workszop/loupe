"""Freeze-frame capture: grab a single full-screen snapshot into a Gdk.Texture.

Uses `cosmic-screenshot` in non-interactive mode, which is silent on COSMIC
(no permission dialog, no notification) and writes a PNG whose path it prints
on stdout. The snapshot is taken once, before the loupe window maps, so the
window can show the frozen desktop and magnify it without any screencast
feedback loop.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

import gi

gi.require_version("Gdk", "4.0")

from gi.repository import Gdk


def grab_screenshot() -> Gdk.Texture:
    """Capture the whole screen and return it as a Gdk.Texture.

    Raises RuntimeError if the screenshot tool fails or produces no file.
    """
    tmpdir = tempfile.mkdtemp(prefix="loupe-shot-")
    try:
        result = subprocess.run(
            [
                "cosmic-screenshot",
                "--interactive=false",
                "--notify=false",
                "-s",
                tmpdir,
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        path = result.stdout.strip()
        if not path or not os.path.exists(path):
            # Some builds may not echo the path; fall back to newest file.
            candidates = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir)]
            candidates = [p for p in candidates if os.path.isfile(p)]
            if not candidates:
                raise RuntimeError(
                    "screenshot failed: " + (result.stderr.strip() or "no output file")
                )
            path = max(candidates, key=os.path.getmtime)

        texture = Gdk.Texture.new_from_filename(path)
        return texture
    finally:
        # Best-effort cleanup of the temp PNG + dir; the texture holds its own
        # decoded copy so the file is no longer needed.
        try:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)
        except OSError:
            pass
