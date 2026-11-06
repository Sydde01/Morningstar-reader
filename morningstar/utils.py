from __future__ import annotations

import os
import re
import subprocess

_num = re.compile(r"(\d+)")
_prefix = re.compile(r"^\d{1,3}\s*[_.\-)\]]+\s*")


def natural_key(s: str):
    return [int(t) if t.isdigit() else t.casefold() for t in _num.split(s)]


def strip_prefix(name: str) -> str:
    out = _prefix.sub("", name, count=1).strip()
    return out or name


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def show_in_explorer(path: str) -> None:
    path = os.path.normpath(path)
    if os.name == "nt":
        if os.path.isdir(path):
            subprocess.Popen(["explorer", path])
        else:
            subprocess.Popen(["explorer", "/select,", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path)])
