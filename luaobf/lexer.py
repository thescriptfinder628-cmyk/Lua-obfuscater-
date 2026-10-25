"""A complete lexer for Lua 5.1 – 5.4 source code.

It understands short and long comments, long strings, every escape sequence
(including ``\\z`` and ``\\u{XXXX}``), decimal/hex integers and floats, the
Lua 5.3 bitwise operators and ``::labels::``.
"""

from __future__ import annotations

from dataclasses import dataclass

KEYWORDS = frozenset(
    """
    and break do else elseif end false for function goto if in
    local nil not or repeat return then true until while
    """.split()
)

# Longest operators first so that greedy matching works.
OPERATORS = (
    "...",
    "..",
    "==",
    "~=",
    "<=",
    ">=",
    "//",
    "::",
    "<<",
    ">>",
    "+",
    "-",
    "*",
    "/",
    "%",
    "^",
    "#",
    "&",
    "~",
    "|",
    "<",
    ">",
    "=",
    "(",
    ")",
    "{",
    "}",
    "[",
    "]",
    ";",
    ":",
    ",",
    ".",
)

#: Operators that only exist in Luau: compound assignment and the function-type
#: arrow.  They are only lexed when the dialect is ``luau``, so that strict Lua
#: keeps rejecting ``x += 1`` and ``(a) -> b``.
LUAU_OPERATORS = ("//=", "..=", "+=", "-=", "*=", "/=", "%=", "^=", "->", "?")

# Token kinds
NAME = "name"
KEYWORD = "keyword"
NUMBER = "number"
STRING = "string"
OP = "op"
EOF = "eof"


class LuaSyntaxError(Exception):
    """Raised for lexical or syntactic errors in Lua input."""

    def __init__(self, message: str, line: int = 0, col: int = 0, filename: str | None = None):
        self.message = message
        self.line = line
        self.col = col
        self.filename = filename
        where = f"{filename}:{line}:{col}" if filename else f"{line}:{col}"
        super().__init__(f"{where}: {message}")


@dataclass
class Token:
    kind: str
    value: object
    raw: str
    line: int
    col: int

    def __repr__(self) -> str:  # pragma: no cover
        return f"Token({self.kind}, {self.value!r})"

    def is_op(self, *ops: str) -> bool:
        return self.kind == OP and self.value in ops

    def is_kw(self, *kws: str) -> bool:
        return self.kind == KEYWORD and self.value in kws


_ESCAPES = {
    "a": 7,
    "b": 8,
    "f": 12,
    "n": 10,
    "r": 13,
    "t": 9,
    "v": 11,
    "\\": 92,
    '"': 34,
    "'": 39,
}


def _is_digit(c: str) -> bool:
    return "0" <= c <= "9"


def _is_hex(c: str) -> bool:
    return _is_digit(c) or ("a" <= c <= "f") or ("A" <= c <= "F")


class Lexer:
    def __init__(self, source: str, filename: str | None = None, dialect: str = "lua") -> None:
        # Normalise line endings the way the reference implementation does.
        self.src = source.replace("\r\n", "\n").replace("\r", "\n")
        self.n = len(self.src)
        self.i = 0
        self.line = 1
        self.col = 1
        self.filename = filename
        self.dialect = dialect
        self.operators = (LUAU_OPERATORS + OPERATORS) if dialect == "luau" else OPERATORS
        self.tokens: list[Token] = []
        #: comments encountered, used only by the "keep comments" debug mode
        self.comments: list[tuple[int, str]] = []

    # -- low level helpers -------------------------------------------------

    def peek(self, off: int = 0) -> str:
        j = self.i + off
        return self.src[j] if j < self.n else ""

    def advance(self, count: int = 1) -> str:
        out = self.src[self.i : self.i + count]
        for ch in out:
            if ch == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1
        self.i += count
        return out

    def error(self, message: str, line: int | None = None, col: int | None = None):
        raise LuaSyntaxError(
            message,
            line if line is not None else self.line,
            col if col is not None else self.col,
            self.filename,
        )

    # -- main entry point --------------------------------------------------

    def tokenize(self) -> list[Token]:
        while True:
            self._skip_space_and_comments()
            if self.i >= self.n:
                self.tokens.append(Token(EOF, "", "", self.line, self.col))
                return self.tokens
            self.tokens.append(self._next_token())

    def _skip_space_and_comments(self) -> None:
        while self.i < self.n:
            ch = self.peek()
            if ch in " \t\n\v\f":
                self.advance()
                continue
            if ch == "-" and self.peek(1) == "-":
                start_line = self.line
                self.advance(2)
                # long comment?
                level = self._long_bracket_level()
                if level is not None:
                    text = self._read_long_bracket(level, is_comment=True)
                    if not text.startswith("\n"):
                        pass
                else:
                    while self.i < self.n and self.peek() != "\n":
                        self.advance()
                self.comments.append((start_line, ""))
                continue
            break

    def _long_bracket_level(self) -> int | None:
        """If the source at the cursor opens a long bracket, return its level."""
        if self.peek() != "[":
            return None
        j = self.i + 1
        level = 0
        while j < self.n and self.src[j] == "=":
            level += 1
            j += 1
        if j < self.n and self.src[j] == "[":
            return level
        return None

    def _read_long_bracket(self, level: int, is_comment: bool = False) -> str:
        # consumes the opening bracket and returns the raw content (bytes-safe
        # via latin-1 style handling is not needed: we keep the source text).
        self.advance(level + 2)
        close = "]" + "=" * level + "]"
        # A newline immediately after the opening bracket is skipped.
        if self.peek() == "\n":
            self.advance()
        start = self.i
        while self.i < self.n:
            if self.src.startswith(close, self.i):
                content = self.src[start : self.i]
                self.advance(len(close))
                return content
            self.advance()
        self.error("unfinished long string/comment (starting at line %d)" % self.line)

    # -- individual tokens -------------------------------------------------

    def _next_token(self) -> Token:
        line, col = self.line, self.col
        ch = self.peek()

        if ch.isalpha() or ch == "_":
            return self._read_name(line, col)
        if _is_digit(ch) or (ch == "." and _is_digit(self.peek(1))):
            return self._read_number(line, col)
        if ch in "'\"":
            return self._read_short_string(line, col)
        if ch == "[":
            level = self._long_bracket_level()
            if level is not None:
                content = self._read_long_bracket(level)
                return Token(STRING, content.encode("utf-8"), None, line, col)

        for op in self.operators:
            if self.src.startswith(op, self.i):
                self.advance(len(op))
                return Token(OP, op, op, line, col)

        self.error(f"unexpected symbol near {ch!r}")

    def _read_name(self, line: int, col: int) -> Token:
        start = self.i
        while self.i < self.n and (self.peek().isalnum() or self.peek() == "_"):
            self.advance()
        text = self.src[start : self.i]
        kind = KEYWORD if text in KEYWORDS else NAME
        return Token(kind, text, text, line, col)

    def _read_number(self, line: int, col: int) -> Token:
        start = self.i
        is_hex = False
        if self.peek() == "0" and self.peek(1) in "xX":
            is_hex = True
            self.advance(2)
            while self.i < self.n and (_is_hex(self.peek()) or self.peek() == "."):
                self.advance()
            if self.peek() in "pP":
                self.advance()
                if self.peek() in "+-":
                    self.advance()
                while self.i < self.n and _is_digit(self.peek()):
                    self.advance()
        else:
            while self.i < self.n and (_is_digit(self.peek()) or self.peek() == "."):
                # don't swallow ".." (concat) or a method-dot
                if self.peek() == "." and self.peek(1) == ".":
                    break
                self.advance()
            if self.peek() in "eE":
                self.advance()
                if self.peek() in "+-":
                    self.advance()
                while self.i < self.n and _is_digit(self.peek()):
                    self.advance()

        raw = self.src[start : self.i]
        # Reject things like "3abc"
        if self.i < self.n and (self.peek().isalnum() or self.peek() == "_"):
            self.error(f"malformed number near '{raw}{self.peek()}'", line, col)

        try:
            value, is_int = _parse_number(raw, is_hex)
        except ValueError:
            self.error(f"malformed number near '{raw}'", line, col)
        return Token(NUMBER, (value, is_int), raw, line, col)

    def _read_short_string(self, line: int, col: int) -> Token:
        quote = self.peek()
        start = self.i
        self.advance()
        out = bytearray()
        while True:
            if self.i >= self.n:
                self.error("unfinished string", line, col)
            ch = self.peek()
            if ch == quote:
                self.advance()
                break
            if ch == "\n":
                self.error("unfinished string (newline in string)", line, col)
            if ch == "\\":
                self.advance()
                out.extend(self._read_escape(line, col))
                continue
            out.extend(ch.encode("utf-8"))
            self.advance()
        raw = self.src[start : self.i]
        return Token(STRING, bytes(out), raw, line, col)

    def _read_escape(self, line: int, col: int) -> bytes:
        ch = self.peek()
        if ch == "":
            self.error("unfinished string", line, col)
        if ch in _ESCAPES:
            self.advance()
            return bytes([_ESCAPES[ch]])
        if ch == "\n":
            self.advance()
            return b"\n"
        if ch == "z":
            self.advance()
            while self.i < self.n and self.peek() in " \t\n\v\f":
                self.advance()
            return b""
        if ch == "x":
            self.advance()
            digits = self.advance(2)
            if len(digits) != 2 or not all(_is_hex(c) for c in digits):
                self.error("hexadecimal digit expected in \\x escape")
            return bytes([int(digits, 16)])
        if ch == "u":
            self.advance()
            if self.peek() != "{":
                self.error("missing '{' in \\u{XXXX} escape")
            self.advance()
            start = self.i
            while self.i < self.n and _is_hex(self.peek()):
                self.advance()
            digits = self.src[start : self.i]
            if not digits or self.peek() != "}":
                self.error("malformed \\u{XXXX} escape")
            self.advance()
            cp = int(digits, 16)
            if cp > 0x7FFFFFFF:
                self.error("UTF-8 value too large in \\u escape")
            return chr(cp).encode("utf-8")
        if _is_digit(ch):
            digits = ""
            while len(digits) < 3 and _is_digit(self.peek()):
                digits += self.advance()
            value = int(digits, 10)
            if value > 255:
                self.error("decimal escape too large")
            return bytes([value])
        self.error(f"invalid escape sequence '\\{ch}'")


def _parse_number(raw: str, is_hex: bool):
    """Return ``(value, is_int)`` for a Lua numeric literal."""
    if is_hex:
        body = raw[2:]
        if "." in body or "p" in body or "P" in body:
            # Python's float.fromhex wants a "0x" prefix and an optional "p" part
            if "p" not in body and "P" not in body:
                body += "p0"
            return float.fromhex("0x" + body), False
        return int(body, 16), True
    if "." in raw or "e" in raw or "E" in raw:
        return float(raw), False
    value = int(raw, 10)
    if value > 0x7FFFFFFFFFFFFFFF:
        # Lua 5.4 wraps oversized integer literals into floats (and 5.1 too)
        return float(raw), False
    return value, True


def tokenize(source: str, filename: str | None = None, dialect: str = "lua") -> list[Token]:
    """Convenience wrapper: ``tokenize(src) -> [Token, ...]``."""
    return Lexer(source, filename, dialect).tokenize()


def strip_comments(source: str, filename: str | None = None) -> str:
    """Remove comments and trailing whitespace while leaving code untouched.

    This is a text-level helper; the obfuscator normally works on the AST.  It
    exists for the ``--strip-only`` CLI mode and for tests.
    """
    src = source
    out = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        if ch in "'\"":
            quote = ch
            j = i + 1
            while j < n:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == quote:
                    j += 1
                    break
                j += 1
            out.append(src[i:j])
            i = j
            continue
        if ch == "[":
            level = 0
            k = i + 1
            while k < n and src[k] == "=":
                level += 1
                k += 1
            if k < n and src[k] == "[":
                close = "]" + "=" * level + "]"
                end = src.find(close, k + 1)
                end = n if end < 0 else end + len(close)
                out.append(src[i:end])
                i = end
                continue
        if ch == "-" and src.startswith("--", i):
            # comment: long or short
            k = i + 2
            level = 0
            while k < n and src[k] == "=":
                level += 1
                k += 1
            if k < n and src[k] == "[":
                close = "]" + "=" * level + "]"
                end = src.find(close, k + 1)
                i = n if end < 0 else end + len(close)
            else:
                end = src.find("\n", i)
                i = n if end < 0 else end
            continue
        out.append(ch)
        i += 1
    return "".join(out)
