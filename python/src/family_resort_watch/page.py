"""Render the artifact page for one trip from the shipped page template.

The page reads all its data from the artifact's db, so only the few strings
that must be in the HTML before any data arrives (the <title> the gallery
shows, the heading, the recess label) are filled in here.
"""

from __future__ import annotations

import html
from pathlib import Path

from . import config



def build(trip: dict) -> Path:
    out = config.data_file("page.html").read_text()
    for key in ("title", "recess_label", "stay_label"):
        out = out.replace("{{" + key.upper() + "}}", html.escape(trip[key]))
    if "{{" in out:
        raise ValueError("the page template has a placeholder build() doesn't fill")
    config.PAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.PAGE_PATH.write_text(out)
    return config.PAGE_PATH
