"""Load the immutable baseline policy and resolve the baseline plus personal style.

Baseline files ship with the package and are never edited in place. Personal style
layers on top: resolved style = baseline plus optional overrides (build-plan §7.1–7.2).
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
    """The baseline plus any personal writing overrides."""
    default = baseline_style()
    p = config.personal_style_path()
    if p.exists() and p.read_text().strip():
        return default.rstrip() + "\n\n## Personal overrides\n\n" + p.read_text().strip() + "\n"
    return default
