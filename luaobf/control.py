"""Control-flow obfuscation: opaque predicates, dead code, and junk statements.

Every predicate is built so that its truth value is fixed by integer arithmetic
that a constant folder cannot simplify away, because it depends on a value that
is only known at run time::

    (J*J+1) % 2 == 1      -- always true  (J*J+1 is always odd)
    (J*J+2) % 2 == 1      -- always false (J*J+2 is always even)

``J`` is a junk constant planted at the top of the chunk.  Dead branches are
never executed, so the behaviour of the program cannot change.
"""

from __future__ import annotations

import random

from . import ast_nodes as A
from .renamer import NameGenerator


class ControlFlow:
    def __init__(
        self,
        rng: random.Random,
        ngen: NameGenerator,
        junk_var: str,
        wrap_conditions: bool = True,
        dead_code: int = 1,
        junk_functions: int = 0,
    ) -> None:
        self.rng = rng
        self.ngen = ngen
        self.junk_var = junk_var
        self.wrap_conditions = wrap_conditions
        self.dead_code = dead_code
        self.junk_functions = junk_functions

    # -- predicates --------------------------------------------------------

    def junk_ref(self) -> A.Expr:
        return A.Name(self.junk_var)

    def _consecutive_product(self) -> A.Expr:
        """``J * (J + 1)`` -- the product of consecutive integers, always even."""
        return A.BinOp(
            "*",
            self.junk_ref(),
            A.BinOp("+", self.junk_ref(), A.Number(1, "1", True)),
        )

    def _parity_test(self, offset: int) -> A.Expr:
        """``(J*(J+1) + offset) % 2 == 1``"""
        return A.BinOp(
            "==",
            A.BinOp(
                "%",
                A.BinOp("+", self._consecutive_product(), A.Number(offset, str(offset), True)),
                A.Number(2, "2", True),
            ),
            A.Number(1, "1", True),
        )

    def always_true(self) -> A.Expr:
        # J*(J+1) is even, so +1 is odd and odd % 2 == 1
        return self._parity_test(1)

    def always_false(self) -> A.Expr:
        # J*(J+1) is even, so +2 is even and even % 2 == 0 != 1
        return self._parity_test(2)

    def wrap(self, cond: A.Expr) -> A.Expr:
        """``cond`` -> ``opaque_true and cond`` (double negation on some runs)."""
        if self.rng.random() < 0.5:
            return A.BinOp("and", self.always_true(), cond)
        # (a or not b) style: not (opaque_false) == true
        return A.BinOp("and", A.UnOp("not", self.always_false()), cond)

    # -- junk code ---------------------------------------------------------

    def junk_number(self) -> A.Expr:
        return A.BinOp(
            self.rng.choice(("+", "-", "*")),
            A.Number(self.rng.randrange(1, 999), None, True),
            A.Number(self.rng.randrange(1, 999), None, True),
        )

    def junk_value(self) -> A.Expr:
        kind = self.rng.randrange(4)
        if kind == 0:
            return A.BinOp(
                self.rng.choice(("+", "-", "*")),
                A.Number(self.rng.randrange(1, 999), None, True),
                A.Number(self.rng.randrange(1, 999), None, True),
            )
        if kind == 1:
            return A.String(bytes(self.rng.randrange(97, 123) for _ in range(self.rng.randrange(3, 12))), None)
        if kind == 2:
            return A.Table(
                [
                    A.TableField(None, A.Number(self.rng.randrange(100), None, True))
                    for _ in range(self.rng.randrange(1, 4))
                ]
            )
        return A.FalseExpr() if self.rng.random() < 0.5 else A.Nil()

    def junk_statements(self, count: int) -> list[A.Stat]:
        stats: list[A.Stat] = []
        for _ in range(count):
            name = self.ngen.next()
            # junk variables are always numbers so the follow-up arithmetic is
            # guaranteed to be valid Lua (dead code must still be well-typed)
            local = A.LocalDecl([name], [self.junk_number()])
            local.new_names = [name]
            stats.append(local)
            if self.rng.random() < 0.6:
                stats.append(
                    A.Assign(
                        [A.Name(name)],
                        [
                            A.BinOp(
                                "+",
                                A.Name(name),
                                A.Number(self.rng.randrange(1, 9), None, True),
                            )
                        ],
                    )
                )
        return stats

    def dead_block(self) -> A.Stat:
        body = A.Block(self.junk_statements(self.rng.randrange(1, 4)))
        return A.If([(self.always_false(), body)], None)

    def junk_function(self) -> A.Stat:
        name = self.ngen.next()
        params = [A.Param(self.ngen.next()) for _ in range(self.rng.randrange(1, 3))]
        body = A.Block(self.junk_statements(self.rng.randrange(1, 3)))
        body.stats.append(A.Return([A.BinOp("+", A.Number(1, "1", True), self.junk_number())]))
        func = A.Function(params, False, body)
        stat = A.LocalFunction(name, func)
        stat.new_name = name
        return stat

    # -- traversal ---------------------------------------------------------

    def run(self, chunk: A.Chunk) -> None:
        if self.junk_functions:
            extra = [self.junk_function() for _ in range(self.junk_functions)]
            chunk.body.stats[0:0] = extra

        self.visit_block(chunk.body, top=True)

    def visit_block(self, block: A.Block, top: bool = False) -> None:
        new_stats: list[A.Stat] = []
        for stat in block.stats:
            self.visit_stat(stat)
            new_stats.append(stat)
            # never insert anything after a return/break: it must stay last
            if isinstance(stat, (A.Return, A.Break, A.Continue)):
                continue
            if self.dead_code and self.rng.random() < 0.12 * self.dead_code:
                new_stats.append(self.dead_block())
        block.stats = new_stats

    def visit_stat(self, stat: A.Stat) -> None:
        if isinstance(stat, A.If):
            if self.wrap_conditions:
                stat.clauses = [
                    (self.wrap(cond) if i else self.wrap(cond), blk)
                    for i, (cond, blk) in enumerate(stat.clauses)
                ]
            for _, blk in stat.clauses:
                self.visit_block(blk)
            if stat.orelse is not None:
                self.visit_block(stat.orelse)
        elif isinstance(stat, A.While):
            if self.wrap_conditions:
                stat.cond = self.wrap(stat.cond)
            self.visit_block(stat.body)
        elif isinstance(stat, A.Repeat):
            self.visit_block(stat.body)
            if self.wrap_conditions:
                # `until c` -> `until c and opaque_true`
                stat.cond = A.BinOp("and", stat.cond, self.always_true())
        elif isinstance(stat, A.NumFor):
            self.visit_block(stat.body)
        elif isinstance(stat, A.GenFor):
            self.visit_block(stat.body)
        elif isinstance(stat, A.Do):
            self.visit_block(stat.body)
        elif isinstance(stat, A.LocalFunction):
            self.visit_block(stat.func.body)
        elif isinstance(stat, A.FuncStat):
            self.visit_block(stat.func.body)
        elif isinstance(stat, A.LocalDecl):
            for expr in stat.exprs:
                self.visit_expr(expr)
        elif isinstance(stat, A.Assign):
            for expr in list(stat.targets) + list(stat.exprs):
                self.visit_expr(expr)
        elif isinstance(stat, A.CallStat):
            self.visit_expr(stat.call)
        elif isinstance(stat, A.Return):
            for expr in stat.exprs:
                self.visit_expr(expr)
        elif isinstance(stat, (A.Break, A.Continue, A.Goto, A.Label, A.Empty, A.TypeDecl)):
            pass

    def visit_expr(self, expr: A.Expr) -> None:
        """Find anonymous functions nested in expressions and obfuscate them."""
        if isinstance(expr, A.Function):
            self.visit_block(expr.body)
        elif isinstance(expr, A.BinOp):
            self.visit_expr(expr.left)
            self.visit_expr(expr.right)
        elif isinstance(expr, A.UnOp):
            self.visit_expr(expr.operand)
        elif isinstance(expr, A.Index):
            self.visit_expr(expr.obj)
            self.visit_expr(expr.key)
        elif isinstance(expr, A.Call):
            self.visit_expr(expr.func)
            for arg in expr.args:
                self.visit_expr(arg)
        elif isinstance(expr, A.MethodCall):
            self.visit_expr(expr.obj)
            for arg in expr.args:
                self.visit_expr(arg)
        elif isinstance(expr, A.Table):
            for field in expr.fields:
                if field.key is not None:
                    self.visit_expr(field.key)
                if field.value is not None:
                    self.visit_expr(field.value)
        elif isinstance(expr, A.Paren):
            self.visit_expr(expr.expr)
        elif isinstance(expr, A.IfExpr):
            for cond, value in expr.clauses:
                self.visit_expr(cond)
                self.visit_expr(value)
            self.visit_expr(expr.orelse)


def apply_control_flow(chunk: A.Chunk, rng: random.Random, ngen: NameGenerator, **opts) -> str | None:
    """Run the pass; returns the junk-variable declaration name (if planted)."""
    cf = ControlFlow(rng, ngen, junk_var=ngen.next(), **opts)
    cf.run(chunk)
    return cf.junk_var


def junk_declaration(name: str, rng: random.Random) -> A.Stat:
    """``local <name> = <large random integer>`` placed at the top of the chunk."""
    # Keep J below 2**24: J*(J+1) < 2**48 is exact even with Lua 5.1 doubles.
    value = rng.randrange(1 << 20, 1 << 24)
    decl = A.LocalDecl([name], [A.Number(value, None, True)])
    decl.new_names = [name]
    return decl
