"""Render the report page: one self-contained HTML file with the data embedded as JSON."""
from __future__ import annotations

import json
from pathlib import Path

TEMPLATE = Path(__file__).with_name("template.html")
SPLIT = "<!--BODY-->"
DATA_SLOT = "/*__DATA__*/"


def _embed(data: dict) -> str:
    """JSON that is safe inside a <script> element (no '</script>' or '<!--' can appear)."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def render(data: dict, fragment: bool = False) -> str:
    """Full HTML document, or with fragment=True just head-content + body-content."""
    head, body = TEMPLATE.read_text(encoding="utf-8").split(SPLIT)
    if DATA_SLOT not in body:
        raise ValueError("template is missing its data slot")
    body = body.replace(DATA_SLOT, _embed(data))
    if fragment:
        return head + body
    return (
        '<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        f"{head}</head>\n<body>\n{body}</body>\n</html>\n"
    )
