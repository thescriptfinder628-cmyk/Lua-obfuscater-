"""Control-flow flattening.

A function body is cut into segments and re-emitted as a state machine::

    local s = 3                        -- entry state (not 1: order is shuffled)
    while true do
      if s == 3 then  <segment A>  s = 1
      elseif s == 7 then  <segment B>  s = 1
      elseif s == 1 then  <segment C>  break
      end
    end

The branches are emitted in a **random order**, so the source order no longer
reflects execution order at all.  Reading the function top-to-bottom tells you
nothing; you have to follow the state numbers, which are random.

Correctness rules (this is the part that is easy to get wrong):

* A segment boundary is only allowed where no local declared before it is used
  after it.  Locals that are used later force the segments to be merged, so
  scoping is bit-for-bit identical to the original.
* ``return``/``break`` stay inside their own branch, where they remain the last
  statement of that block (as Lua requires).
* Nested functions are flattened recursively, independently.
* Blocks containing labels/goto/continue are left alone.

The pass runs *after* scope resolution, so it uses real bindings (not names) to
decide what is used where.
"""

from __future__ import annotations

import random

from . import ast_nodes as A
from .renamer import NameGenerator, walk

#: Statements that end a block and must not be moved into a state machine.
TERMINATORS = (A.Return, A.Break, A.Goto, A.Continue, A.Label)

#: Refuse to flatten tiny bodies: not worth the size cost.
MIN_STATEMENTS = 3
MAX_SEGMENTS = 24


class Flattener:
    def __init__(self, rng: random.Random, ngen: NameGenerator, resolver=None, intensity: int = 1) -> None:
        self.rng = rng
        self.ngen = ngen
        self.resolver = resolver
        self.intensity = max(1, intensity)
        self.flattened = 0

    # -- entry -------------------------------------------------------------

    def run(self, chunk: A.Chunk) -> int:
        """Flatten every eligible body; returns how many bodies were flattened."""
        self.flatten_block(chunk.body, top=True)
        return self.flattened

    def flatten_block(self, block: A.Block, top: bool = False, allow: bool = True) -> None:
        # recurse first: inner functions get flattened independently
        for stat in block.stats:
            self.flatten_nested(stat)

        if not allow or not self._eligible(block):
            return

        segments = self._segment(block)
        if len(segments) < 2:
            return
        block.stats = [self._dispatcher(segments)]
        self.flattened += 1

    def flatten_nested(self, stat: A.Stat) -> None:
        if isinstance(stat, A.LocalFunction):
            self.flatten_block(stat.func.body)
        elif isinstance(stat, A.FuncStat):
            self.flatten_block(stat.func.body)
        elif isinstance(stat, A.Do):
            self.flatten_block(stat.body)
        elif isinstance(stat, A.While):
            self.flatten_block(stat.body)
        elif isinstance(stat, A.Repeat):
            # A `repeat` body's locals are visible in its `until` condition, so
            # a flattened body would make that condition see different variables.
            self.flatten_block(stat.body, allow=False)
        elif isinstance(stat, (A.NumFor, A.GenFor)):
            self.flatten_block(stat.body)
        elif isinstance(stat, A.If):
            for _, blk in stat.clauses:
                self.flatten_block(blk)
            if stat.orelse is not None:
                self.flatten_block(stat.orelse)
        elif isinstance(stat, A.LocalDecl):
            for expr in stat.exprs:
                self.flatten_expr(expr)
        elif isinstance(stat, A.Assign):
            for expr in list(stat.targets) + list(stat.exprs):
                self.flatten_expr(expr)
        elif isinstance(stat, A.CallStat):
            self.flatten_expr(stat.call)
        elif isinstance(stat, A.Return):
            for expr in stat.exprs:
                self.flatten_expr(expr)

    def flatten_expr(self, expr: A.Expr) -> None:
        if isinstance(expr, A.Function):
            self.flatten_block(expr.body)
        elif isinstance(expr, A.BinOp):
            self.flatten_expr(expr.left)
            self.flatten_expr(expr.right)
        elif isinstance(expr, A.UnOp):
            self.flatten_expr(expr.operand)
        elif isinstance(expr, A.Index):
            self.flatten_expr(expr.obj)
            self.flatten_expr(expr.key)
        elif isinstance(expr, A.Call):
            self.flatten_expr(expr.func)
            for arg in expr.args:
                self.flatten_expr(arg)
        elif isinstance(expr, A.MethodCall):
            self.flatten_expr(expr.obj)
            for arg in expr.args:
                self.flatten_expr(arg)
        elif isinstance(expr, A.Table):
            for field in expr.fields:
                if field.key is not None:
                    self.flatten_expr(field.key)
                if field.value is not None:
                    self.flatten_expr(field.value)
        elif isinstance(expr, A.Paren):
            self.flatten_expr(expr.expr)
        elif isinstance(expr, A.IfExpr):
            for cond, value in expr.clauses:
                self.flatten_expr(cond)
                self.flatten_expr(value)
            self.flatten_expr(expr.orelse)

    # -- eligibility -------------------------------------------------------

    def _eligible(self, block: A.Block) -> bool:
        stats = block.stats
        if len(stats) < MIN_STATEMENTS:
            return False
        for stat in stats:
            # A `break`/`continue`/`goto` that belongs to an enclosing loop would
            # escape the dispatcher (`break`) or jump across states, so any such
            # jump that is not contained in a nested loop disqualifies the block.
            if _escapes_block(stat):
                return False
        # a return must stay last; only flatten if that holds
        for index, stat in enumerate(stats[:-1]):
            if isinstance(stat, A.Return):
                return False
        return True

    # -- segmentation ------------------------------------------------------

    def _segment(self, block: A.Block) -> list[list[A.Stat]]:
        """Group statements so no local is used outside its own group."""
        stats = block.stats
        count = len(stats)

        # Which bindings does each statement reference, and declare?
        # Declarations are only recorded in the resolver's binding table, and
        # references are counted with a *full* walk: a nested function that
        # captures a local as an upvalue keeps that local alive too.
        used: list[set[int]] = [set() for _ in range(count)]
        declared: list[set[int]] = [set() for _ in range(count)]

        owner = {id(stat): index for index, stat in enumerate(stats)}
        if self.resolver is not None:
            for binding in self.resolver.bindings:
                index = owner.get(id(binding.node))
                if index is not None:
                    declared[index].add(id(binding))

        for index, stat in enumerate(stats):
            for node in walk(stat):
                if isinstance(node, A.Name) and node.binding is not None:
                    used[index].add(id(node.binding))

        # a declaration that is referenced later forces a merge
        last_use: dict[int, int] = {}
        for index in range(count):
            for binding_id in used[index]:
                last_use[binding_id] = max(last_use.get(binding_id, index), index)

        segments: list[list[A.Stat]] = []
        current: list[A.Stat] = []
        open_bindings: dict[int, int] = {}  # binding -> last statement that uses it

        for index, stat in enumerate(stats):
            current.append(stat)
            for binding_id in declared[index]:
                end = last_use.get(binding_id, index)
                open_bindings[binding_id] = max(open_bindings.get(binding_id, index), end)
            # drop bindings we have passed
            for binding_id in [b for b, end in open_bindings.items() if end <= index]:
                del open_bindings[binding_id]

            if index == count - 1:
                segments.append(current)
                current = []
                break
            if not open_bindings:
                # safe to cut here
                segments.append(current)
                current = []
            if len(segments) >= MAX_SEGMENTS:
                # stop early: everything left goes into the final segment
                current.extend(stats[index + 1 :])
                segments.append(current)
                current = []
                break

        if current:
            segments.append(current)
        return [seg for seg in segments if seg]

    # -- code generation ---------------------------------------------------

    def _dispatcher(self, segments: list[list[A.Stat]]) -> A.Stat:
        rng = self.rng
        state_var = self.ngen.next()
        count = len(segments)

        # random, non-sequential state numbers
        pool = rng.sample(range(2, 2 + count * 4), count)
        states = list(pool)

        # Execution order is always 0,1,2,… (that is the program's meaning).
        # Only the *emitted* order of the branches and the state numbers are
        # randomised, so following the code top-to-bottom is meaningless.
        following: dict[int, int | None] = {}
        for index in range(count):
            following[index] = states[index + 1] if index + 1 < count else None

        clauses: list[tuple[A.Expr, A.Block]] = []
        branch_order = list(range(count))
        rng.shuffle(branch_order)  # emit the branches out of execution order

        for segment_index in branch_order:
            body_stats: list[A.Stat] = list(segments[segment_index])
            next_state = following[segment_index]
            ends_with_return = bool(body_stats) and isinstance(body_stats[-1], (A.Return,))
            if not ends_with_return:
                if next_state is None:
                    body_stats.append(A.Break())
                else:
                    body_stats.append(A.Assign([A.Name(state_var)], [A.Number(next_state, None, True)]))
            condition = A.BinOp("==", A.Name(state_var), A.Number(states[segment_index], None, True))
            clauses.append((condition, A.Block(body_stats)))

        entry = A.LocalDecl([state_var], [A.Number(states[0], None, True)])
        entry.new_names = [state_var]
        loop = A.While(A.TrueExpr(), A.Block([A.If(clauses, None)]))
        return A.Do(A.Block([entry, loop]))


def _escapes_block(stat) -> bool:
    """Would a jump inside this statement leave the block being flattened?

    Descends into `if`/`do` bodies but stops at nested loops (their jumps are
    their own) and at nested functions.
    """
    if isinstance(stat, (A.Break, A.Continue, A.Goto, A.Label)):
        return True
    if isinstance(stat, (A.While, A.Repeat, A.NumFor, A.GenFor, A.Function, A.LocalFunction, A.FuncStat)):
        return False
    if isinstance(stat, A.If):
        for _, blk in stat.clauses:
            if any(_escapes_block(sub) for sub in blk.stats):
                return True
        if stat.orelse is not None and any(_escapes_block(sub) for sub in stat.orelse.stats):
            return True
        return False
    if isinstance(stat, A.Do):
        return any(_escapes_block(sub) for sub in stat.body.stats)
    return False


def _walk_stat(stat: A.Stat):
    """Yield nodes of a statement, not descending into nested function bodies."""
    stack = [stat]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (A.Function, A.LocalFunction, A.FuncStat)):
            continue  # a nested function is its own scope and its own flattened unit
        for field in getattr(node, "_fields", ()):
            value = getattr(node, field, None)
            if isinstance(value, A.Node):
                stack.append(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, A.Node):
                        stack.append(item)
                    elif isinstance(item, tuple):
                        for sub in item:
                            if isinstance(sub, A.Node):
                                stack.append(sub)


def apply_flatten(chunk: A.Chunk, rng: random.Random, ngen: NameGenerator, resolver=None, intensity: int = 1) -> int:
    return Flattener(rng, ngen, resolver, intensity).run(chunk)
