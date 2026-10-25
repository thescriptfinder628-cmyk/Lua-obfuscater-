"""Turn the AST back into Lua source, either pretty-printed or minified."""

from __future__ import annotations

import math

from . import ast_nodes as A
from .lexer import KEYWORDS

# Output precedence (higher binds tighter).
PREC: dict[str, int] = {
    "or": 1,
    "and": 2,
    "<": 3,
    ">": 3,
    "<=": 3,
    ">=": 3,
    "~=": 3,
    "==": 3,
    "|": 4,
    "~": 5,
    "&": 6,
    "<<": 7,
    ">>": 7,
    "..": 9,
    "+": 10,
    "-": 10,
    "*": 11,
    "/": 11,
    "//": 11,
    "%": 11,
    "^": 14,
}
RIGHT_ASSOC = {"..", "^"}
UNARY_PREC = 12
PRIMARY = 15

_MULTI_OPS = ("...", "..", "==", "~=", "<=", ">=", "//", "::", "<<", ">>", "--", "[[")


def _is_ident_start(c: str) -> bool:
    return c.isalpha() or c == "_"


def _is_ident_char(c: str) -> bool:
    return c.isalnum() or c == "_"


def is_identifier(text: str) -> bool:
    return bool(text) and _is_ident_start(text[0]) and all(_is_ident_char(c) for c in text) and text not in KEYWORDS


def needs_space(prev: str, nxt: str) -> bool:
    """Would gluing ``prev`` and ``nxt`` together change how they lex?"""
    if not prev or not nxt:
        return False
    a, b = prev[-1], nxt[0]
    if _is_ident_char(a) and _is_ident_char(b):
        # "and" .. "not"  /  "1" .. "x"  /  "x" .. "1"
        return True
    if a.isdigit() and b == ".":
        return True
    if (a + b) in _MULTI_OPS:
        return True
    if a == "[" and b in "[=":
        return True
    if a == "-" and b == "-":
        return True
    return False


def cat(parts: list[str]) -> str:
    out = ""
    for part in parts:
        if not part:
            continue
        if out and needs_space(out, part):
            out += " "
        out += part
    return out


def format_number(node: A.Number, use_raw: bool = True) -> str:
    value = node.value
    if node.is_int:
        return str(value)
    if use_raw and node.raw is not None and math.isfinite(value):
        return node.raw
    if value != value:  # NaN
        return "(0/0)"
    if value == math.inf:
        return "1e999"
    if value == -math.inf:
        return "-1e999"
    text = repr(value)
    if "e" not in text and "E" not in text and "." not in text:
        text += ".0"
    return text


def quote_string(data: bytes) -> str:
    """Encode bytes as a single-line Lua string literal."""
    out = ['"']
    for byte in data:
        ch = chr(byte)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif 32 <= byte < 127:
            out.append(ch)
        else:
            out.append("\\%d" % byte)
    out.append('"')
    return "".join(out)


class Unparser:
    def __init__(self, minify: bool = True, indent: str = "  ", use_raw: bool = True) -> None:
        self.minify = minify
        self.indent_unit = indent
        self.level = 0
        self.use_raw = use_raw

    # -- helpers -----------------------------------------------------------

    def ind(self) -> str:
        return "" if self.minify else self.indent_unit * self.level

    def newline(self) -> str:
        return "\n"

    def block(self, blk: A.Block) -> str:
        self.level += 1
        lines = [self.ind() + self.stat(s) for s in blk.stats]
        self.level -= 1
        return self.newline().join(line for line in lines if line != "")

    def body(self, blk: A.Block) -> str:
        """Body text placed between keywords (``then`` … ``end``)."""
        inner = self.block(blk)
        if not inner:
            return ""
        if self.minify:
            return " " + inner + " "
        return "\n" + inner + "\n" + self.ind()

    # -- expressions -------------------------------------------------------

    def expr(self, node: A.Expr, min_prec: int = 0) -> str:
        text, prec = self._expr(node)
        if prec < min_prec:
            return "(" + text + ")"
        return text

    def _expr(self, node: A.Expr) -> tuple[str, int]:
        if isinstance(node, A.Name):
            return node.out, PRIMARY
        if isinstance(node, A.Nil):
            return "nil", PRIMARY
        if isinstance(node, A.TrueExpr):
            return "true", PRIMARY
        if isinstance(node, A.FalseExpr):
            return "false", PRIMARY
        if isinstance(node, A.Vararg):
            return "...", PRIMARY
        if isinstance(node, A.Number):
            return format_number(node, self.use_raw), PRIMARY
        if isinstance(node, A.String):
            if node.raw is not None and self.use_raw:
                return node.raw, PRIMARY
            return quote_string(node.value), PRIMARY
        if isinstance(node, A.Paren):
            return "(" + self.expr(node.expr) + ")", PRIMARY
        if isinstance(node, A.Function):
            return self.function_expr(node), 1
        if isinstance(node, A.Table):
            return self.table(node), PRIMARY
        if isinstance(node, A.Index):
            obj = self.suffixed(node.obj)
            key = node.key
            if isinstance(key, A.String) and is_identifier(key.text()) and (key.raw is not None or self.use_raw):
                return obj + "." + key.text(), PRIMARY
            return obj + "[" + self.expr(key) + "]", PRIMARY
        if isinstance(node, A.Call):
            return self.suffixed(node.func) + self.call_args(node.args), PRIMARY
        if isinstance(node, A.MethodCall):
            return (
                self.suffixed(node.obj) + ":" + node.method + self.call_args(node.args),
                PRIMARY,
            )
        if isinstance(node, A.BinOp):
            prec = PREC[node.op]
            left = self.expr(node.left, prec)
            right = self.expr(node.right, prec if node.op in RIGHT_ASSOC else prec + 1)
            return cat([left, node.op, right]), prec
        if isinstance(node, A.UnOp):
            operand = self.expr(node.operand, UNARY_PREC)
            op = node.op
            if op == "not":
                return cat(["not", operand]), UNARY_PREC
            return cat([op, operand]), UNARY_PREC
        raise TypeError(f"cannot unparse {type(node).__name__}")

    def suffixed(self, node: A.Expr) -> str:
        """Render ``node`` so it can carry ``[`` / ``.`` / ``(`` / ``:`` suffixes."""
        if isinstance(node, (A.Name, A.Index, A.Call, A.MethodCall, A.Paren)):
            return self.expr(node)
        # A bare number, string, table or function literal is not a prefixexp.
        return "(" + self.expr(node) + ")"

    def index_tail(self, key: A.Expr) -> str:
        if isinstance(key, A.String) and key.raw is not None and self.use_raw and is_identifier(key.text()):
            return "." + key.text()
        if isinstance(key, A.String) and key.raw is None and is_identifier(key.text()):
            return "." + key.text()
        return "[" + self.expr(key) + "]"

    def call_args(self, args: list[A.Expr]) -> str:
        if len(args) == 1 and isinstance(args[0], A.String):
            # f"str" form -- compact and valid
            return self.expr(args[0])
        if len(args) == 1 and isinstance(args[0], A.Table):
            return self.table(args[0])
        inner = ",".join(self.expr(a) for a in args)
        return "(" + inner + ")"

    def table(self, node: A.Table) -> str:
        parts = []
        for field in node.fields:
            if field.key is None:
                parts.append(self.expr(field.value))
            else:
                key = field.key
                if isinstance(key, A.String) and is_identifier(key.text()):
                    parts.append(cat([key.text(), "=", self.expr(field.value)]))
                else:
                    parts.append(cat(["[", self.expr(key), "]=", self.expr(field.value)]))
        return "{" + ",".join(parts) + "}"

    def function_expr(self, node: A.Function) -> str:
        params = self.params(node)
        sig = "function(" + params + ")"
        return sig + self.body(node.body) + "end"

    def function_decl(self, prefix: str, node: A.Function) -> str:
        """``prefix`` is e.g. ``"function t:m"`` or ``"local function f"``."""
        params = self.params(node)
        if node.is_method and node.params and node.params[0].name == "self":
            # skip the implicit self both in pretty and minified output
            params = ",".join(p.out for p in node.params[1:])
            if node.is_vararg:
                params = params + ",..." if params else "..."
        return cat([prefix + "(" + params + ")", self.body(node.body)]) + "end"

    def params(self, node: A.Function) -> str:
        parts = [p.out for p in node.params]
        if node.is_vararg:
            parts.append("...")
        return ",".join(parts)

    # -- statements --------------------------------------------------------

    def stat(self, node: A.Stat) -> str:
        if isinstance(node, A.LocalDecl):
            names = []
            for name, new, attr in zip(node.names, node.new_names, node.attribs):
                piece = new or name
                if attr:
                    piece = cat([piece, "<" + attr + ">"])
                names.append(piece)
            text = "local " + ",".join(names)
            if node.exprs:
                text = cat([text, "=", ",".join(self.expr(e) for e in node.exprs)])
            return text
        if isinstance(node, A.LocalFunction):
            name = node.new_name or node.name
            return self.function_decl("local function " + name, node.func)
        if isinstance(node, A.Assign):
            targets = ",".join(self.expr(t) for t in node.targets)
            return cat([targets, "=", ",".join(self.expr(e) for e in node.exprs)])
        if isinstance(node, A.CallStat):
            return self.expr(node.call)
        if isinstance(node, A.Do):
            return "do" + self.body(node.body) + "end"
        if isinstance(node, A.While):
            return cat(["while", self.expr(node.cond), "do"]) + self.body(node.body) + "end"
        if isinstance(node, A.Repeat):
            return "repeat" + self.body(node.body) + cat(["until", self.expr(node.cond)])
        if isinstance(node, A.If):
            out = []
            for i, (cond, blk) in enumerate(node.clauses):
                kw = "if" if i == 0 else "elseif"
                out.append(cat([kw, self.expr(cond), "then"]) + self.body(blk))
            if node.orelse is not None:
                out.append("else" + self.body(node.orelse))
            out.append("end")
            return "".join(out)
        if isinstance(node, A.NumFor):
            var = node.new_name or node.var
            head = cat(
                [
                    "for " + var + "=",
                    self.expr(node.start),
                    ",",
                    self.expr(node.stop),
                ]
            )
            if node.step is not None:
                head = cat([head, ",", self.expr(node.step)])
            head = cat([head, "do"])
            return head + self.body(node.body) + "end"
        if isinstance(node, A.GenFor):
            names = ",".join(
                new or old for old, new in zip(node.vars, node.new_names)
            )
            head = cat(["for " + names + " in", ",".join(self.expr(e) for e in node.exprs), "do"])
            return head + self.body(node.body) + "end"
        if isinstance(node, A.FuncStat):
            path = ".".join(node.path)
            if node.method:
                path = path + ":" + node.method
            return self.function_decl("function " + path, node.func)
        if isinstance(node, A.Return):
            if not node.exprs:
                return "return"
            return cat(["return", ",".join(self.expr(e) for e in node.exprs)])
        if isinstance(node, A.Break):
            return "break"
        if isinstance(node, A.Goto):
            return "goto " + node.label
        if isinstance(node, A.Label):
            return "::" + node.name + "::"
        if isinstance(node, A.Empty):
            return ""
        raise TypeError(f"cannot unparse statement {type(node).__name__}")

    def chunk(self, node: A.Chunk) -> str:
        self.level = 0
        text = self.block(node.body)
        if not self.minify:
            text += "\n"
        return text


def unparse(node: A.Node, minify: bool = True, use_raw: bool = True) -> str:
    """Render a :class:`Chunk` (or statement/expression) as Lua source."""
    up = Unparser(minify=minify, use_raw=use_raw)
    if isinstance(node, A.Chunk):
        return up.chunk(node)
    if isinstance(node, A.Block):
        return up.block(node)
    if isinstance(node, A.Stat):
        return up.stat(node)
    return up.expr(node)


def minify_source(source: str) -> str:
    """Parse then re-emit without comments/whitespace (no renaming)."""
    from .parser import parse

    return unparse(parse(source), minify=True)
