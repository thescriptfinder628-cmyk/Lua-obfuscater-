"""Lower language features that the chosen target does not support.

Most "obfuscation" tools only work for the dialect they were written for.  This
pass makes one output work everywhere:

* ``goto``/labels      -> removed (loops become flagged or nested structures)
* ``continue``         -> ``goto`` label (5.2+) or ``repeat … until true`` +
                          ``break`` (5.1/LuaJIT) or kept (Luau)
* ``if … then … else`` expressions -> ``and``/``or`` chains or an IIFE
* bitwise operators    -> arithmetic emulation (32-bit, exact for 32-bit values)
* ``//``               -> ``math.floor(a / b)``
* ``<const>``/``<close>`` attributes -> stripped
* Luau type declarations -> dropped

The pass is deliberately conservative: it refuses (with a clear error) when it
cannot preserve semantics, instead of emitting subtly broken Lua.
"""

from __future__ import annotations

from . import ast_nodes as A
from .targets import Target


class LoweringError(RuntimeError):
    """Raised when a feature cannot be expressed on the requested target."""


#: names used by the generated helpers.  They are deliberately obscure and get
#: renamed by the normal renaming pass anyway.
HELPERS = {
    "band": "__lx_band",
    "bor": "__lx_bor",
    "bxor": "__lx_bxor",
    "bnot": "__lx_bnot",
    "shl": "__lx_shl",
    "shr": "__lx_shr",
    "floor": "__lx_floor",
}


def helper_source() -> str:
    """Arithmetic implementations of the 5.3 bitwise library (32-bit exact)."""
    b, bo, bx, bn, shl, shr, fl = (
        HELPERS["band"],
        HELPERS["bor"],
        HELPERS["bxor"],
        HELPERS["bnot"],
        HELPERS["shl"],
        HELPERS["shr"],
        HELPERS["floor"],
    )
    return f"""local function {fl}(x) local f=math.floor(x) if f>x then f=f+1 end return f end
local function {b}(x,y) local r,m=0,1 for _=1,32 do local a=x%2 local c=y%2 if a==1 and c==1 then r=r+m end x=(x-a)/2 y=(y-c)/2 m=m*2 end return r end
local function {bo}(x,y) local r,m=0,1 for _=1,32 do local a=x%2 local c=y%2 if a==1 or c==1 then r=r+m end x=(x-a)/2 y=(y-c)/2 m=m*2 end return r end
local function {bx}(x,y) local r,m=0,1 for _=1,32 do local a=x%2 local c=y%2 if a~=c then r=r+m end x=(x-a)/2 y=(y-c)/2 m=m*2 end return r end
local function {bn}(x) return -(x)-1 end
local function {shl}(x,n) n=n%32 return ({fl}(x)*2^n)%2^32 end
local function {shr}(x,n) n=n%32 return {fl}(({fl}(x)-x%2)/2^n) end
"""


class Lowerer:
    def __init__(self, target: Target) -> None:
        self.target = target
        self.used = set()
        self.loop_stack: list[dict] = []
        self.label_counter = 0
        self.continue_counter = 0
        self.helpers_needed = False

    # -- helpers -----------------------------------------------------------

    def _fresh_label(self) -> str:
        self.label_counter += 1
        return f"__cont_{self.label_counter}"

    def binop(self, op: str, left: A.Expr, right: A.Expr) -> A.Expr:
        """Build a binary operation, lowering it if the target lacks it."""
        if op in ("&", "|", "~", "<<", ">>") and self.target.needs_bitwise_lowering:
            self.helpers_needed = True
            fn = {
                "&": HELPERS["band"],
                "|": HELPERS["bor"],
                "~": HELPERS["bxor"],
                "<<": HELPERS["shl"],
                ">>": HELPERS["shr"],
            }[op]
            return A.Call(A.Name(fn), [left, right])
        if op == "//" and self.target.needs_fdiv_lowering:
            self.helpers_needed = True
            return A.Call(A.Name(HELPERS["floor"]), [A.BinOp("/", left, right)])
        return A.BinOp(op, left, right)

    def unop(self, op: str, operand: A.Expr) -> A.Expr:
        if op == "~" and self.target.needs_bitwise_lowering:
            self.helpers_needed = True
            return A.Call(A.Name(HELPERS["bnot"]), [operand])
        return A.UnOp(op, operand)

    # -- entry -------------------------------------------------------------

    def run(self, chunk: A.Chunk) -> bool:
        """Lower the chunk in place; returns True if helper code is needed."""
        chunk.body = self.block(chunk.body)
        return self.helpers_needed

    # -- statements --------------------------------------------------------

    def block(self, block: A.Block) -> A.Block:
        stats: list[A.Stat] = []
        for stat in block.stats:
            lowered = self.stat(stat)
            if lowered is None:
                continue
            if isinstance(lowered, list):
                stats.extend(lowered)
            else:
                stats.append(lowered)
        block.stats = stats
        return block

    def stat(self, node: A.Stat):
        if isinstance(node, A.LocalDecl):
            node.exprs = [self.expr(e) for e in node.exprs]
            if not self.target.has_attributes:
                node.attribs = [None] * len(node.names)
            return node

        if isinstance(node, A.Assign):
            node.targets = [self.expr(t) for t in node.targets]
            node.exprs = [self.expr(e) for e in node.exprs]
            return node

        if isinstance(node, A.CallStat):
            node.call = self.expr(node.call)
            return node

        if isinstance(node, A.Return):
            node.exprs = [self.expr(e) for e in node.exprs]
            return node

        if isinstance(node, A.LocalFunction):
            self.function(node.func)
            return node

        if isinstance(node, A.FuncStat):
            self.function(node.func)
            return node

        if isinstance(node, A.Do):
            node.body = self.block(node.body)
            return node

        if isinstance(node, A.While):
            node.cond = self.expr(node.cond)
            node.body = self.loop_body(node.body)
            return node

        if isinstance(node, A.Repeat):
            node.body = self.loop_body(node.body, is_repeat=True)
            node.cond = self.expr(node.cond)
            return node

        if isinstance(node, A.NumFor):
            node.start = self.expr(node.start)
            node.stop = self.expr(node.stop)
            node.step = self.expr(node.step) if node.step is not None else None
            node.body = self.loop_body(node.body)
            return node

        if isinstance(node, A.GenFor):
            node.exprs = [self.expr(e) for e in node.exprs]
            node.body = self.loop_body(node.body)
            return node

        if isinstance(node, A.If):
            node.clauses = [(self.expr(c), self.block(b)) for c, b in node.clauses]
            if node.orelse is not None:
                node.orelse = self.block(node.orelse)
            return node

        if isinstance(node, A.Break):
            # inside a 5.1-style continue wrapper, `break` must also escape the
            # wrapper: set the flag, then really break
            if self.loop_stack and self.loop_stack[-1]["kind"] == "break":
                flag = self.loop_stack[-1]["flag"]
                return [A.Assign([A.Name(flag)], [A.TrueExpr()]), A.Break()]
            return node

        if isinstance(node, (A.Empty, A.TypeDecl)):
            return None if isinstance(node, A.TypeDecl) else node

        if isinstance(node, (A.Label, A.Goto)):
            if not self.target.has_goto:
                raise LoweringError(
                    f"the script uses goto/labels, which Lua {self.target.version} does not "
                    f"support; use --target lua52 or newer"
                )
            return node

        if isinstance(node, A.Continue):
            return self.continue_stat()

        raise LoweringError(f"cannot lower statement {type(node).__name__}")

    # -- loops and continue ------------------------------------------------

    def loop_body(self, body: A.Block, is_repeat: bool = False) -> A.Block:
        """Lower ``continue`` inside a loop body for the current target."""
        if not _loop_uses_continue(body):
            # nothing to do, but still descend for other lowerings
            self.loop_stack.append({"kind": "plain"})
            body = self.block(body)
            self.loop_stack.pop()
            return body

        if self.target.has_continue:
            self.loop_stack.append({"kind": "native"})
            body = self.block(body)
            self.loop_stack.pop()
            return body

        if self.target.has_goto:
            label = self._fresh_label()
            # The label goes *after* an inner scope, so a forward `goto` never
            # jumps into the scope of a local declared in the body (illegal).
            self.loop_stack.append({"kind": "goto", "label": label})
            inner = self.block(body)
            self.loop_stack.pop()
            return A.Block([A.Do(inner), A.Label(label)])

        # Lua 5.1 / LuaJIT: wrap the body once and use `break` as `continue`,
        # with a flag that makes an original `break` still leave the loop.
        self.continue_counter += 1
        flag = f"__brk{self.continue_counter}"
        self.loop_stack.append({"kind": "break", "flag": flag})
        inner = self.block(body)
        self.loop_stack.pop()
        return A.Block(
            [
                A.LocalDecl([flag], [A.FalseExpr()]),
                A.Repeat(inner, A.TrueExpr()),
                A.If([(A.Name(flag), A.Block([A.Break()]))], None),
            ]
        )

    def continue_stat(self):
        if not self.loop_stack:
            raise LoweringError("`continue` used outside of a loop")
        context = self.loop_stack[-1]
        if context["kind"] == "native":
            return A.Continue()
        if context["kind"] == "goto":
            return A.Goto(context["label"])
        if context["kind"] == "break":
            return A.Break()
        raise LoweringError("`continue` used outside of a loop")

    # -- expressions -------------------------------------------------------

    def function(self, node: A.Function) -> None:
        saved = self.loop_stack
        self.loop_stack = []
        node.body = self.block(node.body)
        self.loop_stack = saved

    def expr(self, node: A.Expr) -> A.Expr:
        if isinstance(node, (A.Nil, A.TrueExpr, A.FalseExpr, A.Number, A.String, A.Name, A.Vararg)):
            return node

        if isinstance(node, A.BinOp):
            left = self.expr(node.left)
            right = self.expr(node.right)
            return self.binop(node.op, left, right)

        if isinstance(node, A.UnOp):
            operand = self.expr(node.operand)
            return self.unop(node.op, operand)

        if isinstance(node, A.Index):
            node.obj = self.expr(node.obj)
            node.key = self.expr(node.key)
            return node

        if isinstance(node, A.Call):
            node.func = self.expr(node.func)
            node.args = [self.expr(a) for a in node.args]
            return node

        if isinstance(node, A.MethodCall):
            node.obj = self.expr(node.obj)
            node.args = [self.expr(a) for a in node.args]
            return node

        if isinstance(node, A.Table):
            for field in node.fields:
                if field.key is not None:
                    field.key = self.expr(field.key)
                if field.value is not None:
                    field.value = self.expr(field.value)
            return node

        if isinstance(node, A.Function):
            self.function(node)
            return node

        if isinstance(node, A.Paren):
            node.expr = self.expr(node.expr)
            return node

        if isinstance(node, A.IfExpr):
            lowered = self.expr_of(node)
            return self.expr(lowered)

        raise LoweringError(f"cannot lower expression {type(node).__name__}")

    # -- if-expressions ----------------------------------------------------

    def expr_of(self, node: A.IfExpr) -> A.Expr:
        clauses = [(self.expr(c), self.expr(v)) for c, v in node.clauses]
        orelse = self.expr(node.orelse)

        if self.target.has_if_expr:
            node.clauses = clauses
            node.orelse = orelse
            return node

        # `and`/`or` chaining is only equivalent when every branch is truthy
        if all(_is_truthy(v) for _, v in clauses) and _is_truthy(orelse):
            result = orelse
            for cond, value in reversed(clauses):
                result = A.BinOp("or", A.BinOp("and", cond, value), result)
            return result

        # general case: an immediately-invoked function
        body_stats: list[A.Stat] = [
            A.If([(cond, A.Block([A.Return([value])])) for cond, value in clauses], A.Block([A.Return([orelse])]))
        ]
        needs_varargs = any(_has_vararg(v) for v in [*(v for _, v in clauses), orelse])
        func = A.Function([], needs_varargs, A.Block(body_stats))
        args: list[A.Expr] = [A.Vararg()] if needs_varargs else []
        return A.Call(func, args)


def _is_truthy(node: A.Expr) -> bool:
    return not isinstance(node, (A.Nil, A.FalseExpr))


def _has_vararg(node: A.Node) -> bool:
    from .renamer import walk

    return any(isinstance(n, A.Vararg) for n in walk(node))


def _loop_uses_continue(block: A.Block) -> bool:
    """Does this loop body contain a `continue` for *this* loop?

    Nested loops and nested functions own their own `continue`, so we must not
    descend into them.
    """
    return _scan_continue(block)


def _scan_continue(node) -> bool:
    if isinstance(node, A.Continue):
        return True
    if isinstance(node, (A.While, A.Repeat, A.NumFor, A.GenFor, A.Function, A.LocalFunction, A.FuncStat)):
        return False
    for field in getattr(node, "_fields", ()):
        value = getattr(node, field, None)
        if isinstance(value, A.Node):
            if _scan_continue(value):
                return True
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, tuple):
                    for sub in item:
                        if isinstance(sub, A.Node) and _scan_continue(sub):
                            return True
                elif isinstance(item, A.Node) and _scan_continue(item):
                    return True
    return False


def lower_chunk(chunk: A.Chunk, target: Target) -> tuple[str, list[str]]:
    """Lower ``chunk`` in place.  Returns ``(helper_prelude, warnings)``."""
    lowerer = Lowerer(target)
    needs_helpers = lowerer.run(chunk)
    warnings: list[str] = []
    prelude = ""
    if needs_helpers:
        prelude = helper_source()
        warnings.append(
            f"bitwise operators were emulated arithmetically for {target.name} "
            "(exact for 32-bit values; on targets without an integer subtype the "
            "results are floats, which is what that runtime itself would produce)"
        )
    return prelude, warnings
