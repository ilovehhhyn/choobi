"""Load the immutable baseline (style, rules, policy) and resolve the personal style.

Baseline files ship with the package and are never edited in place. Personal style
layers on top: resolved style = personal override, else baseline (build-plan §7.1–7.2).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

import yaml

from . import config

_BASELINE_DIR = Path(__file__).resolve().parent.parent / "baseline"


def _read(name: str) -> str:
    return (_BASELINE_DIR / name).read_text()


@lru_cache(maxsize=1)
def policy() -> Dict[str, Any]:
    return yaml.safe_load(_read("policy.yaml"))


@lru_cache(maxsize=1)
def rules() -> Dict[str, Any]:
    return yaml.safe_load(_read("rules.yaml"))


def baseline_style() -> str:
    return _read("style.md")


def resolved_style() -> str:
    """Personal style guide if present, else the shipped baseline."""
    p = config.personal_style_path()
    if p.exists() and p.read_text().strip():
        return p.read_text()
    return baseline_style()
