"""Kimiya lexer: line-oriented, indentation-aware (spaces only).

Token kinds: NAME KEYWORD WKEYWORD NUMBER STRING OP NEWLINE INDENT DEDENT EOF
Comments run from `--` to end of line. The keyword split mirrors the paper's
typography: core keywords vs the world-effecting extension (magenta).
"""

from __future__ import annotations

from dataclasses import dataclass

KEYWORDS = {
    "pool", "context", "schema", "effect", "domain", "preserve", "allow_loss",
    "param", "memo", "explore",
    "gen", "select", "judge", "check", "retry", "until", "budget", "panel",
    "paraphrase_prompts", "under", "by", "if", "then", "else", "forall", "in",
    "commit", "abstain", "print", "true", "false", "null", "and", "or", "not",
    "contradicts", "irreversible", "recoverable",
    "fn", "return", "use", "pyfn", "python", "agent",
}
WKEYWORDS = {"act", "observe", "settle", "within", "inv", "compensate", "display"}

OPS = [":=", "|=", "==", "!=", "<=", ">=", "~", "<", ">", "(", ")", "[", "]",
       "{", "}", ",", ".", ":", "+", "-", "*", "/", "="]


@dataclass
class Token:
    kind: str
    value: str
    line: int
    col: int

    def __repr__(self):
        return f"{self.kind}({self.value!r}@{self.line})"


class LexError(SyntaxError):
    pass


def lex(source: str) -> list[Token]:
    tokens: list[Token] = []
    indents = [0]
    lines = source.split("\n")
    for ln, raw in enumerate(lines, 1):
        # strip comments (respecting strings)
        line = _strip_comment(raw)
        if not line.strip():
            continue
        if "\t" in line[: len(line) - len(line.lstrip())]:
            raise LexError(f"line {ln}: tabs are not allowed in indentation")
        indent = len(line) - len(line.lstrip(" "))
        if indent > indents[-1]:
            indents.append(indent)
            tokens.append(Token("INDENT", "", ln, 0))
        while indent < indents[-1]:
            indents.pop()
            tokens.append(Token("DEDENT", "", ln, 0))
        if indent != indents[-1]:
            raise LexError(f"line {ln}: inconsistent dedent")
        _lex_line(line.strip(), ln, indent, tokens)
        tokens.append(Token("NEWLINE", "", ln, len(line)))
    while len(indents) > 1:
        indents.pop()
        tokens.append(Token("DEDENT", "", len(lines), 0))
    tokens.append(Token("EOF", "", len(lines) + 1, 0))
    return tokens


def _strip_comment(line: str) -> str:
    in_str = False
    i = 0
    while i < len(line):
        c = line[i]
        if c == '"':
            in_str = not in_str
        elif not in_str and c == "-" and line[i:i + 2] == "--":
            return line[:i]
        i += 1
    return line


def _lex_line(text: str, ln: int, base_col: int, out: list[Token]):
    i = 0
    while i < len(text):
        c = text[i]
        col = base_col + i + 1
        if c == " ":
            i += 1
            continue
        if c == '"':
            j = i + 1
            buf = []
            while j < len(text):
                if text[j] == "\\" and j + 1 < len(text):
                    buf.append({"n": "\n", "t": "\t", '"': '"',
                                "\\": "\\"}.get(text[j + 1], text[j + 1]))
                    j += 2
                    continue
                if text[j] == '"':
                    break
                buf.append(text[j])
                j += 1
            if j >= len(text):
                raise LexError(f"line {ln}: unterminated string")
            out.append(Token("STRING", "".join(buf), ln, col))
            i = j + 1
            continue
        if c.isdigit() or (c == "." and i + 1 < len(text)
                           and text[i + 1].isdigit()):
            j = i
            while j < len(text) and (text[j].isdigit() or text[j] == "."):
                j += 1
            out.append(Token("NUMBER", text[i:j], ln, col))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < len(text) and (text[j].isalnum() or text[j] == "_"):
                j += 1
            word = text[i:j]
            if word in WKEYWORDS:
                out.append(Token("WKEYWORD", word, ln, col))
            elif word in KEYWORDS:
                out.append(Token("KEYWORD", word, ln, col))
            else:
                out.append(Token("NAME", word, ln, col))
            i = j
            continue
        for op in OPS:
            if text.startswith(op, i):
                out.append(Token("OP", op, ln, col))
                i += len(op)
                break
        else:
            raise LexError(f"line {ln}: unexpected character {c!r}")
