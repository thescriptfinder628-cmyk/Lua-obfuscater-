"""Scope resolution: bind every identifier to its declaration.

Getting this right is what makes renaming safe.  The Lua rules that matter:

* ``local x = x``  -- the initialiser sees the *outer* ``x``, so the declaration
  takes effect only after the statement.
* ``local function f`` -- ``f`` is visible inside its own body (recursion).
* ``repeat ... until c`` -- locals declared in the body are visible in ``c``.
* ``for i = a, b`` -- ``a``/``b`` are evaluated outside ``i``'s scope.
* ``function t:m()`` -- an implicit ``self`` parameter exists.
* Every block introduces a scope; closures capture upvalues by reference.
"""

from __future__ import annotations

from . import ast_nodes as A


class Binding:
    """A declaration that identifiers can refer to."""

    __slots__ = ("kind", "node", "slot", "name", "new_name", "refs", "renamable")

    def __init__(self, kind: str, node, slot: int, name: str, renamable: bool = True) -> None:
        self.kind = kind  # 'local' | 'param' | 'for'
        self.node = node
        self.slot = slot
        self.name = name
        self.new_name: str | None = None
        self.refs = 0
        self.renamable = renamable

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Binding {self.name}#{id(self) % 1000}>"


class Scope:
    __slots__ = ("parent", "names", "kind")

    def __init__(self, parent: "Scope | None", kind: str = "block") -> None:
        self.parent = parent
        self.names: dict[str, Binding] = {}
        self.kind = kind

    def declare(self, name: str, binding: Binding) -> None:
        self.names[name] = binding

    def lookup(self, name: str) -> Binding | None:
        scope: Scope | None = self
        while scope is not None:
            b = scope.names.get(name)
            if b is not None:
                return b
            scope = scope.parent
        return None


#: Globals that let a script reach variables *by name at runtime*.  Renaming
#: locals is still sound here (the compiler already resolved those names), but
#: the script is no longer statically analysable, so we warn.
#:
#: Note: ``setmetatable``/``rawget``/``require`` are deliberately absent -- they
#: touch fields and modules, not variable names, and warning on them makes the
#: tool noisy for ordinary OOP code.
DYNAMIC_GLOBALS = frozenset(
    {
        "load",
        "loadstring",
        "loadfile",
        "dofile",
        "getfenv",
        "setfenv",
        "debug",
        "_G",
        "_ENV",
        "module",
    }
)


class Resolver:
    def __init__(self) -> None:
        self.scope: Scope | None = None
        self.globals_used: set[str] = set()
        self.dynamic_features: set[str] = set()
        self.bindings: list[Binding] = []
        self.function_depth = 0
        #: names that must never be renamed (implicit method ``self``)
        self.protected: set[str] = set()

    # -- helpers -----------------------------------------------------------

    def push(self, kind: str = "block") -> Scope:
        self.scope = Scope(self.scope, kind)
        return self.scope

    def pop(self) -> None:
        assert self.scope is not None
        self.scope = self.scope.parent

    def declare(self, name: str, node, slot: int, kind: str, renamable: bool = True) -> Binding:
        assert self.scope is not None
        b = Binding(kind, node, slot, name, renamable)
        self.scope.declare(name, b)
        self.bindings.append(b)
        return b

    def reference(self, node: A.Name) -> None:
        b = self.scope.lookup(node.id) if self.scope else None
        if b is not None:
            node.binding = b
            node.scope_kind = b.kind
            b.refs += 1
        else:
            node.binding = None
            node.scope_kind = "global"
            self.globals_used.add(node.id)
            if node.id in DYNAMIC_GLOBALS:
                self.dynamic_features.add(node.id)

    # -- entry -------------------------------------------------------------

    def run(self, chunk: A.Chunk) -> None:
        self.push("chunk")
        for stat in chunk.body.stats:
            self.stat(stat)
        self.pop()

    # -- statements --------------------------------------------------------

    def block(self, blk: A.Block, new_scope: bool = True) -> None:
        if new_scope:
            self.push()
        for stat in blk.stats:
            self.stat(stat)
        if new_scope:
            self.pop()

    def stat(self, node: A.Stat) -> None:
        if isinstance(node, A.LocalDecl):
            # initialisers are evaluated before the names come into scope
            for expr in node.exprs:
                self.expr(expr)
            for slot, name in enumerate(node.names):
                self.declare(name, node, slot, "local")

        elif isinstance(node, A.LocalFunction):
            b = self.declare(node.name, node, 0, "local")
            self.function(node.func, local_binding=b)

        elif isinstance(node, A.Assign):
            for target in node.targets:
                self.expr(target)
            for expr in node.exprs:
                self.expr(expr)

        elif isinstance(node, A.CallStat):
            self.expr(node.call)

        elif isinstance(node, A.Do):
            self.block(node.body)

        elif isinstance(node, A.While):
            self.expr(node.cond)
            self.block(node.body)

        elif isinstance(node, A.Repeat):
            self.push()
            for stat in node.body.stats:
                self.stat(stat)
            self.expr(node.cond)  # body locals are visible here
            self.pop()

        elif isinstance(node, A.If):
            for cond, blk in node.clauses:
                self.expr(cond)
                self.block(blk)
            if node.orelse is not None:
                self.block(node.orelse)

        elif isinstance(node, A.NumFor):
            self.expr(node.start)
            self.expr(node.stop)
            if node.step is not None:
                self.expr(node.step)
            self.push("for")
            self.declare(node.var, node, 0, "for")
            self.block(node.body, new_scope=False)
            self.pop()

        elif isinstance(node, A.GenFor):
            for expr in node.exprs:
                self.expr(expr)
            self.push("for")
            for slot, name in enumerate(node.vars):
                self.declare(name, node, slot, "for")
            self.block(node.body, new_scope=False)
            self.pop()

        elif isinstance(node, A.FuncStat):
            # function a.b.c:m() ... -> only "a" is a variable reference
            root = A.Name(node.path[0])
            self.reference(root)
            node.path_ref = root
            self.function(node.func, is_method=node.method is not None)

        elif isinstance(node, A.Return):
            for expr in node.exprs:
                self.expr(expr)

        elif isinstance(node, (A.Break, A.Goto, A.Label, A.Empty)):
            pass

        else:
            raise TypeError(f"resolver: unknown statement {type(node).__name__}")

    # -- functions ---------------------------------------------------------

    def function(self, node: A.Function, is_method: bool = False, local_binding=None) -> None:
        self.function_depth += 1
        self.push("function")
        for slot, param in enumerate(node.params):
            renamable = True
            if node.is_method and slot == 0 and param.name == "self":
                renamable = False  # implicit self must keep its name
            self.declare(param.name, param, slot, "param", renamable=renamable)
        # the body block shares the parameter scope
        self.block(node.body, new_scope=False)
        self.pop()
        self.function_depth -= 1

    # -- expressions -------------------------------------------------------

    def expr(self, node: A.Expr) -> None:
        if isinstance(node, A.Name):
            self.reference(node)
        elif isinstance(node, (A.Nil, A.TrueExpr, A.FalseExpr, A.Number, A.String, A.Vararg)):
            pass
        elif isinstance(node, A.BinOp):
            self.expr(node.left)
            self.expr(node.right)
        elif isinstance(node, A.UnOp):
            self.expr(node.operand)
        elif isinstance(node, A.Index):
            self.expr(node.obj)
            self.expr(node.key)
        elif isinstance(node, A.Call):
            self.expr(node.func)
            for arg in node.args:
                self.expr(arg)
        elif isinstance(node, A.MethodCall):
            self.expr(node.obj)
            for arg in node.args:
                self.expr(arg)
        elif isinstance(node, A.Table):
            for field in node.fields:
                if field.key is not None:
                    self.expr(field.key)
                if field.value is not None:
                    self.expr(field.value)
        elif isinstance(node, A.Function):
            self.function(node)
        elif isinstance(node, A.Paren):
            self.expr(node.expr)
        else:
            raise TypeError(f"resolver: unknown expression {type(node).__name__}")


def resolve(chunk: A.Chunk) -> Resolver:
    """Annotate every :class:`Name` with the :class:`Binding` it refers to."""
    resolver = Resolver()
    resolver.run(chunk)
    return resolver
