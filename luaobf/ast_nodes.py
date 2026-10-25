"""AST node definitions for the Lua obfuscator.

The tree is deliberately simple: plain classes with a ``fields`` tuple so that
generic passes (renaming, walking, rewriting) can operate without reflection
tricks.
"""

from __future__ import annotations


class Node:
    """Base class for every AST node."""

    _fields: tuple[str, ...] = ()

    #: line/column of the construct in the source file (for error messages)
    line: int = 0
    col: int = 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        parts = []
        for f in self._fields:
            parts.append(f"{f}={getattr(self, f)!r}")
        return f"{type(self).__name__}({', '.join(parts)})"

    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return False
        return all(getattr(self, f) == getattr(other, f) for f in self.fields)


# --------------------------------------------------------------------------
# Expressions
# --------------------------------------------------------------------------


class Expr(Node):
    pass


class Nil(Expr):
    def __init__(self) -> None:
        pass


class TrueExpr(Expr):
    def __init__(self) -> None:
        pass


class FalseExpr(Expr):
    def __init__(self) -> None:
        pass


class Vararg(Expr):
    def __init__(self) -> None:
        pass


class Number(Expr):
    """A numeric literal.

    ``value`` keeps the exact Lua value (int for integers, float for floats) so
    the encoder can rebuild it.  ``raw`` preserves the original spelling when
    no transformation is requested.
    """

    _fields = ("value", "raw", "is_int")

    def __init__(self, value, raw: str, is_int: bool) -> None:
        self.value = value
        self.raw = raw
        self.is_int = is_int


class String(Expr):
    """A string literal.  ``value`` holds the raw bytes of the Lua string."""

    _fields = ("value", "raw")

    def __init__(self, value: bytes, raw: str | None = None) -> None:
        self.value = value
        self.raw = raw

    def text(self) -> str:
        return self.value.decode("utf-8", "replace")


class Name(Expr):
    """A variable reference.  ``new_name`` is filled in by the renamer."""

    _fields = ("id",)

    def __init__(self, ident: str) -> None:
        self.id = ident
        self.new_name: str | None = None
        #: set by the resolver: 'local', 'upvalue', 'global' or 'param'
        self.scope_kind: str | None = None
        #: the LocalDecl / Param node this reference resolves to (if any)
        self.binding = None

    @property
    def out(self) -> str:
        return self.new_name or self.id


class BinOp(Expr):
    _fields = ("op", "left", "right")

    def __init__(self, op: str, left: Expr, right: Expr) -> None:
        self.op = op
        self.left = left
        self.right = right


class UnOp(Expr):
    _fields = ("op", "operand")

    def __init__(self, op: str, operand: Expr) -> None:
        self.op = op
        self.operand = operand


class Index(Expr):
    """``obj[key]`` — also used for ``obj.name`` (key is a String)."""

    _fields = ("obj", "key")

    def __init__(self, obj: Expr, key: Expr) -> None:
        self.obj = obj
        self.key = key


class Call(Expr):
    """``func(args...)``"""

    _fields = ("func", "args")

    def __init__(self, func: Expr, args: list[Expr]) -> None:
        self.func = func
        self.args = args


class MethodCall(Expr):
    """``obj:method(args...)``  (``method`` stays a plain identifier)"""

    _fields = ("obj", "method", "args")

    def __init__(self, obj: Expr, method: str, args: list[Expr]) -> None:
        self.obj = obj
        self.method = method
        self.args = args


class TableField(Node):
    """One entry of a table constructor.

    ``key`` is ``None`` for positional entries and ``value`` is ``None`` for
    ``[x] = nil``-style holes (not normally written by hand).
    """

    _fields = ("key", "value")

    def __init__(self, key: Expr | None, value: Expr | None) -> None:
        self.key = key
        self.value = value


class Table(Expr):
    _fields = ("fields",)

    def __init__(self, fields: list[TableField]) -> None:
        self.fields = fields


class Param(Node):
    """A function parameter."""

    _fields = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name
        self.new_name: str | None = None

    @property
    def out(self) -> str:
        return self.new_name or self.name


class Function(Expr):
    _fields = ("params", "is_vararg", "body", "is_method")

    def __init__(
        self,
        params: list[Param],
        is_vararg: bool,
        body: Block,
        is_method: bool = False,
    ) -> None:
        self.params = params
        self.is_vararg = is_vararg
        self.body = body
        #: True when written with ``function t:m()`` sugar; the implicit
        #: ``self`` parameter is then part of ``params``.
        self.is_method = is_method


# --------------------------------------------------------------------------
# Statements
# --------------------------------------------------------------------------


class Stat(Node):
    pass


class Block(Node):
    _fields = ("stats",)

    def __init__(self, stats: list[Stat] | None = None) -> None:
        self.stats = stats if stats is not None else []


class LocalDecl(Stat):
    """``local a, b = 1, 2`` (``attribs`` holds 5.4 ``<const>`` marks)."""

    _fields = ("names", "attribs", "exprs")

    def __init__(
        self,
        names: list[str],
        exprs: list[Expr],
        attribs: list[str | None] | None = None,
    ) -> None:
        self.names = names
        self.exprs = exprs
        self.attribs = attribs or [None] * len(names)
        self.new_names: list[str | None] = [None] * len(names)


class LocalFunction(Stat):
    _fields = ("name", "func")

    def __init__(self, name: str, func: Function) -> None:
        self.name = name
        self.func = func
        self.new_name: str | None = None


class Assign(Stat):
    _fields = ("targets", "exprs")

    def __init__(self, targets: list[Expr], exprs: list[Expr]) -> None:
        self.targets = targets
        self.exprs = exprs


class CallStat(Stat):
    _fields = ("call",)

    def __init__(self, call: Expr) -> None:
        self.call = call


class Do(Stat):
    _fields = ("body",)

    def __init__(self, body: Block) -> None:
        self.body = body


class While(Stat):
    _fields = ("cond", "body")

    def __init__(self, cond: Expr, body: Block) -> None:
        self.cond = cond
        self.body = body


class Repeat(Stat):
    _fields = ("body", "cond")

    def __init__(self, body: Block, cond: Expr) -> None:
        self.body = body
        self.cond = cond


class If(Stat):
    """``clauses`` is a list of ``(condition, block)`` pairs; ``orelse`` is the
    else-block (or ``None``)."""

    _fields = ("clauses", "orelse")

    def __init__(
        self,
        clauses: list[tuple[Expr, Block]],
        orelse: Block | None = None,
    ) -> None:
        self.clauses = clauses
        self.orelse = orelse


class NumFor(Stat):
    _fields = ("var", "start", "stop", "step", "body")

    def __init__(
        self,
        var: str,
        start: Expr,
        stop: Expr,
        step: Expr | None,
        body: Block,
    ) -> None:
        self.var = var
        self.start = start
        self.stop = stop
        self.step = step
        self.body = body
        self.new_name: str | None = None


class GenFor(Stat):
    _fields = ("vars", "exprs", "body")

    def __init__(self, vars: list[str], exprs: list[Expr], body: Block) -> None:
        self.vars = vars
        self.exprs = exprs
        self.body = body
        self.new_names: list[str | None] = [None] * len(vars)


class FuncStat(Stat):
    """``function a.b.c:m(...) end``"""

    _fields = ("path", "method", "func")

    def __init__(self, path: list[str], method: str | None, func: Function) -> None:
        self.path = path
        self.method = method
        self.func = func


class Return(Stat):
    _fields = ("exprs",)

    def __init__(self, exprs: list[Expr]) -> None:
        self.exprs = exprs


class Break(Stat):
    pass


class Goto(Stat):
    _fields = ("label",)

    def __init__(self, label: str) -> None:
        self.label = label


class Label(Stat):
    _fields = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name


class Empty(Stat):
    """A stray ``;``"""

    pass


class Chunk(Node):
    _fields = ("body",)

    def __init__(self, body: Block) -> None:
        self.body = body


class Paren(Expr):
    """``(expr)`` — kept separate because it truncates multiple results."""

    _fields = ("expr",)

    def __init__(self, expr: Expr) -> None:
        self.expr = expr
