"""Copy selected live appearance from the calling Kitty window."""

import json
import math
import os
from pathlib import Path
import re
import subprocess

COLOR_NAME = re.compile(r"^(?:foreground|background|cursor|color(?:[0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])|[a-z][a-z0-9_]*_(?:foreground|background|color))$")
COLOR_VALUE = re.compile(r"^#[0-9a-fA-F]{6}$")


def capture():
    window_id = os.environ.get("KITTY_WINDOW_ID", "")
    if not re.fullmatch(r"[1-9][0-9]*", window_id):
        raise ValueError("--inherit-appearance requires running inside a Kitty window")
    kitten = Path(__file__).with_name("appearance_kitten.py")
    try:
        result = subprocess.run(["kitty", "@", "kitten", "--match", "id:" + window_id, str(kitten)],
                                capture_output=True, text=True, timeout=15, check=True)
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ValueError("cannot read source Kitty appearance; check remote-control permissions") from exc
    if not isinstance(data, dict) or not isinstance(data.get("colors"), str):
        raise ValueError("invalid source Kitty appearance")
    size = data.get("font_size")
    if isinstance(size, bool) or not isinstance(size, (int, float)) or not math.isfinite(size) or not 0 < size <= 1000:
        raise ValueError("invalid source Kitty font size")
    options = ["-o", f"font_size={size}"]
    seen = set()
    for line in data["colors"].splitlines():
        fields = line.split()
        if len(fields) != 2 or not COLOR_NAME.fullmatch(fields[0]) or not COLOR_VALUE.fullmatch(fields[1]) or fields[0] in seen:
            raise ValueError("invalid source Kitty color data")
        seen.add(fields[0])
        options.extend(["-o", f"{fields[0]}={fields[1]}"])
    if not {"foreground", "background", "mark1_foreground"}.issubset(seen):
        raise ValueError("incomplete source Kitty color data")
    if "marker" not in data:
        raise ValueError("source Kitty did not report marker state")
    marker = data["marker"]
    if marker is not None:
        # Kitty does not retain regex flags in current_marker_spec. Only function
        # markers can be reconstructed without silently changing their meaning.
        if (not isinstance(marker, list) or len(marker) != 2 or marker[0] != "function" or
                not isinstance(marker[1], str) or not marker[1] or "\x00" in marker[1] or
                len(marker[1]) > 4096):
            raise ValueError("cannot safely inherit this Kitty marker; only function markers are supported")
    return {"options": options, "marker": marker}
