"""Load the immutable baseline policy and resolve the editable personal style.

Baseline files ship with the package and are never edited in place. A personal style file is a
complete editable copy; when it is absent, the bundled baseline is the resolved style.
"""
from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import Any, Dict

import yaml

from . import config

def _read(name: str) -> str:
    return resources.files("choobi").joinpath("baseline", name).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def policy() -> Dict[str, Any]:
    return yaml.safe_load(_read("policy.yaml"))


def baseline_style() -> str:
    return _read("style.md")


def resolved_style() -> str:
    """The complete personal style document, or the bundled baseline by default."""
    default = baseline_style()
    p = config.personal_style_path()
    if p.exists() and p.read_text().strip():
        return p.read_text()
    return default
