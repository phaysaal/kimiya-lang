"""Syntax highlighter: ANSI for terminals, HTML for documents.

Follows the paper's typography: core keywords blue, the world-effecting
extension magenta, comments gray, strings brown.
"""

from __future__ import annotations

import html
import re

from .lexer import KEYWORDS, WKEYWORDS

ANSI = {
    "kw": "\033[1;34m",      # core keywords: bold blue
    "wkw": "\033[1;35m",     # world extension: bold magenta
    "str": "\033[33m",       # strings: brown/yellow
    "num": "\033[36m",       # numbers: cyan
    "cmt": "\033[90m",       # comments: gray
    "op": "\033[2m",
    "0": "\033[0m",
}
CSS = """
.kim { font-family: ui-monospace, monospace; white-space: pre; }
.kim .kw  { color: #1e3c82; font-weight: 600; }
.kim .wkw { color: #8c1e5a; font-weight: 600; }
.kim .str { color: #783c14; }
.kim .num { color: #0e7490; }
.kim .cmt { color: #6e6e6e; font-style: italic; }
"""

TOKEN_RE = re.compile(
    r'(?P<cmt>--[^\n]*)|(?P<str>"(?:\\.|[^"\\])*")|'
    r'(?P<num>\b\d+(?:\.\d+)?\b)|(?P<word>\b[A-Za-z_][A-Za-z0-9_]*\b)')


def _classify(m: re.Match) -> tuple[str, str]:
    text = m.group(0)
    if m.lastgroup == "cmt":
        return "cmt", text
    if m.lastgroup == "str":
        return "str", text
    if m.lastgroup == "num":
        return "num", text
    if text in WKEYWORDS:
        return "wkw", text
    if text in KEYWORDS:
        return "kw", text
    return "", text


def ansi(source: str) -> str:
    out = []
    pos = 0
    for m in TOKEN_RE.finditer(source):
        out.append(source[pos:m.start()])
        cls, text = _classify(m)
        out.append(f"{ANSI[cls]}{text}{ANSI['0']}" if cls else text)
        pos = m.end()
    out.append(source[pos:])
    return "".join(out)


def html_page(source: str, title: str = "kimiya") -> str:
    out = []
    pos = 0
    for m in TOKEN_RE.finditer(source):
        out.append(html.escape(source[pos:m.start()]))
        cls, text = _classify(m)
        esc = html.escape(text)
        out.append(f'<span class="{cls}">{esc}</span>' if cls else esc)
        pos = m.end()
    out.append(html.escape(source[pos:]))
    body = "".join(out)
    return (f"<!doctype html><meta charset='utf-8'><title>{title}</title>"
            f"<style>{CSS}</style><div class='kim'>{body}</div>")
