"""AGENT.md / SKILL.md frontmatter parser.

A file starts with `---`, contains YAML, ends with `---`, then free-form body.
Returns (frontmatter_dict, body_str). Missing or malformed frontmatter → ({}, raw).
"""
from __future__ import annotations

import yaml


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    lines = text.split("\n")
    # First line is '---'; find closing '---'
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, text
    fm_text = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(fm, dict):
        return {}, text
    return fm, body
